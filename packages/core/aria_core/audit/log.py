"""The append-only, hash-chained audit log (SECURITY.md §16).

Every agent action, policy decision, S2 access, credential use and state
transition is appended here. Three properties make the log worth trusting:

**Tamper-evidence.** Each event's hash covers the previous event's hash, so
editing or deleting one breaks verification for everything after it.
:func:`verify_chain` reports where.

**No gaps or forks.** Writes to one chain are serialized with a transaction-level
advisory lock, so two concurrent writers cannot both claim sequence *n* with the
same predecessor. Without that, two events could share a ``prev_hash`` and a
verifier could not tell which history is the real one. A unique constraint on
``(chain_id, seq)`` is the backstop if the lock is ever bypassed.

**Nothing is lost to over-caution.** Reasons and payloads are scanned for
sensitive patterns and redacted, never rejected: an event that fails to write is
worse than an event carrying ``[redacted:ssn]`` (see :mod:`aria_core.redaction`).

Two kinds of chain: one per tenant, keyed by the tenant id, and one platform chain
keyed by the nil UUID for events that belong to no tenant. Which one a write lands
on follows from the transaction's scope, and the row-level security policies
enforce that a tenant cannot read or write the other.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.db.models import AuditEvent
from aria_core.redaction import redact_sensitive
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    AuditRecord,
    SubjectKind,
)
from aria_core.schemas.identity import PLATFORM_CHAIN_ID, AuditChainId, TenantId

__all__ = ["GENESIS_HASH", "ChainVerification", "append_event", "read_chain", "verify_chain"]

#: The ``prev_hash`` of the first event in a chain.
GENESIS_HASH: Final = "0" * 64

#: Namespace for the advisory lock, so ARIA's locks cannot collide with another
#: application's on the same database. Bytes of "ARIA".
_LOCK_NAMESPACE: Final = 0x41524941


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The result of walking a chain."""

    chain_id: uuid.UUID
    length: int
    valid: bool
    #: Sequence number of the first event that did not verify, if any.
    broken_at: int | None = None
    detail: str = ""


def _canonical(payload: dict[str, Any]) -> str:
    """A byte-stable rendering, so the same event always hashes the same.

    Sorted keys, no insignificant whitespace, and ``ensure_ascii=False`` so the
    hash does not depend on how a non-ASCII character happens to be escaped.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(
    *,
    chain_id: uuid.UUID,
    seq: int,
    prev_hash: str,
    tenant_id: uuid.UUID | None,
    event: AuditEventDraft,
) -> str:
    """The hash of one event, covering its content and its place in the chain."""
    return hashlib.sha256(
        _canonical(
            {
                "chain_id": str(chain_id),
                "seq": seq,
                "prev_hash": prev_hash,
                "tenant_id": str(tenant_id) if tenant_id else None,
                "occurred_at": event.occurred_at.isoformat(),
                "action": event.action.value,
                "actor_kind": event.actor_kind.value,
                "actor_id": event.actor_id,
                "subject_kind": event.subject_kind.value,
                "subject_id": event.subject_id,
                "outcome": event.outcome.value,
                "reason": event.reason,
                "payload": event.payload,
            }
        ).encode("utf-8")
    ).hexdigest()


async def _lock_chain(session: AsyncSession, chain_id: uuid.UUID) -> None:
    """Serialize writers of one chain for the rest of this transaction.

    Transaction-scoped, so the lock is released on commit or rollback with no
    cleanup path to forget. Writers of different chains never wait on each other.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:chain_id))"),
        {"namespace": _LOCK_NAMESPACE, "chain_id": str(chain_id)},
    )


