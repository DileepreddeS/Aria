"""Anchors catch the rewrite the chain cannot (SECURITY.md §16).

The test that matters here is the consistent rewrite: an attacker with database
access edits an event and recomputes every hash after it, so ``verify_chain``
passes. Only something recorded outside the database notices.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.audit.anchor import (
    AnchorRecord,
    FileAnchorStore,
    record_all_heads,
    record_chain_head,
    verify_against_anchors,
)
from aria_core.audit.log import append_event, compute_hash, read_chain, verify_chain
from aria_core.db.session import tenant_transaction
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    SubjectKind,
)
from aria_core.schemas.identity import TenantId

pytestmark = pytest.mark.db


@pytest.fixture
def store(tmp_path: Path) -> FileAnchorStore:
    return FileAnchorStore(tmp_path / "audit-anchors.jsonl")


def _draft(reason: str) -> AuditEventDraft:
    return AuditEventDraft(
        action=AuditAction.APPLICATION_STATE_CHANGED,
        actor_kind=ActorKind.SYSTEM,
        actor_id="test",
        subject_kind=SubjectKind.APPLICATION,
        subject_id=str(uuid.uuid4()),
        outcome=AuditOutcome.OK,
        reason=reason,
    )


async def _write_events(engine: AsyncEngine, tenant_id: TenantId, reasons: list[str]) -> None:
    for reason in reasons:
        async with tenant_transaction(engine, tenant_id) as session:
            await append_event(session, _draft(reason), tenant_id=tenant_id)


async def _rewrite_chain_consistently(
    migrate_engine: AsyncEngine, engine: AsyncEngine, tenant_id: TenantId, *, seq: int, reason: str
) -> None:
    """Edit one event and re-hash the whole chain after it, as an attacker would.

    The result is a chain that verifies: every prev_hash lines up and every hash
    matches its content. It is simply not what happened.
    """
    async with tenant_transaction(engine, tenant_id) as session:
        rows = await read_chain(session, tenant_id)

    previous_hash = next(row.prev_hash for row in rows if row.seq == seq)
    async with migrate_engine.begin() as connection:
        for row in rows:
            if row.seq < seq:
                continue
            new_reason = reason if row.seq == seq else row.reason
            new_hash = compute_hash(
                chain_id=row.chain_id,
                seq=row.seq,
                prev_hash=previous_hash,
                tenant_id=row.tenant_id,
                event=AuditEventDraft(
                    action=AuditAction(row.action),
                    actor_kind=ActorKind(row.actor_kind),
                    actor_id=row.actor_id,
                    subject_kind=SubjectKind(row.subject_kind),
                    subject_id=row.subject_id,
                    outcome=AuditOutcome(row.outcome),
                    reason=new_reason,
                    payload=row.payload,
                    occurred_at=row.occurred_at,
                ),
            )
            await connection.execute(
                text(
                    "UPDATE audit_events SET reason = :reason, prev_hash = :prev, hash = :hash WHERE id = :id"
                ),
                {"reason": new_reason, "prev": previous_hash, "hash": new_hash, "id": row.id},
            )
            previous_hash = new_hash


class TestRecordingAnchors:
    async def test_the_head_is_recorded(
        self, engine: AsyncEngine, tenant_a: TenantId, store: FileAnchorStore
    ) -> None:
        await _write_events(engine, tenant_a, ["one", "two", "three"])

        async with tenant_transaction(engine, tenant_a) as session:
            record = await record_chain_head(session, store, chain_id=tenant_a)
            rows = await read_chain(session, tenant_a)

        assert record is not None
        assert record.seq == 3
        assert record.head_hash == rows[-1].hash

    async def test_an_empty_chain_records_nothing(
        self, engine: AsyncEngine, tenant_a: TenantId, store: FileAnchorStore
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            assert await record_chain_head(session, store, chain_id=tenant_a) is None

    async def test_an_anchor_carries_no_event_content(
        self, engine: AsyncEngine, tenant_a: TenantId, store: FileAnchorStore
    ) -> None:
        await _write_events(engine, tenant_a, ["a reason that must not be copied into the anchor"])
        async with tenant_transaction(engine, tenant_a) as session:
            await record_chain_head(session, store, chain_id=tenant_a)

        # Anchors are kept outside the database, so they must hold nothing worth
        # stealing: a position and a hash, and no event content.
        assert "must not be copied" not in store.path.read_text(encoding="utf-8")

    async def test_the_store_only_ever_appends(
        self, engine: AsyncEngine, tenant_a: TenantId, store: FileAnchorStore
    ) -> None:
        await _write_events(engine, tenant_a, ["one"])
        async with tenant_transaction(engine, tenant_a) as session:
            await record_chain_head(session, store, chain_id=tenant_a)
        await _write_events(engine, tenant_a, ["two"])
        async with tenant_transaction(engine, tenant_a) as session:
            await record_chain_head(session, store, chain_id=tenant_a)

        lines = store.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert [record.seq for record in store.records_for(tenant_a)] == [1, 2]

    async def test_records_are_separated_by_chain(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId, store: FileAnchorStore
    ) -> None:
        await _write_events(engine, tenant_a, ["a"])
        await _write_events(engine, tenant_b, ["b1", "b2"])
        for tenant_id in (tenant_a, tenant_b):
            async with tenant_transaction(engine, tenant_id) as session:
                await record_chain_head(session, store, chain_id=tenant_id)

        assert [record.seq for record in store.records_for(tenant_a)] == [1]
        assert [record.seq for record in store.records_for(tenant_b)] == [2]

    async def test_every_chain_can_be_anchored_in_one_pass(
        self, engine: AsyncEngine, tenant_a: TenantId, store: FileAnchorStore
    ) -> None:
        await _write_events(engine, tenant_a, ["one"])
        records = await record_all_heads(engine, store)

        anchored = {record.chain_id for record in records}
        assert str(tenant_a) in anchored


class TestVerifyingAgainstAnchors:
    async def test_an_untouched_chain_passes(
        self, engine: AsyncEngine, tenant_a: TenantId, store: FileAnchorStore
    ) -> None:
        await _write_events(engine, tenant_a, ["one", "two"])
        async with tenant_transaction(engine, tenant_a) as session:
            await record_chain_head(session, store, chain_id=tenant_a)
            await _write_events(engine, tenant_a, [])

        async with tenant_transaction(engine, tenant_a) as session:
            result = await verify_against_anchors(session, store, chain_id=tenant_a)

        assert result.valid is True
        assert result.anchors_checked == 1

    async def test_growing_the_chain_after_an_anchor_is_fine(
        self, engine: AsyncEngine, tenant_a: TenantId, store: FileAnchorStore
    ) -> None:
        await _write_events(engine, tenant_a, ["one"])
        async with tenant_transaction(engine, tenant_a) as session:
            await record_chain_head(session, store, chain_id=tenant_a)
        await _write_events(engine, tenant_a, ["two", "three"])

        async with tenant_transaction(engine, tenant_a) as session:
            assert (await verify_against_anchors(session, store, chain_id=tenant_a)).valid is True

    async def test_a_consistently_rewritten_chain_passes_its_own_check_but_fails_the_anchor(
        self,
        engine: AsyncEngine,
        migrate_engine: AsyncEngine,
        tenant_a: TenantId,
        store: FileAnchorStore,
    ) -> None:
        await _write_events(engine, tenant_a, ["submitted to acme", "rejected by acme", "archived"])
        async with tenant_transaction(engine, tenant_a) as session:
            anchor = await record_chain_head(session, store, chain_id=tenant_a)
        assert anchor is not None

        await _rewrite_chain_consistently(
            migrate_engine, engine, tenant_a, seq=1, reason="never submitted to acme"
        )

        async with tenant_transaction(engine, tenant_a) as session:
            internal = await verify_chain(session, tenant_a)
            against_anchor = await verify_against_anchors(session, store, chain_id=tenant_a)

        # The chain is self-consistent: this is exactly the attack the hash chain
        # alone cannot see.
        assert internal.valid is True
        assert against_anchor.valid is False
        assert against_anchor.broken_at == anchor.seq
        assert "rewritten" in against_anchor.detail

    async def test_a_truncated_chain_fails_the_anchor(
        self,
        engine: AsyncEngine,
        migrate_engine: AsyncEngine,
        tenant_a: TenantId,
        store: FileAnchorStore,
    ) -> None:
        await _write_events(engine, tenant_a, ["one", "two", "three"])
        async with tenant_transaction(engine, tenant_a) as session:
            await record_chain_head(session, store, chain_id=tenant_a)

        async with migrate_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM audit_events WHERE chain_id = :chain AND seq = 3"),
                {"chain": str(tenant_a)},
            )

        async with tenant_transaction(engine, tenant_a) as session:
            result = await verify_against_anchors(session, store, chain_id=tenant_a)

        assert result.valid is False
        assert result.broken_at == 3
        assert "removed" in result.detail

    async def test_a_chain_with_no_anchors_says_so_rather_than_claiming_safety(
        self, engine: AsyncEngine, tenant_a: TenantId, store: FileAnchorStore
    ) -> None:
        await _write_events(engine, tenant_a, ["one"])
        async with tenant_transaction(engine, tenant_a) as session:
            result = await verify_against_anchors(session, store, chain_id=tenant_a)

        assert result.anchors_checked == 0
        assert "nothing has been anchored" in result.detail

    async def test_several_anchors_are_all_checked(
        self,
        engine: AsyncEngine,
        migrate_engine: AsyncEngine,
        tenant_a: TenantId,
        store: FileAnchorStore,
    ) -> None:
        for reason in ("one", "two", "three"):
            await _write_events(engine, tenant_a, [reason])
            async with tenant_transaction(engine, tenant_a) as session:
                await record_chain_head(session, store, chain_id=tenant_a)

        # Rewriting the middle of the chain is caught by the second anchor even
        # though the first and third still match.
        await _rewrite_chain_consistently(migrate_engine, engine, tenant_a, seq=2, reason="edited")

        async with tenant_transaction(engine, tenant_a) as session:
            result = await verify_against_anchors(session, store, chain_id=tenant_a)

        assert result.anchors_checked == 3
        assert result.valid is False
        assert result.broken_at == 2


class TestStoreMechanics:
    def test_records_for_an_unwritten_store_is_empty(self, tmp_path: Path) -> None:
        assert FileAnchorStore(tmp_path / "missing.jsonl").records_for(uuid.uuid4()) == []

    def test_a_record_round_trips(self, store: FileAnchorStore) -> None:
        chain_id = uuid.uuid4()
        record = AnchorRecord.from_head(chain_id, 7, "a" * 64)
        store.append(record)
        assert store.records_for(chain_id) == [record]
