"""The question queue, and what answering one does.

CLAUDE.md turns low confidence into a ``UserQuestion`` rather than a default answer.
This file holds that to its consequences:

* a question survives the request that raised it, so "what is still waiting?" has an
  answer;
* re-importing the same resume does not ask again, because a queue that grows with
  every upload is one nobody reads;
* answering applies the change **as a draft** — the held-back role arrives with its
  bullets and still needs approving, so the answer and the claim stay two acts;
* an answer that does not answer the question is refused rather than stored, because
  a stored shrug would close the question without resolving it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.audit.log import read_chain
from aria_core.db.models import Candidate, Experience, Upload, User, UserQuestion
from aria_core.db.session import tenant_transaction
from aria_core.graph.facts import draft_facts
from aria_core.graph.questions import (
    QuestionNotFound,
    QuestionStatus,
    UnansweredQuestion,
    answer_question,
    ask,
    dismiss_question,
    get_question,
    open_question_count,
    open_questions,
    questions_for,
)
from aria_core.graph.status import ApprovalStatus, ExperienceKind
from aria_core.importer.apply import apply_import_plan
from aria_core.importer.plan import QuestionKind
from aria_core.importer.resume import parse_resume_text
from aria_core.schemas.audit import AuditAction
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_core.taxonomy.model import SkillTaxonomy

pytestmark = pytest.mark.db

TODAY = dt.date(2026, 10, 8)

#: An internship the resume dated by season, and an assistantship whose pay status
#: only the candidate knows. One question each, for different reasons.
RESUME = """EXPERIENCE
Zelis - Software Engineering Intern
Summer 2023
- Automated a manual claims report with Python, saving 6 hours a week
- Shipped an internal dashboard in React

