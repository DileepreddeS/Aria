"""Facts become true only when the candidate says so (RESUME_ENGINE §5)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.audit.log import read_chain
from aria_core.db.models import Candidate, Experience, Fact, FactMetric, FactSkill
from aria_core.db.session import tenant_transaction
from aria_core.graph.facts import (
    ALLOWED_ACTIONS,
    FactNotFound,
    IllegalFactTransition,
    approve_fact,
    approved_facts,
    create_draft_fact,
    draft_facts,
    edit_fact,
    get_fact,
    reconsider_fact,
    reject_fact,
    rejected_facts,
    revision_history,
)
from aria_core.graph.status import ApprovalStatus, ExperienceKind, GraphSource
from aria_core.schemas.audit import AuditAction
from aria_core.schemas.identity import TenantId, UserId, new_id

pytestmark = pytest.mark.db

BULLET = "Built 20+ React components used by 4 teams"


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
    return created


async def _draft(
    engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, text: str = BULLET, **kwargs: object
) -> uuid.UUID:
    async with tenant_transaction(engine, tenant_a) as session:
        return await create_draft_fact(
            session,
            tenant_id=tenant_a,
            candidate_id=candidate_id,
            text=text,
            source=GraphSource.RESUME_IMPORT,
            **kwargs,  # type: ignore[arg-type]
        )


class TestImportProducesDraftsOnly:
    async def test_a_new_fact_is_a_draft(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            fact = await get_fact(session, fact_id)
        assert fact.status is ApprovalStatus.DRAFT
        assert fact.approved_at is None
        assert fact.version == 1

    async def test_a_draft_is_invisible_to_the_engines(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        # The whole point: an imported bullet is not yet something ARIA will say.
        await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            assert await approved_facts(session, candidate_id) == []
            assert len(await draft_facts(session, candidate_id)) == 1

    async def test_there_is_no_way_to_create_an_approved_fact_directly(self) -> None:
        # Approval is a separate, audited act by a person. If create_draft_fact grew
        # an `approve=True`, an importer could verify its own guesses.
        import inspect

        signature = inspect.signature(create_draft_fact)
        assert "status" not in signature.parameters
        assert "approve" not in signature.parameters
        assert "approved_by" not in signature.parameters

    async def test_provenance_travels_with_the_draft(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        upload_id = new_id()
        fact_id = await _draft(engine, tenant_a, candidate_id, source_line=42)
        async with tenant_transaction(engine, tenant_a) as session:
            fact = await get_fact(session, fact_id)
        assert fact.source_line == 42
        assert fact.source is GraphSource.RESUME_IMPORT
        assert upload_id is not None  # the upload link is covered in the import tests

    async def test_edges_are_written_with_the_fact(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        fact_id = await _draft(
            engine,
            tenant_a,
            candidate_id,
            skills=[("react", "React"), ("typescript", "TypeScript")],
            themes=["scale"],
            metrics=[("20+", "components built", None), ("4", "teams using them", None)],
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = (
                (await session.execute(select(FactSkill.skill_canonical).where(FactSkill.fact_id == fact_id)))
                .scalars()
                .all()
            )
            metrics = (
                (await session.execute(select(FactMetric.exact_value).where(FactMetric.fact_id == fact_id)))
                .scalars()
                .all()
            )
        assert sorted(skills) == ["react", "typescript"]
        assert sorted(metrics) == ["20+", "4"]


class TestApproval:
    async def test_approving_makes_a_fact_usable(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)

        async with tenant_transaction(engine, tenant_a) as session:
            approved = await approved_facts(session, candidate_id)
            assert [fact.text for fact in approved] == [BULLET]
            assert approved[0].approved_at is not None
            assert await draft_facts(session, candidate_id) == []

    async def test_approval_records_who(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)
            stored = await session.scalar(select(Fact.approved_by).where(Fact.id == fact_id))
        assert stored == user_a

    async def test_approving_twice_is_refused(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)
        with pytest.raises(IllegalFactTransition, match="cannot approve a fact that is approved"):
            async with tenant_transaction(engine, tenant_a) as session:
                await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)

    async def test_every_decision_is_audited(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)

        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)

        assert events[-1].action == AuditAction.FACT_APPROVED.value
        assert events[-1].subject_id == str(fact_id)
        assert events[-1].actor_id == str(user_a)
        assert events[-1].payload["from"] == "draft"


class TestEditingAppendsRevisions:
    async def test_an_edit_creates_a_new_version_and_keeps_the_old_text(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            version = await edit_fact(
                session,
                tenant_id=tenant_a,
                fact_id=fact_id,
                text="Built 23 React components used by 4 teams",
                edited_by=user_a,
            )
        assert version == 2

        async with tenant_transaction(engine, tenant_a) as session:
            history = await revision_history(session, fact_id)
            current = await get_fact(session, fact_id)

        # Both versions survive: a resume built from version 1 still explains
        # (RESUME_ENGINE §1.10).
        assert history == [(1, BULLET), (2, "Built 23 React components used by 4 teams")]
        assert current.version == 2
        assert current.text == "Built 23 React components used by 4 teams"

    async def test_an_edit_is_approved_because_the_candidate_wrote_it(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await edit_fact(
                session, tenant_id=tenant_a, fact_id=fact_id, text="Rewritten by me", edited_by=user_a
            )
            fact = await get_fact(session, fact_id)
        assert fact.status is ApprovalStatus.APPROVED
        assert fact.source is GraphSource.USER_EDIT

    async def test_an_edit_can_stay_a_draft(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await edit_fact(
                session,
                tenant_id=tenant_a,
                fact_id=fact_id,
                text="Half-written, come back to it",
                edited_by=user_a,
                approve=False,
            )
            fact = await get_fact(session, fact_id)
        assert fact.status is ApprovalStatus.DRAFT

    async def test_editing_an_approved_fact_supersedes_the_text_not_the_approval(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            await edit_fact(
                session, tenant_id=tenant_a, fact_id=fact_id, text="Corrected wording", edited_by=user_a
            )
            approved = await approved_facts(session, candidate_id)
        assert [fact.text for fact in approved] == ["Corrected wording"]

    async def test_blank_text_is_refused_with_advice(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        with pytest.raises(ValueError, match="reject it instead"):
            async with tenant_transaction(engine, tenant_a) as session:
                await edit_fact(session, tenant_id=tenant_a, fact_id=fact_id, text="   ", edited_by=user_a)

    async def test_an_edit_is_audited_with_both_versions(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await edit_fact(session, tenant_id=tenant_a, fact_id=fact_id, text="Reworded", edited_by=user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)
        assert events[-1].action == AuditAction.FACT_EDITED.value
        assert events[-1].payload["from_version"] == 1
        assert events[-1].payload["to_version"] == 2


class TestRejection:
    async def test_a_rejected_fact_is_kept_but_unusable(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        # Kept so a second import does not propose the same wrong bullet again.
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await reject_fact(
                session,
                tenant_id=tenant_a,
                fact_id=fact_id,
                rejected_by=user_a,
                reason="that was a different project",
            )
        async with tenant_transaction(engine, tenant_a) as session:
            assert await approved_facts(session, candidate_id) == []
            assert await draft_facts(session, candidate_id) == []
            assert len(await rejected_facts(session, candidate_id)) == 1

    async def test_an_approved_fact_can_be_rejected_later(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            await reject_fact(session, tenant_id=tenant_a, fact_id=fact_id, rejected_by=user_a)
            fact = await get_fact(session, fact_id)
        assert fact.status is ApprovalStatus.REJECTED
        assert fact.approved_at is None

    async def test_a_rejected_fact_can_be_reconsidered(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await reject_fact(session, tenant_id=tenant_a, fact_id=fact_id, rejected_by=user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            await reconsider_fact(session, tenant_id=tenant_a, fact_id=fact_id, actor=user_a)
            fact = await get_fact(session, fact_id)
        assert fact.status is ApprovalStatus.DRAFT

    async def test_a_rejected_fact_cannot_be_approved_without_reconsidering(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await reject_fact(session, tenant_id=tenant_a, fact_id=fact_id, rejected_by=user_a)
        with pytest.raises(IllegalFactTransition):
            async with tenant_transaction(engine, tenant_a) as session:
                await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)


class TestTheLifecycleTable:
    def test_every_status_appears(self) -> None:
        assert set(ALLOWED_ACTIONS) == set(ApprovalStatus)

    def test_a_draft_can_be_approved_edited_or_rejected(self) -> None:
        assert ALLOWED_ACTIONS[ApprovalStatus.DRAFT] == {"approve", "edit", "reject"}

    def test_an_approved_fact_cannot_be_approved_again(self) -> None:
        assert "approve" not in ALLOWED_ACTIONS[ApprovalStatus.APPROVED]

    def test_a_rejected_fact_only_comes_back_through_reconsider(self) -> None:
        assert ALLOWED_ACTIONS[ApprovalStatus.REJECTED] == {"reconsider"}


class TestScopeAndErrors:
    async def test_an_unknown_fact_is_not_silently_ignored(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        with pytest.raises(FactNotFound):
            async with tenant_transaction(engine, tenant_a) as session:
                await approve_fact(session, tenant_id=tenant_a, fact_id=new_id(), approved_by=user_a)

    async def test_another_tenants_fact_is_not_found(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        tenant_b: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        with pytest.raises(FactNotFound):
            async with tenant_transaction(engine, tenant_b) as session:
                await approve_fact(session, tenant_id=tenant_b, fact_id=fact_id, approved_by=user_a)

    async def test_facts_are_returned_in_a_stable_order(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        # The approval CLI walks this list; a shifting order would mean "fact 3"
        # meant something different between two runs.
        for index in range(5):
            await _draft(engine, tenant_a, candidate_id, text=f"Bullet number {index}")
        async with tenant_transaction(engine, tenant_a) as session:
            first = [fact.fact_id for fact in await draft_facts(session, candidate_id)]
            second = [fact.fact_id for fact in await draft_facts(session, candidate_id)]
        assert first == second
        assert len(first) == 5


class TestAnchors:
    async def test_a_fact_knows_what_it_is_attached_to(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        import datetime as dt

        experience_id = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Experience).values(
                    id=experience_id,
                    tenant_id=tenant_a,
                    candidate_id=candidate_id,
                    employer="Accenture",
                    title="Software Engineer",
                    kind=ExperienceKind.PROFESSIONAL.value,
                    start_month=dt.date(2024, 6, 1),
                    status=ApprovalStatus.DRAFT.value,
                    source=GraphSource.RESUME_IMPORT.value,
                )
            )
        fact_id = await _draft(engine, tenant_a, candidate_id, experience_id=experience_id)

        async with tenant_transaction(engine, tenant_a) as session:
            fact = await get_fact(session, fact_id)
        assert fact.experience_id == experience_id
        assert fact.anchor_id == experience_id

    async def test_a_standalone_fact_has_no_anchor(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        fact_id = await _draft(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            assert (await get_fact(session, fact_id)).anchor_id is None
