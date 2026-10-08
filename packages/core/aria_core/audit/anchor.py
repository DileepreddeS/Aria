"""External anchors for audit chain heads (SECURITY.md §16).

The hash chain detects an *inconsistent* edit. It cannot detect a consistent one.
Someone with write access to the database can change an event and recompute every
hash after it, and :func:`aria_core.audit.log.verify_chain` will happily call the
result valid — the chain is self-consistent, just not the history that happened.

The fix is to put something outside the database that the attacker would also have
to rewrite. Periodically — daily is the intended cadence — the current head of each
chain, its sequence number and its hash, is appended to a store that is not the
database. Verification then asks a question the database cannot answer on its own:
*is the event at sequence 412 still the one we saw at sequence 412 yesterday?*

Phase 0 ships :class:`FileAnchorStore`: append-only JSON Lines in a file outside
the repository and outside the database. That is enough to make a silent rewrite
require two separate kinds of access. It is deliberately not enough for production,
which needs a store the application cannot rewrite at all — object storage with
versioning and an object-lock/WORM retention policy, or a managed append-only log,
chosen with the cloud. Until then, the honest statement is: anchors raise the cost
of a silent rewrite; they do not yet make it impossible.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from aria_core.db.models import AuditEvent, Tenant
from aria_core.db.session import platform_transaction, tenant_transaction
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import PLATFORM_CHAIN_ID, TenantId

__all__ = [
    "AnchorRecord",
    "AnchorStore",
    "AnchorVerification",
    "FileAnchorStore",
    "record_all_heads",
    "record_chain_head",
    "verify_against_anchors",
]


@dataclass(frozen=True, slots=True)
class AnchorRecord:
    """One observation: this chain's head was this, at this time.

    Holds no event content — only where the chain had reached and the hash that
    summarised it. An anchor store is therefore safe to keep somewhere the main
    database is not, which is the entire point of it.
    """

    chain_id: str
    seq: int
    head_hash: str
    recorded_at: str

    @classmethod
    def from_head(cls, chain_id: uuid.UUID, seq: int, head_hash: str) -> AnchorRecord:
        return cls(chain_id=str(chain_id), seq=seq, head_hash=head_hash, recorded_at=utc_now().isoformat())


@dataclass(frozen=True, slots=True)
class AnchorVerification:
    chain_id: uuid.UUID
    anchors_checked: int
    valid: bool
    broken_at: int | None = None
    detail: str = ""


class AnchorStore(Protocol):
    """Append-only storage for anchors. Implementations never rewrite a record."""

    def append(self, record: AnchorRecord) -> None: ...

    def records_for(self, chain_id: uuid.UUID) -> list[AnchorRecord]: ...


class FileAnchorStore:
    """JSON Lines, opened for append only. Development and single-machine use.

    One record per line, never edited in place. Kept outside the repository and
    outside the database — by default next to the other local secrets in
    ``%APPDATA%\\aria`` — so a database compromise alone cannot bring it along.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: AnchorRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # "a" rather than "w" or "r+": the only supported operation is adding a line.
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")

    def records_for(self, chain_id: uuid.UUID) -> list[AnchorRecord]:
        if not self._path.exists():
            return []
        records: list[AnchorRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("chain_id") == str(chain_id):
                records.append(AnchorRecord(**payload))
        return sorted(records, key=lambda record: record.seq)


async def record_chain_head(
    session: AsyncSession, store: AnchorStore, *, chain_id: uuid.UUID
) -> AnchorRecord | None:
    """Append the chain's current head to the store. ``None`` if the chain is empty.

    Recording the same head twice is harmless: an anchor is an observation, and two
    observations of an unchanged chain agree.
    """
    row = (
        await session.execute(
            select(AuditEvent.seq, AuditEvent.hash)
            .where(AuditEvent.chain_id == chain_id)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None

    record = AnchorRecord.from_head(chain_id, row[0], row[1])
    store.append(record)
    return record


async def record_all_heads(engine: AsyncEngine, store: AnchorStore) -> list[AnchorRecord]:
    """Anchor every chain: the platform chain and one per tenant.

    Intended to run daily. Each chain is read under its own scope, because that is
    the only scope its rows are visible from.
    """
    recorded: list[AnchorRecord] = []

    async with platform_transaction(engine) as session:
        platform = await record_chain_head(session, store, chain_id=PLATFORM_CHAIN_ID)
        tenant_ids = list((await session.execute(select(Tenant.id))).scalars().all())
    if platform is not None:
        recorded.append(platform)

    for tenant_id in tenant_ids:
        async with tenant_transaction(engine, TenantId(tenant_id)) as session:
            record = await record_chain_head(session, store, chain_id=tenant_id)
        if record is not None:
            recorded.append(record)

    return recorded


async def verify_against_anchors(
    session: AsyncSession, store: AnchorStore, *, chain_id: uuid.UUID
) -> AnchorVerification:
    """Check the chain against what was observed outside the database.

    Catches the rewrite that an internal check cannot: if every hash was recomputed
    so the chain verifies, the event at an anchored sequence number no longer has
    the hash that was recorded for it.
    """
    anchors = store.records_for(chain_id)
    if not anchors:
        return AnchorVerification(
            chain_id=chain_id,
            anchors_checked=0,
            valid=True,
            detail="nothing has been anchored for this chain yet, so there is nothing to compare",
        )

    for anchor in anchors:
        stored_hash = await session.scalar(
            select(AuditEvent.hash).where(AuditEvent.chain_id == chain_id, AuditEvent.seq == anchor.seq)
        )
        if stored_hash is None:
            return AnchorVerification(
                chain_id=chain_id,
                anchors_checked=len(anchors),
                valid=False,
                broken_at=anchor.seq,
                detail=(
                    f"sequence {anchor.seq} was anchored on {anchor.recorded_at} but is no longer in "
                    "the chain: events have been removed"
                ),
            )
        if stored_hash != anchor.head_hash:
            return AnchorVerification(
                chain_id=chain_id,
                anchors_checked=len(anchors),
                valid=False,
                broken_at=anchor.seq,
                detail=(
                    f"sequence {anchor.seq} no longer matches the hash anchored on "
                    f"{anchor.recorded_at}: the chain was rewritten, consistently enough to pass its "
                    "own verification"
                ),
            )

    return AnchorVerification(chain_id=chain_id, anchors_checked=len(anchors), valid=True)


def default_anchor_path(home: Path) -> Path:
    """Where the development anchor log lives: beside the other local secrets."""
    return home / "audit-anchors.jsonl"


def parse_recorded_at(record: AnchorRecord) -> dt.datetime:
    return dt.datetime.fromisoformat(record.recorded_at)