Cascadia State University - Graduate Research Assistant
Sept 2022 - May 2023
- Fine-tuned a model for defect detection
"""


@pytest.fixture(scope="module")
def taxonomy() -> SkillTaxonomy:
    return load_default_taxonomy()


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
    return created


async def _upload(engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID) -> uuid.UUID:
    upload_id = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(
            insert(Upload).values(
                id=upload_id,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                original_filename="resume.pdf",
                detected_type="pdf",
                size_bytes=2048,
                sha256=upload_id.hex * 2,
                quarantine_name=f"{upload_id}.bin",
                scan_status="not_scanned",
                scan_detail="dev scanner",
                parse_status="parsed",
            )
        )
    return upload_id


async def _import(
    engine: AsyncEngine,
    tenant_a: TenantId,
    candidate_id: uuid.UUID,
    taxonomy: SkillTaxonomy,
    text: str = RESUME,
) -> uuid.UUID:
    upload_id = await _upload(engine, tenant_a, candidate_id)
    plan = parse_resume_text(text, taxonomy, today=TODAY)
    async with tenant_transaction(engine, tenant_a) as session:
        await apply_import_plan(
            session,
            plan,
            tenant_id=tenant_a,
            candidate_id=candidate_id,
            upload_id=upload_id,
        )
    return upload_id


class TestTheQueue:
    async def test_an_import_leaves_its_questions_behind(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            waiting = await open_questions(session, candidate_id)
        kinds = {question.kind for question in waiting}
        assert QuestionKind.MISSING_DATES.value in kinds
        assert QuestionKind.PAID_OR_UNPAID.value in kinds

    async def test_a_question_keeps_the_lines_that_prompted_it(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        upload_id = await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            dates = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.MISSING_DATES.value
            )
        assert dates.source_upload_id == upload_id
        assert dates.source_lines == (2,)
        assert "season" in dates.detail

    async def test_re_importing_does_not_ask_again(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # The dedupe key is built from what the question is about, not from the
        # upload, so a second export of the same resume is not a second queue.
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            first = await open_question_count(session, candidate_id)

        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            assert await open_question_count(session, candidate_id) == first

    async def test_the_outcome_reports_what_was_actually_queued(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        upload_id = await _upload(engine, tenant_a, candidate_id)
        plan = parse_resume_text(RESUME, taxonomy, today=TODAY)
        async with tenant_transaction(engine, tenant_a) as session:
            again = await apply_import_plan(
                session,
                plan,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                upload_id=upload_id,
            )
        assert again.questions
        assert again.question_ids == ()
        assert "already been asked" in "\n".join(again.summary_lines())

    async def test_two_candidates_may_hold_the_same_question(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        # Dedupe is per candidate, not global. (One candidate per user, by
        # constraint, so this needs two users.)
        async with tenant_transaction(engine, tenant_a) as session:
            for _ in range(2):
                user_id, candidate_id = new_id(), new_id()
                await session.execute(
                    insert(User).values(
                        id=user_id,
                        tenant_id=tenant_a,
                        email=f"{user_id.hex[:12]}@example.test",
                        display_name="Another",
                    )
                )
                await session.execute(
                    insert(Candidate).values(id=candidate_id, tenant_id=tenant_a, user_id=user_id)
                )
                created = await ask(
                    session,
                    tenant_id=tenant_a,
                    candidate_id=candidate_id,
                    kind=QuestionKind.MISSING_DATES.value,
                    prompt="When did this start?",
                    dedupe_key="missing_dates:acme",
                )
                assert created is not None

    async def test_asking_twice_returns_none_the_second_time(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            first = await ask(
                session,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                kind=QuestionKind.UNKNOWN_SKILLS.value,
                prompt="Is Fortran a skill?",
                dedupe_key="unknown_skills:fortran",
            )
            second = await ask(
                session,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                kind=QuestionKind.UNKNOWN_SKILLS.value,
                prompt="Is Fortran a skill?",
                dedupe_key="unknown_skills:fortran",
            )
        assert first is not None
        assert second is None


class TestAnsweringCreatesTheHeldBackRole:
    async def test_the_role_arrives_as_a_draft_with_its_bullets(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            dates = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.MISSING_DATES.value
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=dates.question_id,
                answer="2023-06 to 2023-08",
                answered_by=user_a,
            )

        assert outcome.subject_kind == "experience"
        assert len(outcome.fact_ids) == 2

        async with tenant_transaction(engine, tenant_a) as session:
            row = (
                await session.execute(
                    select(
                        Experience.employer,
                        Experience.title,
                        Experience.kind,
                        Experience.start_month,
                        Experience.end_month,
                        Experience.status,
                    ).where(Experience.id == outcome.subject_id)
                )
            ).first()

        assert row is not None
        assert row[0] == "Zelis"
        assert row[1] == "Software Engineering Intern"
        assert row[2] == ExperienceKind.INTERNSHIP.value
        assert row[3] == dt.date(2023, 6, 1)
        assert row[4] == dt.date(2023, 8, 1)
        # Still a draft: the answer supplied the months, not the approval.
        assert row[5] == ApprovalStatus.DRAFT.value

    async def test_the_bullets_are_attached_to_the_new_role(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            dates = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.MISSING_DATES.value
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=dates.question_id,
                answer="2023-06 to 2023-08",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            facts = [
                fact
                for fact in await draft_facts(session, candidate_id)
                if fact.experience_id == outcome.subject_id
            ]
        assert len(facts) == 2
        assert any("claims report" in fact.text for fact in facts)
        # The line numbers survived the round trip through the payload.
        assert all(fact.source_line is not None for fact in facts)

    async def test_an_open_end_is_allowed(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            dates = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.MISSING_DATES.value
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=dates.question_id,
                answer="2024-06 to present",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            end = (
                await session.execute(select(Experience.end_month).where(Experience.id == outcome.subject_id))
            ).scalar_one()
        assert end is None

    async def test_the_question_closes_with_a_name_on_it(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            dates = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.MISSING_DATES.value
            )
            await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=dates.question_id,
                answer="2023-06 to 2023-08",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            answered = await get_question(session, dates.question_id)
            row = (
                await session.execute(
                    select(UserQuestion.answered_by, UserQuestion.answered_at).where(
                        UserQuestion.id == dates.question_id
                    )
                )
            ).first()
        assert answered.status is QuestionStatus.ANSWERED
        assert answered.answer == "2023-06 to 2023-08"
        assert row is not None and row[0] == user_a and row[1] is not None

    @pytest.mark.parametrize(
        "answer",
        ["summer", "2023-06", "maybe", "Jun 2023 - Aug 2023 ish", "2023-08 to 2023-06"],
        ids=["a-season", "no-end", "a-shrug", "unparseable", "backwards"],
    )
    async def test_an_answer_that_does_not_answer_is_refused(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
        answer: str,
    ) -> None:
        # Storing a shrug would close the question without resolving it, and the
        # policy engine would then allow a submission that is still missing a date.
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            dates = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.MISSING_DATES.value
            )
            with pytest.raises(UnansweredQuestion):
                await answer_question(
                    session,
                    tenant_id=tenant_a,
                    question_id=dates.question_id,
                    answer=answer,
                    answered_by=user_a,
                )

        async with tenant_transaction(engine, tenant_a) as session:
            assert (await get_question(session, dates.question_id)).is_open


class TestAnsweringWhetherItWasPaid:
    async def test_paid_makes_it_part_time(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # Paid is part-time employment and counts towards general work experience;
        # unpaid is project time and does not.
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            paid = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.PAID_OR_UNPAID.value
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=paid.question_id,
                answer="yes",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            kind = (
                await session.execute(select(Experience.kind).where(Experience.id == outcome.subject_id))
            ).scalar_one()
        assert kind == ExperienceKind.PART_TIME.value

    async def test_unpaid_leaves_it_as_project_time(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            paid = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.PAID_OR_UNPAID.value
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=paid.question_id,
                answer="no",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            kind = (
                await session.execute(select(Experience.kind).where(Experience.id == outcome.subject_id))
            ).scalar_one()
        assert kind == ExperienceKind.RESEARCH.value

    @pytest.mark.parametrize("answer", ["maybe", "sort of", "I think so?"])
    async def test_a_non_answer_is_refused(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
        answer: str,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            paid = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.PAID_OR_UNPAID.value
            )
            with pytest.raises(UnansweredQuestion, match="does not answer"):
                await answer_question(
                    session,
                    tenant_id=tenant_a,
                    question_id=paid.question_id,
                    answer=answer,
                    answered_by=user_a,
                )


class TestLifecycle:
    async def test_a_question_cannot_be_answered_twice(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # Otherwise the second answer would create the role a second time.
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            dates = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.MISSING_DATES.value
            )
            await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=dates.question_id,
                answer="2023-06 to 2023-08",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            with pytest.raises(UnansweredQuestion, match="already answered"):
                await answer_question(
                    session,
                    tenant_id=tenant_a,
                    question_id=dates.question_id,
                    answer="2024-01 to 2024-02",
                    answered_by=user_a,
                )

    async def test_a_dismissed_question_is_kept_and_not_re_asked(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            waiting = await open_questions(session, candidate_id)
            await dismiss_question(
                session,
                tenant_id=tenant_a,
                question_id=waiting[0].question_id,
                actor=user_a,
                reason="not worth answering",
            )

        async with tenant_transaction(engine, tenant_a) as session:
            assert await open_question_count(session, candidate_id) == len(waiting) - 1
            dismissed = await questions_for(session, candidate_id, status=QuestionStatus.DISMISSED)
        assert [question.question_id for question in dismissed] == [waiting[0].question_id]

        # A re-import must not resurrect it.
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            assert await open_question_count(session, candidate_id) == len(waiting) - 1

    async def test_an_unknown_question_is_not_found(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            with pytest.raises(QuestionNotFound):
                await answer_question(
                    session,
                    tenant_id=tenant_a,
                    question_id=new_id(),
                    answer="2023-06 to 2023-08",
                    answered_by=user_a,
                )

    async def test_a_blank_answer_is_refused(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            waiting = await open_questions(session, candidate_id)
            with pytest.raises(UnansweredQuestion, match="cannot be blank"):
                await answer_question(
                    session,
                    tenant_id=tenant_a,
                    question_id=waiting[0].question_id,
                    answer="   ",
                    answered_by=user_a,
                )


class TestTheAuditTrail:
    async def test_answering_is_audited_without_the_answer_text(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # The answer is S1 and stays in the row. The event records its shape.
        await _import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            dates = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionKind.MISSING_DATES.value
            )
            await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=dates.question_id,
                answer="2023-06 to 2023-08",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)

        answered = [event for event in events if event.action == AuditAction.USER_QUESTION_ANSWERED.value]
        assert len(answered) == 1
        assert answered[0].subject_id == str(dates.question_id)
        assert answered[0].actor_id == str(user_a)
        assert answered[0].payload["created"] == "experience"
        assert answered[0].payload["facts_created"] == 2
        assert "2023-06" not in str(answered[0].payload)
