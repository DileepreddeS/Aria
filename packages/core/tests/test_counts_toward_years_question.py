"""Whether a role counts toward years is asked, never inferred (DECISIONS 2026-10-25).

Approving a role asks; roles approved before the flag existed are asked when the candidate
next looks; the answer is a plain yes or no and sets the flag; the snapshot carries it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Experience
from aria_core.db.session import tenant_transaction
from aria_core.graph.entities import EntityKind, approve_entity
from aria_core.graph.questions import (
    UnansweredQuestion,
    answer_question,
    ask_counts_toward_years,
    open_questions,
)
from aria_core.graph.snapshot import build_candidate_snapshot
from aria_core.graph.status import ApprovalStatus, ExperienceKind, GraphSource
from aria_core.importer.plan import QuestionKind
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId, UserId, new_id

pytestmark = pytest.mark.db

KIND = "counts_toward_years"


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
    return created


async def _role(
    engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, *, approved_by: UserId | None = None
) -> uuid.UUID:
    created = new_id()
    approved = {"status": ApprovalStatus.APPROVED.value, "approved_at": utc_now(), "approved_by": approved_by}
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(
            insert(Experience).values(
                id=created,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                employer="Cascadia State University",
                title="Graduate Research Assistant",
                # The importer's guess. Nothing here may read it as an answer.
                kind=ExperienceKind.PROFESSIONAL.value,
                start_month=dt.date(2024, 1, 1),
                end_month=dt.date(2024, 12, 1),
                source=GraphSource.RESUME_IMPORT.value,
                **(approved if approved_by else {"status": ApprovalStatus.DRAFT.value}),
            )
        )
    return created


async def _years_questions(
    engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
) -> list[uuid.UUID | None]:
    async with tenant_transaction(engine, tenant_a) as session:
        return [q.subject_id for q in await open_questions(session, candidate_id) if q.kind == KIND]


async def _flag(engine: AsyncEngine, tenant_a: TenantId, role_id: uuid.UUID) -> bool | None:
    async with tenant_transaction(engine, tenant_a) as session:
        flag: bool | None = await session.scalar(
            select(Experience.counts_toward_years).where(Experience.id == role_id)
        )
    return flag


class TestAsking:
    async def test_approving_a_role_asks_and_sets_nothing(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        role_id = await _role(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            await approve_entity(
                session, tenant_id=tenant_a, kind=EntityKind.EXPERIENCE, entity_id=role_id, approved_by=user_a
            )
        assert await _years_questions(engine, tenant_a, candidate_id) == [role_id]
        assert await _flag(engine, tenant_a, role_id) is None

    async def test_a_role_approved_before_the_flag_is_asked_once(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        role_id = await _role(engine, tenant_a, candidate_id, approved_by=user_a)
        assert await _years_questions(engine, tenant_a, candidate_id) == []
        for expected in (1, 0):
            async with tenant_transaction(engine, tenant_a) as session:
                created = await ask_counts_toward_years(
                    session, tenant_id=tenant_a, candidate_id=candidate_id
                )
            assert len(created) == expected
        assert await _years_questions(engine, tenant_a, candidate_id) == [role_id]

    async def test_a_draft_role_is_not_asked(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        await _role(engine, tenant_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            assert await ask_counts_toward_years(session, tenant_id=tenant_a, candidate_id=candidate_id) == []


class TestAnswering:
    @pytest.mark.parametrize(("answer", "flag"), [("yes", True), ("No", False)])
    async def test_yes_or_no_sets_the_flag(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        answer: str,
        flag: bool,
    ) -> None:
        role_id = await _role(engine, tenant_a, candidate_id, approved_by=user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            (question_id,) = await ask_counts_toward_years(
                session, tenant_id=tenant_a, candidate_id=candidate_id
            )
            await answer_question(
                session, tenant_id=tenant_a, question_id=question_id, answer=answer, answered_by=user_a
            )
        assert await _flag(engine, tenant_a, role_id) is flag
        async with tenant_transaction(engine, tenant_a) as session:
            snapshot = await build_candidate_snapshot(session, candidate_id, now=dt.date(2026, 10, 1))
        assert [role.counts_toward_years for role in snapshot.experiences] == [flag]

    @pytest.mark.parametrize("answer", ["paid", "maybe", "part-time"])
    async def test_anything_but_yes_or_no_is_refused(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId, answer: str
    ) -> None:
        # "Paid" answers a different question: a paid assistantship may still be a role
        # the candidate does not count.
        role_id = await _role(engine, tenant_a, candidate_id, approved_by=user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            (question_id,) = await ask_counts_toward_years(
                session, tenant_id=tenant_a, candidate_id=candidate_id
            )
            with pytest.raises(UnansweredQuestion, match="counts toward"):
                await answer_question(
                    session, tenant_id=tenant_a, question_id=question_id, answer=answer, answered_by=user_a
                )
        assert await _flag(engine, tenant_a, role_id) is None


class TestTheDatabaseKnowsEveryKind:
    async def test_every_question_kind_passes_the_kind_check(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        # The CHECK is a list written into migrations; a kind added in code but not
        # there fails only when it is first asked (as this one first did).
        async with tenant_transaction(engine, tenant_a) as session:
            definition = await session.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_user_questions_kind_is_known'"
                )
            )
        assert definition is not None
        assert [kind.value for kind in QuestionKind if f"'{kind.value}'" not in definition] == []
