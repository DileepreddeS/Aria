"""The audit log is append-only and tamper-evident (SECURITY.md §16)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.audit.log import (
    GENESIS_HASH,
    append_event,
    compute_hash,
    read_chain,
    verify_chain,
)
from aria_core.db.session import platform_transaction, tenant_transaction
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    SubjectKind,
)
from aria_core.schemas.identity import PLATFORM_CHAIN_ID, TenantId

pytestmark = pytest.mark.db


def _draft(reason: str = "", **payload: object) -> AuditEventDraft:
    return AuditEventDraft(
        action=AuditAction.POLICY_DECIDED,
        actor_kind=ActorKind.SYSTEM,
        actor_id="test",
        subject_kind=SubjectKind.APPLICATION,
        subject_id=str(uuid.uuid4()),
        outcome=AuditOutcome.ALLOWED,
        reason=reason,
        payload=payload,
    )


class TestChainStructure:
    async def test_the_first_event_links_to_the_genesis_hash(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            record = await append_event(session, _draft(), tenant_id=tenant_a)

        assert record.seq == 1
        assert record.prev_hash == GENESIS_HASH
        assert len(record.hash) == 64

    async def test_each_event_links_to_the_one_before_it(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        hashes: list[str] = []
        for index in range(4):
            async with tenant_transaction(engine, tenant_a) as session:
                record = await append_event(session, _draft(step=index), tenant_id=tenant_a)
                hashes.append(record.hash)
                assert record.seq == index + 1

        async with tenant_transaction(engine, tenant_a) as session:
            rows = await read_chain(session, tenant_a)

        assert [row.seq for row in rows] == [1, 2, 3, 4]
        assert [row.prev_hash for row in rows] == [GENESIS_HASH, *hashes[:-1]]
        assert await _verify(engine, tenant_a) is True

    async def test_each_tenant_has_its_own_chain(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await append_event(session, _draft(), tenant_id=tenant_a)
        async with tenant_transaction(engine, tenant_b) as session:
            first_for_b = await append_event(session, _draft(), tenant_id=tenant_b)

        # B's chain starts at 1 regardless of what A has written.
        assert first_for_b.seq == 1
        assert first_for_b.prev_hash == GENESIS_HASH

    async def test_a_tenant_cannot_read_another_tenants_chain(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await append_event(session, _draft(secret="tenant a only"), tenant_id=tenant_a)

        async with tenant_transaction(engine, tenant_b) as session:
            assert await read_chain(session, tenant_a) == []


class TestPlatformChain:
    async def test_events_with_no_tenant_go_on_the_platform_chain(self, engine: AsyncEngine) -> None:
        async with platform_transaction(engine) as session:
            record = await append_event(
                session,
                AuditEventDraft(
                    action=AuditAction.SERVICE_STARTED,
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="llm_gateway",
                    subject_kind=SubjectKind.SERVICE,
                    subject_id="llm_gateway",
                    outcome=AuditOutcome.OK,
                ),
            )
        assert record.chain_id == str(PLATFORM_CHAIN_ID)

    async def test_a_tenant_cannot_see_the_platform_chain(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        async with platform_transaction(engine) as session:
            await append_event(
                session,
                AuditEventDraft(
                    action=AuditAction.SERVICE_STARTED,
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="api",
                    subject_kind=SubjectKind.SERVICE,
                    subject_id="api",
                    outcome=AuditOutcome.OK,
                ),
            )

        async with tenant_transaction(engine, tenant_a) as session:
            assert await read_chain(session, PLATFORM_CHAIN_ID) == []

    async def test_a_tenant_cannot_file_an_event_on_another_chain(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        # The policy ties chain_id to the scope's tenant, so this is refused by the
        # database rather than only by the writer.
        with pytest.raises(Exception, match=r"(?i)row-level security"):
            async with tenant_transaction(engine, tenant_b) as session:
                await append_event(session, _draft(), tenant_id=tenant_a)


class TestTamperEvidence:
    async def test_an_altered_event_is_detected(
        self, engine: AsyncEngine, migrate_engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await append_event(session, _draft(reason="denied: host not allowlisted"), tenant_id=tenant_a)
            await append_event(session, _draft(reason="allowed"), tenant_id=tenant_a)

        # aria_app cannot do this; only a compromised owner or direct database
        # access could. The point is that it does not go unnoticed.
        await _tamper(
            migrate_engine,
            "UPDATE audit_events SET reason = 'nothing happened' WHERE chain_id = :chain AND seq = 1",
            {"chain": str(tenant_a)},
        )

        async with tenant_transaction(engine, tenant_a) as session:
            result = await verify_chain(session, tenant_a)

        assert result.valid is False
        assert result.broken_at == 1
        assert "altered" in result.detail

    async def test_a_removed_event_is_detected(
        self, engine: AsyncEngine, migrate_engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        for _ in range(3):
            async with tenant_transaction(engine, tenant_a) as session:
                await append_event(session, _draft(), tenant_id=tenant_a)

        await _tamper(
            migrate_engine,
            "DELETE FROM audit_events WHERE chain_id = :chain AND seq = 2",
            {"chain": str(tenant_a)},
        )

        async with tenant_transaction(engine, tenant_a) as session:
            result = await verify_chain(session, tenant_a)

        assert result.valid is False
        assert result.broken_at == 3
        assert "missing" in result.detail

    async def test_a_re_linked_event_is_still_detected(
        self, engine: AsyncEngine, migrate_engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            first = await append_event(session, _draft(), tenant_id=tenant_a)
            await append_event(session, _draft(), tenant_id=tenant_a)

        # Rewrite event 2 to point at event 1 but with different content: the
        # prev_hash lines up, so only recomputing the hash catches it.
        await _tamper(
            migrate_engine,
            "UPDATE audit_events SET reason = 'edited', prev_hash = :prev "
            "WHERE chain_id = :chain AND seq = 2",
            {"prev": first.hash, "chain": str(tenant_a)},
        )

        async with tenant_transaction(engine, tenant_a) as session:
            result = await verify_chain(session, tenant_a)

        assert result.valid is False
        assert result.broken_at == 2

    async def test_an_empty_chain_verifies(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            result = await verify_chain(session, tenant_a)
        assert result.valid is True
        assert result.length == 0

    def test_the_hash_covers_the_content(self) -> None:
        chain_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        base = _draft(reason="allowed")
        first = compute_hash(
            chain_id=chain_id, seq=1, prev_hash=GENESIS_HASH, tenant_id=tenant_id, event=base
        )
        changed = compute_hash(
            chain_id=chain_id,
            seq=1,
            prev_hash=GENESIS_HASH,
            tenant_id=tenant_id,
            event=base.model_copy(update={"reason": "denied"}),
        )
        assert first != changed

    def test_the_hash_is_stable_for_the_same_event(self) -> None:
        chain_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        draft = _draft(reason="allowed", host="boards.greenhouse.io")
        arguments = {
            "chain_id": chain_id,
            "seq": 7,
            "prev_hash": GENESIS_HASH,
            "tenant_id": tenant_id,
            "event": draft,
        }
        assert compute_hash(**arguments) == compute_hash(**arguments)  # type: ignore[arg-type]


class TestConcurrentWrites:
    async def test_concurrent_writers_produce_one_unbroken_chain(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        """Two writers must not both claim the same sequence or predecessor.

        Without serialization per chain, concurrent appends read the same head and
        write a fork: two events with the same prev_hash, and no way afterwards to
        say which history is the real one.
        """
        writers = 8

        async def write(index: int) -> None:
            async with tenant_transaction(engine, tenant_a) as session:
                await append_event(session, _draft(worker=index), tenant_id=tenant_a)

        await asyncio.gather(*(write(index) for index in range(writers)))

        async with tenant_transaction(engine, tenant_a) as session:
            rows = await read_chain(session, tenant_a)
            result = await verify_chain(session, tenant_a)

        assert [row.seq for row in rows] == list(range(1, writers + 1))
        assert len({row.prev_hash for row in rows}) == writers, "two events share a predecessor"
        assert len({row.hash for row in rows}) == writers
        assert result.valid is True
        assert result.length == writers

    async def test_writers_on_different_chains_do_not_block_each_other(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        async def write(tenant_id: TenantId) -> None:
            async with tenant_transaction(engine, tenant_id) as session:
                await append_event(session, _draft(), tenant_id=tenant_id)

        await asyncio.wait_for(
            asyncio.gather(*(write(tenant) for tenant in (tenant_a, tenant_b) for _ in range(3))),
            timeout=15,
        )

        for tenant_id in (tenant_a, tenant_b):
            async with tenant_transaction(engine, tenant_id) as session:
                assert (await verify_chain(session, tenant_id)).length == 3


class TestSensitivePatternsAreRedactedNotDropped:
    async def test_an_ssn_in_a_reason_is_replaced_and_the_event_is_still_written(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            record = await append_event(
                session,
                _draft(reason="form rejected the value 123-45-6789 as invalid"),
                tenant_id=tenant_a,
            )

        assert record.redactions == ["ssn"]
        assert "123-45-6789" not in record.event.reason
        assert "[redacted:ssn]" in record.event.reason

        async with tenant_transaction(engine, tenant_a) as session:
            rows = await read_chain(session, tenant_a)
        assert len(rows) == 1
        assert "123-45-6789" not in rows[0].reason

    async def test_a_payload_string_is_scanned_too(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            record = await append_event(
                session,
                _draft(answer="I am currently on F-1 OPT", field="work_authorization"),
                tenant_id=tenant_a,
            )

        assert "self_immigration_status" in record.redactions
        assert "F-1 OPT" not in str(record.event.payload)
        assert record.event.payload["field"] == "work_authorization"

    async def test_a_redacted_event_still_verifies(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        # The hash must cover what was stored, not what the caller passed.
        async with tenant_transaction(engine, tenant_a) as session:
            await append_event(session, _draft(reason="SSN: 123-45-6789"), tenant_id=tenant_a)
        async with tenant_transaction(engine, tenant_a) as session:
            assert (await verify_chain(session, tenant_a)).valid is True


class TestPayloadDiscipline:
    def test_a_nested_payload_is_refused(self) -> None:
        with pytest.raises(ValueError, match="payloads hold scalars"):
            _draft(detail={"nested": "value"})

    def test_scalars_are_accepted(self) -> None:
        draft = _draft(count=3, ratio=0.5, ok=True, nothing=None, name="x")
        assert draft.payload["count"] == 3


async def _verify(engine: AsyncEngine, tenant_id: TenantId) -> bool:
    async with tenant_transaction(engine, tenant_id) as session:
        return (await verify_chain(session, tenant_id)).valid


async def _tamper(engine: AsyncEngine, statement: str, parameters: dict[str, str]) -> None:
    """Edit the audit log the way database-level access could.

    Deliberately not the application role: aria_app holds SELECT and INSERT only,
    which another test asserts. These tests are about what happens when someone who
    *can* rewrite a row does — the chain notices.
    """
    async with engine.begin() as connection:
        await connection.execute(text(statement), parameters)
