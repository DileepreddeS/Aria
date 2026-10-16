"""Taking back an answer, and what withdrawing an upload does to answers.

The failure these exist for: "were you paid?" answered yes set a role to part-time; the
role was a draft of an upload later withdrawn, so it was deleted; the answered question
was not, and its key stopped the new reading asking again. The role came back as
research, nothing asked, and nothing could change it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Experience, Upload, UserQuestion
from aria_core.db.session import tenant_transaction
from aria_core.graph.entities import EntityKind, approve_entity, reject_entity
from aria_core.graph.questions import (
    QuestionStatus,
    Reconsidered,
    UnansweredQuestion,
    answer_question,
    dismiss_question,
    get_question,
    reconsider_question,
)
from aria_core.importer.apply import apply_import_plan
from aria_core.importer.resume import parse_resume_text
from aria_core.importer.withdraw import plan_withdrawal, withdraw
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.taxonomy.loader import load_default_taxonomy

pytestmark = pytest.mark.db

TODAY = dt.date(2026, 10, 15)
RESUME = """EXPERIENCE
Cascadia State University - Graduate Research Assistant
Oct 2025 - Present
- Fine-tuned open-source language models for research on agent safety

Nordvale Consulting - Software Engineer
- Reduced the nightly batch from six hours to forty minutes
"""


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
    return created


async def _import(engine: AsyncEngine, tenant: TenantId, candidate: uuid.UUID) -> uuid.UUID:
    upload_id = new_id()
    async with tenant_transaction(engine, tenant) as session:
        await session.execute(
            insert(Upload).values(
                id=upload_id,
                tenant_id=tenant,
                candidate_id=candidate,
                original_filename="resume.txt",
                detected_type="txt",
                size_bytes=len(RESUME),
                sha256=upload_id.hex * 2,
                quarantine_name=f"{upload_id}.bin",
                scan_status="not_scanned",
                scan_detail="dev scanner",
                parse_status="parsed",
                extracted_text=RESUME,
            )
        )
        plan = parse_resume_text(RESUME, load_default_taxonomy(), today=TODAY)
        await apply_import_plan(session, plan, tenant_id=tenant, candidate_id=candidate, upload_id=upload_id)
    return upload_id


async def _question(engine: AsyncEngine, tenant: TenantId, kind: str, status: str = "open") -> uuid.UUID:
    async with tenant_transaction(engine, tenant) as session:
        found: uuid.UUID = (
            await session.execute(
                select(UserQuestion.id).where(UserQuestion.kind == kind, UserQuestion.status == status)
            )
        ).scalar_one()
    return found


async def _answer(
    engine: AsyncEngine, tenant: TenantId, user: UserId, question: uuid.UUID, answer: str
) -> None:
    async with tenant_transaction(engine, tenant) as session:
        await answer_question(
            session, tenant_id=tenant, question_id=question, answer=answer, answered_by=user
        )


async def _reconsider(
    engine: AsyncEngine, tenant: TenantId, user: UserId, question: uuid.UUID
) -> Reconsidered:
    async with tenant_transaction(engine, tenant) as session:
        return await reconsider_question(session, tenant_id=tenant, question_id=question, actor=user)


async def _kind_of_the_assistantship(engine: AsyncEngine, tenant: TenantId) -> list[str]:
    async with tenant_transaction(engine, tenant) as session:
        return list(
            (
                await session.execute(
                    select(Experience.kind).where(Experience.title == "Graduate Research Assistant")
                )
            ).scalars()
        )


class TestReconsideringAQuestion:
    async def test_a_dismissal_is_taken_back(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        await _import(engine, tenant_a, candidate_id)
        question = await _question(engine, tenant_a, "paid_or_unpaid")
        async with tenant_transaction(engine, tenant_a) as session:
            await dismiss_question(session, tenant_id=tenant_a, question_id=question, actor=user_a)
        assert await _reconsider(engine, tenant_a, user_a, question) is Reconsidered.REOPENED
        async with tenant_transaction(engine, tenant_a) as session:
            assert (await get_question(session, question)).status is QuestionStatus.OPEN

    async def test_a_paid_answer_reopens_on_its_role_and_answering_again_changes_it(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        await _import(engine, tenant_a, candidate_id)
        question = await _question(engine, tenant_a, "paid_or_unpaid")
        await _answer(engine, tenant_a, user_a, question, "yes")
        assert await _kind_of_the_assistantship(engine, tenant_a) == ["part_time"]

        assert await _reconsider(engine, tenant_a, user_a, question) is Reconsidered.REOPENED
        async with tenant_transaction(engine, tenant_a) as session:
            reopened = await get_question(session, question)
        assert reopened.status is QuestionStatus.OPEN and reopened.answer is None
        await _answer(engine, tenant_a, user_a, question, "no")
        assert await _kind_of_the_assistantship(engine, tenant_a) == ["research"]

    async def test_an_answer_that_created_a_row_still_there_is_refused_until_it_is_rejected(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        await _import(engine, tenant_a, candidate_id)
        question = await _question(engine, tenant_a, "missing_dates")
        await _answer(engine, tenant_a, user_a, question, "2024-02 to 2025-07")
        with pytest.raises(UnansweredQuestion, match="reject it first"):
            await _reconsider(engine, tenant_a, user_a, question)

        async with tenant_transaction(engine, tenant_a) as session:
            created = (await get_question(session, question)).subject_id
            assert created is not None
            await reject_entity(
                session, tenant_id=tenant_a, kind=EntityKind.EXPERIENCE, entity_id=created, rejected_by=user_a
            )
        assert await _reconsider(engine, tenant_a, user_a, question) is Reconsidered.REOPENED

    async def test_an_answer_whose_row_is_gone_is_withdrawn_and_asked_again_on_import(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        await _import(engine, tenant_a, candidate_id)
        question = await _question(engine, tenant_a, "paid_or_unpaid")
        await _answer(engine, tenant_a, user_a, question, "yes")
        async with tenant_transaction(engine, tenant_a) as session:
            # The role goes some other way than withdrawal: the answer still names it.
            await session.execute(
                update(UserQuestion).where(UserQuestion.id == question).values(subject_id=new_id())
            )

        assert await _reconsider(engine, tenant_a, user_a, question) is Reconsidered.WITHDRAWN
        await _import(engine, tenant_a, candidate_id)
        asked_again = await _question(engine, tenant_a, "paid_or_unpaid")
        assert asked_again != question

    async def test_an_open_question_has_nothing_to_reconsider(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        await _import(engine, tenant_a, candidate_id)
        question = await _question(engine, tenant_a, "paid_or_unpaid")
        with pytest.raises(UnansweredQuestion, match="still open"):
            await _reconsider(engine, tenant_a, user_a, question)


class TestWithdrawingAnUploadWithdrawsItsAnswers:
    async def _replaced(
        self, engine: AsyncEngine, tenant: TenantId, user: UserId, candidate: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID]:
        old = await _import(engine, tenant, candidate)
        await _answer(engine, tenant, user, await _question(engine, tenant, "paid_or_unpaid"), "yes")
        new = new_id()
        async with tenant_transaction(engine, tenant) as session:
            await session.execute(update(Upload).where(Upload.id == old).values(superseded_by=new))
            await session.execute(
                insert(Upload).values(
                    id=new,
                    tenant_id=tenant,
                    candidate_id=candidate,
                    original_filename="resume.txt",
                    detected_type="txt",
                    size_bytes=len(RESUME),
                    sha256=new.hex * 2,
                    quarantine_name=f"{new}.bin",
                    scan_status="not_scanned",
                    scan_detail="dev scanner",
                    parse_status="parsed",
                    extracted_text=RESUME,
                )
            )
        return old, new

    async def test_the_paid_answer_goes_with_its_role_and_is_asked_again(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        old, new = await self._replaced(engine, tenant_a, user_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            plan = await plan_withdrawal(session, candidate_id=candidate_id, upload_id=old)
            assert plan.counts()["answers"] == 1
            deleted = await withdraw(session, plan, tenant_id=tenant_a, actor_id=str(user_a))
            plan_ = parse_resume_text(RESUME, load_default_taxonomy(), today=TODAY)
            await apply_import_plan(
                session, plan_, tenant_id=tenant_a, candidate_id=candidate_id, upload_id=new
            )
        assert deleted["answers"] == 1

        asked_again = await _question(engine, tenant_a, "paid_or_unpaid")
        await _answer(engine, tenant_a, user_a, asked_again, "yes")
        assert await _kind_of_the_assistantship(engine, tenant_a) == ["part_time"]

    async def test_an_answer_whose_role_stays_is_kept(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        old, _ = await self._replaced(engine, tenant_a, user_a, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            role = (
                await session.execute(
                    select(Experience.id).where(Experience.title == "Graduate Research Assistant")
                )
            ).scalar_one()
            await approve_entity(
                session, tenant_id=tenant_a, kind=EntityKind.EXPERIENCE, entity_id=role, approved_by=user_a
            )
            plan = await plan_withdrawal(session, candidate_id=candidate_id, upload_id=old)
        assert plan.counts()["answers"] == 0
        assert plan.kept.get("answered questions") == 1
