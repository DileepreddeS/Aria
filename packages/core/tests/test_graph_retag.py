"""Re-tagging skill edges with the current taxonomy, against a real database (DECISIONS 2026-10-17).

The tags are stored as taxonomy 2026.10.2 made them: RabbitMQ filed as message queues,
the word "checkout" as payments, FAISS as vector databases. Re-tagging must bring the
edges in line with 2026.10.3, write exactly what it previewed, and leave every fact's
text, version and approval, and every declared skill's approval, as they were.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import AuditEvent, Candidate, DeclaredSkill, Fact, FactRevision, FactSkill
from aria_core.db.session import tenant_transaction
from aria_core.graph.facts import approve_fact, create_draft_fact
from aria_core.graph.retag import StaleRetag, apply_retag, plan_retag
from aria_core.graph.status import ApprovalStatus, GraphSource
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.taxonomy.loader import load_default_taxonomy

pytestmark = pytest.mark.db

RABBIT = "Wired RabbitMQ async messaging between 5 services for the checkout platform"
#: An enrichment answer about Kafka that never says the word: its stored tag stands.
KAFKA = "Used it for the event pipeline behind the order history page"


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
    return created


async def _old_graph(
    engine: AsyncEngine, tenant: TenantId, user: UserId, candidate: uuid.UUID
) -> dict[str, uuid.UUID]:
    """The graph as 2026.10.2 tagged it: one approved fact, one draft, three declared skills."""
    async with tenant_transaction(engine, tenant) as session:
        rabbit = await create_draft_fact(
            session,
            tenant_id=tenant,
            candidate_id=candidate,
            text=RABBIT,
            source=GraphSource.RESUME_IMPORT,
            skills=[("message_queue", "RabbitMQ"), ("payments", "checkout")],
        )
        await approve_fact(session, tenant_id=tenant, fact_id=rabbit, approved_by=user)
        kafka = await create_draft_fact(
            session,
            tenant_id=tenant,
            candidate_id=candidate,
            text=KAFKA,
            source=GraphSource.ENRICHMENT_INTERVIEW,
            skills=[("kafka", "Kafka")],
        )
        ids = {"rabbit": rabbit, "kafka": kafka}
        for canonical, surface, status in (
            ("vector_database", "FAISS", ApprovalStatus.APPROVED),
            ("payments", "checkout", ApprovalStatus.APPROVED),
            ("rabbitmq", "RabbitMQ", ApprovalStatus.APPROVED),
            ("message_queue", "rabbitmq", ApprovalStatus.DRAFT),
        ):
            declared = new_id()
            await session.execute(
                insert(DeclaredSkill).values(
                    id=declared,
                    tenant_id=tenant,
                    candidate_id=candidate,
                    skill_canonical=canonical,
                    surface_form=surface,
                    status=status.value,
                    source=GraphSource.RESUME_IMPORT.value,
                    approved_at=None if status is ApprovalStatus.DRAFT else dt.datetime.now(dt.UTC),
                    approved_by=None if status is ApprovalStatus.DRAFT else user,
                )
            )
            ids[f"declared {surface} {canonical}"] = declared
    return ids


async def _edges(engine: AsyncEngine, tenant: TenantId, fact_id: uuid.UUID) -> set[tuple[str, str]]:
    async with tenant_transaction(engine, tenant) as session:
        rows = (
            await session.execute(
                select(FactSkill.skill_canonical, FactSkill.surface_form).where(FactSkill.fact_id == fact_id)
            )
        ).all()
    return {(row[0], row[1]) for row in rows}


async def test_the_preview_shows_every_change_and_writes_nothing(
    engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
) -> None:
    ids = await _old_graph(engine, tenant_a, user_a, candidate_id)
    async with tenant_transaction(engine, tenant_a) as session:
        plan = await plan_retag(session, candidate_id=candidate_id, taxonomy=load_default_taxonomy())
    (fact,) = plan.facts
    assert fact.fact_id == ids["rabbit"]
    # Re-pointed by its spelling, and the misfire word dropped.
    assert fact.removed == (("message_queue", "RabbitMQ"), ("payments", "checkout"))
    assert fact.added == (("rabbitmq", "RabbitMQ"),)
    declared = {entry.surface_form: entry for entry in plan.declared}
    assert (declared["FAISS"].new, declared["FAISS"].left_because) == ("faiss", None)
    assert declared["checkout"].left_because == "its spelling names no skill now"
    assert declared["rabbitmq"].left_because == "rabbitmq is already declared"
    assert await _edges(engine, tenant_a, ids["rabbit"]) == {
        ("message_queue", "RabbitMQ"),
        ("payments", "checkout"),
    }


async def test_applying_changes_edges_and_nothing_else(
    engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
) -> None:
    ids = await _old_graph(engine, tenant_a, user_a, candidate_id)
    taxonomy = load_default_taxonomy()
    async with tenant_transaction(engine, tenant_a) as session:
        before = (
            (await session.execute(select(Fact).where(Fact.candidate_id == candidate_id))).scalars().all()
        )
        snapshot = {fact.id: (fact.status, fact.current_version, fact.approved_at) for fact in before}
        texts = dict((await session.execute(select(FactRevision.fact_id, FactRevision.text))).all())
        plan = await plan_retag(session, candidate_id=candidate_id, taxonomy=taxonomy)
    async with tenant_transaction(engine, tenant_a) as session:
        await apply_retag(
            session,
            tenant_id=tenant_a,
            candidate_id=candidate_id,
            actor_id=str(user_a),
            taxonomy=taxonomy,
            fingerprint=plan.fingerprint,
        )

    assert await _edges(engine, tenant_a, ids["rabbit"]) == {("rabbitmq", "RabbitMQ")}
    # The enrichment answer never spells Kafka; its tag still names a skill, so it stays.
    assert await _edges(engine, tenant_a, ids["kafka"]) == {("kafka", "Kafka")}
    async with tenant_transaction(engine, tenant_a) as session:
        after = (await session.execute(select(Fact).where(Fact.candidate_id == candidate_id))).scalars().all()
        assert {fact.id: (fact.status, fact.current_version, fact.approved_at) for fact in after} == snapshot
        assert dict((await session.execute(select(FactRevision.fact_id, FactRevision.text))).all()) == texts
        declared = {
            row.id: (row.skill_canonical, row.status)
            for row in (await session.execute(select(DeclaredSkill))).scalars().all()
        }
        assert declared[ids["declared FAISS vector_database"]] == ("faiss", "approved")
        # Left as they were, approval and all.
        assert declared[ids["declared checkout payments"]] == ("payments", "approved")
        assert declared[ids["declared rabbitmq message_queue"]] == ("message_queue", "draft")
        (event,) = (
            (await session.execute(select(AuditEvent).where(AuditEvent.action == "graph.retagged")))
            .scalars()
            .all()
        )
        assert event.payload["fingerprint"] == plan.fingerprint
        assert event.payload["edges_removed"] == 2 and event.payload["edges_added"] == 1
    async with tenant_transaction(engine, tenant_a) as session:
        again = await plan_retag(session, candidate_id=candidate_id, taxonomy=taxonomy)
    assert again.facts == () and all(entry.left_because for entry in again.declared)


async def test_a_change_after_the_preview_is_refused_and_nothing_is_written(
    engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
) -> None:
    ids = await _old_graph(engine, tenant_a, user_a, candidate_id)
    taxonomy = load_default_taxonomy()
    async with tenant_transaction(engine, tenant_a) as session:
        plan = await plan_retag(session, candidate_id=candidate_id, taxonomy=taxonomy)
    # Another tag lands between what the candidate saw and the write.
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(
            update(FactSkill).where(FactSkill.fact_id == ids["kafka"]).values(surface_form="checkout")
        )
    with pytest.raises(StaleRetag):
        async with tenant_transaction(engine, tenant_a) as session:
            await apply_retag(
                session,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                actor_id=str(user_a),
                taxonomy=taxonomy,
                fingerprint=plan.fingerprint,
            )
    assert await _edges(engine, tenant_a, ids["rabbit"]) == {
        ("message_queue", "RabbitMQ"),
        ("payments", "checkout"),
    }
