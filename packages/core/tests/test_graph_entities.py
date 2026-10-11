"""Approving the rest of the graph, and the gate that could not be passed without it.

The completeness report counts approved roles, education, skills and contact details.
Until this module existed nothing could approve any of them, so "founder's graph
complete" — the Phase 1 gate — was unreachable. A gate that cannot be passed teaches
you to ignore the report, which is worse than having no report at all.

The lifecycle is the same one facts use, taken from the same table rather than copied,
because "approved" has to mean the same thing everywhere: a person said this is true
about them, and their name is on it. The database enforces the name: an approved row
with no approver violates a CHECK.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.audit.log import read_chain
from aria_core.db.models import Candidate, Experience, ProfileField
from aria_core.db.session import tenant_transaction
from aria_core.graph.entities import (
    EntityKind,
    EntityNotFound,
    IllegalEntityTransition,
    approve_entity,
    entities,
    get_entity,
    reconsider_entity,
    reject_entity,
)
from aria_core.graph.facts import ALLOWED_ACTIONS
from aria_core.graph.status import ApprovalStatus, ExperienceKind, GraphSource, ProfileFieldKind
from aria_core.schemas.audit import AuditAction
from aria_core.schemas.identity import TenantId, UserId, new_id

pytestmark = pytest.mark.db


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
    return created


@pytest.fixture
async def role_id(engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(
            insert(Experience).values(
                id=created,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                employer="Accenture",
                title="Software Engineer",
                client="Carvana",
                kind=ExperienceKind.PROFESSIONAL.value,
                start_month=dt.date(2024, 6, 1),
                status=ApprovalStatus.DRAFT.value,
                source=GraphSource.RESUME_IMPORT.value,
                source_line=5,
            )
        )
    return created


class TestApproving:
    async def test_a_role_can_be_approved(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        role_id: uuid.UUID,
        user_a: UserId,
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            view = await approve_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                approved_by=user_a,
            )
        assert view.status is ApprovalStatus.APPROVED
        assert view.approved_by == user_a

    async def test_the_approver_is_recorded_on_the_row(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        role_id: uuid.UUID,
        user_a: UserId,
    ) -> None:
        # "I worked at Accenture from June 2024" is as much a truth claim as a bullet,
        # so the row carries a name for the same reason a fact does.
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                approved_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            row = (
                await session.execute(
                    select(Experience.approved_by, Experience.approved_at).where(Experience.id == role_id)
                )
            ).first()
        assert row is not None
        assert row[0] == user_a
        assert row[1] is not None

    async def test_the_database_refuses_an_approval_with_no_approver(
        self, engine: AsyncEngine, tenant_a: TenantId, role_id: uuid.UUID
    ) -> None:
        # Enforced in the database because the alternative is every future writer
        # remembering, and the first one to forget produces a claim with nobody
        # attached to it.
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    text("UPDATE experiences SET status = 'approved', approved_at = now() WHERE id = :id"),
                    {"id": role_id},
                )

    async def test_a_summary_describes_the_row(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, role_id: uuid.UUID
    ) -> None:
        # A list of ids is not something anybody can approve.
        async with tenant_transaction(engine, tenant_a) as session:
            view = await get_entity(session, EntityKind.EXPERIENCE, role_id)
        assert "Software Engineer" in view.summary
        assert "Accenture" in view.summary
        assert "Carvana" in view.summary
        assert "Jun 2024 - present" in view.summary


class TestTheLifecycleIsTheSameOneFactsUse:
    async def test_rejecting_keeps_the_row(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        role_id: uuid.UUID,
        user_a: UserId,
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            view = await reject_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                rejected_by=user_a,
                reason="wrong dates",
            )
        assert view.status is ApprovalStatus.REJECTED
        async with tenant_transaction(engine, tenant_a) as session:
            rejected = await entities(
                session, candidate_id, EntityKind.EXPERIENCE, status=ApprovalStatus.REJECTED
            )
        assert [item.entity_id for item in rejected] == [role_id]

    async def test_rejecting_clears_any_earlier_approval(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        role_id: uuid.UUID,
        user_a: UserId,
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                approved_by=user_a,
            )
            view = await reject_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                rejected_by=user_a,
            )
        assert view.approved_by is None
        assert view.approved_at is None

    async def test_a_rejected_row_can_be_reconsidered(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        role_id: uuid.UUID,
        user_a: UserId,
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await reject_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                rejected_by=user_a,
            )
            view = await reconsider_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                actor=user_a,
            )
        assert view.status is ApprovalStatus.DRAFT

    async def test_approving_a_rejected_row_is_refused(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        role_id: uuid.UUID,
        user_a: UserId,
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await reject_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                rejected_by=user_a,
            )
            with pytest.raises(IllegalEntityTransition, match="rejected"):
                await approve_entity(
                    session,
                    tenant_id=tenant_a,
                    kind=EntityKind.EXPERIENCE,
                    entity_id=role_id,
                    approved_by=user_a,
                )

    def test_it_uses_the_fact_transition_table_rather_than_a_copy(self) -> None:
        # Not a style point: "approved" meaning something different here than it does
        # for a fact is how a lifecycle becomes decoration.
        import inspect

        from aria_core.graph import entities as module

        source = inspect.getsource(module)
        assert "ALLOWED_ACTIONS" in source
        assert ALLOWED_ACTIONS[ApprovalStatus.REJECTED] == frozenset({"reconsider"})

    async def test_an_unknown_row_is_not_found(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            with pytest.raises(EntityNotFound):
                await approve_entity(
                    session,
                    tenant_id=tenant_a,
                    kind=EntityKind.EXPERIENCE,
                    entity_id=new_id(),
                    approved_by=user_a,
                )


class TestEveryKind:
    @pytest.mark.parametrize("kind", list(EntityKind), ids=lambda kind: kind.value)
    async def test_listing_works_for_each_kind(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, kind: EntityKind
    ) -> None:
        # Every kind the enum names has a model behind it and can be listed. A kind
        # with no model would be an endpoint that 500s.
        async with tenant_transaction(engine, tenant_a) as session:
            assert await entities(session, candidate_id, kind) == []

    async def test_a_contact_detail_can_be_approved(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        # The kind the completeness report needs most: it requires five of these
        # approved, and before this there was no way to approve one.
        field_id = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(ProfileField).values(
                    id=field_id,
                    tenant_id=tenant_a,
                    candidate_id=candidate_id,
                    kind=ProfileFieldKind.PHONE.value,
                    value="+1 469 555 0134",
                    status=ApprovalStatus.DRAFT.value,
                    source=GraphSource.RESUME_IMPORT.value,
                )
            )
        async with tenant_transaction(engine, tenant_a) as session:
            view = await approve_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.PROFILE_FIELD,
                entity_id=field_id,
                approved_by=user_a,
            )
        assert view.status is ApprovalStatus.APPROVED
        assert "+1 469 555 0134" in view.summary


class TestTheAuditTrail:
    async def test_approving_is_audited_with_the_kind(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        role_id: uuid.UUID,
        user_a: UserId,
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                approved_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)

        approved = [event for event in events if event.action == AuditAction.GRAPH_ENTITY_APPROVED.value]
        assert len(approved) == 1
        assert approved[0].subject_id == str(role_id)
        assert approved[0].actor_id == str(user_a)
        assert approved[0].payload["entity_kind"] == "experience"

    async def test_the_reason_for_a_rejection_is_recorded_without_its_length_lying(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        role_id: uuid.UUID,
        user_a: UserId,
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await reject_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.EXPERIENCE,
                entity_id=role_id,
                rejected_by=user_a,
                reason="those are the wrong dates",
            )
        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)
        rejected = next(event for event in events if event.action == AuditAction.GRAPH_ENTITY_REJECTED.value)
        assert rejected.reason == "those are the wrong dates"
        assert rejected.payload["reason_length"] == len("those are the wrong dates")