async def _chain_head(session: AsyncSession, chain_id: uuid.UUID) -> tuple[int, str]:
    """``(seq, hash)`` of the last event, or ``(0, GENESIS_HASH)`` for a new chain."""
    row = (
        await session.execute(
            select(AuditEvent.seq, AuditEvent.hash)
            .where(AuditEvent.chain_id == chain_id)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return 0, GENESIS_HASH
    return row[0], row[1]


async def append_event(
    session: AsyncSession,
    draft: AuditEventDraft,
    *,
    tenant_id: TenantId | None = None,
) -> AuditRecord:
    """Append one event to a tenant's chain, or to the platform chain.

    Pass ``tenant_id`` inside a tenant-scoped transaction; omit it inside a
    platform-scoped one. The write happens in the caller's transaction, so an
    action and its audit event commit together or not at all — a state change can
    never be recorded without its event, or an event without its change.
    """
    chain_id: AuditChainId = AuditChainId(tenant_id) if tenant_id is not None else PLATFORM_CHAIN_ID

    redacted_reason, reason_hits = redact_sensitive(draft.reason)
    redacted_payload: dict[str, Any] = {}
    payload_hits: set[str] = set()
    for key, value in draft.payload.items():
        if isinstance(value, str):
            cleaned, hits = redact_sensitive(value)
            redacted_payload[key] = cleaned
            payload_hits.update(hits)
        else:
            redacted_payload[key] = value
    redactions = sorted(set(reason_hits) | payload_hits)

    safe = draft.model_copy(update={"reason": redacted_reason, "payload": redacted_payload})

    await _lock_chain(session, chain_id)
    previous_seq, previous_hash = await _chain_head(session, chain_id)
    seq = previous_seq + 1
    digest = compute_hash(
        chain_id=chain_id, seq=seq, prev_hash=previous_hash, tenant_id=tenant_id, event=safe
    )

    await session.execute(
        insert(AuditEvent).values(
            chain_id=chain_id,
            seq=seq,
            prev_hash=previous_hash,
            hash=digest,
            tenant_id=tenant_id,
            occurred_at=safe.occurred_at,
            actor_kind=safe.actor_kind.value,
            actor_id=safe.actor_id,
            action=safe.action.value,
            subject_kind=safe.subject_kind.value,
            subject_id=safe.subject_id,
            outcome=safe.outcome.value,
            reason=safe.reason,
            payload=safe.payload,
        )
    )
    return AuditRecord(
        chain_id=str(chain_id),
        seq=seq,
        prev_hash=previous_hash,
        hash=digest,
        event=safe,
        redactions=redactions,
    )


async def read_chain(session: AsyncSession, chain_id: uuid.UUID) -> list[AuditEvent]:
    """Every event in one chain, oldest first. Visible only to its own scope."""
    return list(
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.chain_id == chain_id).order_by(AuditEvent.seq)
            )
        )
        .scalars()
        .all()
    )


async def verify_chain(session: AsyncSession, chain_id: uuid.UUID) -> ChainVerification:
    """Walk a chain and confirm it has not been altered.

    Checks three things at every step: the sequence has no gaps, each event's
    ``prev_hash`` is the previous event's ``hash``, and each event's ``hash`` is
    what its content produces. Reports the first sequence number that fails, so a
    verification failure says where to look.
    """
    rows = await read_chain(session, chain_id)
    expected_prev = GENESIS_HASH

    for index, row in enumerate(rows, start=1):
        if row.seq != index:
            return ChainVerification(
                chain_id=chain_id,
                length=len(rows),
                valid=False,
                broken_at=row.seq,
                detail=f"expected sequence {index}, found {row.seq}: an event is missing",
            )
        if row.prev_hash != expected_prev:
            return ChainVerification(
                chain_id=chain_id,
                length=len(rows),
                valid=False,
                broken_at=row.seq,
                detail="prev_hash does not match the preceding event",
            )
        recomputed = compute_hash(
            chain_id=row.chain_id,
            seq=row.seq,
            prev_hash=row.prev_hash,
            tenant_id=row.tenant_id,
            # Strict mode wants the enum members, not their stored values; the
            # conversion also rejects a row whose action or outcome is not one ARIA
            # writes, which is itself worth hearing about.
            event=AuditEventDraft(
                action=AuditAction(row.action),
                actor_kind=ActorKind(row.actor_kind),
                actor_id=row.actor_id,
                subject_kind=SubjectKind(row.subject_kind),
                subject_id=row.subject_id,
                outcome=AuditOutcome(row.outcome),
                reason=row.reason,
                payload=row.payload,
                occurred_at=row.occurred_at,
            ),
        )
        if recomputed != row.hash:
            return ChainVerification(
                chain_id=chain_id,
                length=len(rows),
                valid=False,
                broken_at=row.seq,
                detail="the stored hash does not match the event's content: it was altered",
            )
        expected_prev = row.hash

    return ChainVerification(chain_id=chain_id, length=len(rows), valid=True)
