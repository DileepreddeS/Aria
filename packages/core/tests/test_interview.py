"""The onboarding interviews: what they ask, and what an answer becomes.

RESUME_ENGINE §5 sets the contract this file holds to:

* enrichment asks **targeted** questions — each names a specific bullet, role or
  skill that is measurably incomplete;
* **answers become new draft facts**, beside the originals rather than replacing
  them, so the sentence that was approved is the sentence that was written;
* motivations are the candidate's own words and are **never inferred** from the
  resume, because what somebody has done and what they want next are different
  things.

And one rule that is about the queue rather than the graph: it asks few questions at a
time. A queue of thirty is a queue nobody answers, which leaves the graph exactly as
incomplete as before.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Experience, Motivation, ProfileField, Upload
from aria_core.db.session import tenant_transaction
from aria_core.graph.facts import approve_fact, create_draft_fact, draft_facts, get_fact
from aria_core.graph.questions import answer_question, open_questions
from aria_core.graph.status import (
    ApprovalStatus,
    ExperienceKind,
    GraphSource,
    MotivationKind,
    ProfileFieldKind,
)
from aria_core.importer.apply import apply_import_plan
from aria_core.importer.plan import QuestionKind
from aria_core.importer.resume import parse_resume_text
from aria_core.interview.enrichment import MAX_QUESTIONS_PER_ROUND, find_gaps, run_enrichment
from aria_core.interview.motivations import answered_kinds, run_motivations_interview
from aria_core.interview.templates import (
    ENRICHMENT_PROMPTS,
    MOTIVATION_PROMPTS,
    PROFILE_FIELD_PROMPTS,
    QuestionTemplate,
)
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_core.taxonomy.model import SkillTaxonomy

pytestmark = pytest.mark.db

TODAY = dt.date(2026, 10, 8)

#: A resume with the gaps the interview exists to find: a role with bullets that have
#: no numbers, a role with no bullets at all, and a skill nothing mentions.
RESUME = """EXPERIENCE
Accenture - Software Engineer
Jun 2024 - Present
- Rewrote the claims export as a scheduled job
- Took over the on-call rotation for the billing service

Zelis - Data Engineering Intern
May 2023 - Aug 2023

TECHNICAL SKILLS
Languages: Python, TypeScript
Infrastructure: Kafka, Terraform
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


async def imported(
    engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
) -> uuid.UUID:
    upload_id = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(
            insert(Upload).values(
                id=upload_id,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                original_filename="resume.txt",
                detected_type="txt",
                size_bytes=512,
                sha256=upload_id.hex * 2,
                quarantine_name=f"{upload_id}.bin",
                scan_status="not_scanned",
                scan_detail="dev scanner",
                parse_status="parsed",
            )
        )
    plan = parse_resume_text(RESUME, taxonomy, today=TODAY)
    async with tenant_transaction(engine, tenant_a) as session:
        await apply_import_plan(
            session, plan, tenant_id=tenant_a, candidate_id=candidate_id, upload_id=upload_id
        )
    return upload_id


class TestWhatTheEnrichmentInterviewFinds:
    async def test_a_bullet_with_no_number_is_a_gap(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            gaps = await find_gaps(session, candidate_id)
        metric_gaps = [gap for gap in gaps if gap.kind == QuestionTemplate.MISSING_METRIC]
        assert metric_gaps
        # The question quotes the bullet, so it is answerable without going to look.
        assert any("claims export" in gap.prompt for gap in metric_gaps)

    async def test_a_role_with_no_bullets_is_a_gap(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            gaps = await find_gaps(session, candidate_id)
        context = [gap for gap in gaps if gap.kind == QuestionTemplate.MISSING_CONTEXT]
        assert [gap.payload["employer"] for gap in context] == ["Zelis"]

    async def test_a_listed_skill_nothing_mentions_is_a_gap(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        # Kafka and Terraform are on the skills line and in no bullet.
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            gaps = await find_gaps(session, candidate_id)
        skills = {
            gap.payload["skill_canonical"] for gap in gaps if gap.kind == QuestionTemplate.UNEVIDENCED_SKILL
        }
        assert {"kafka", "terraform"} <= skills

    async def test_a_missing_contact_detail_is_a_gap(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        # This resume has no header at all.
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            gaps = await find_gaps(session, candidate_id)
        fields = {
            gap.payload["field_kind"] for gap in gaps if gap.kind == QuestionTemplate.MISSING_PROFILE_FIELD
        }
        assert ProfileFieldKind.EMAIL.value in fields
        assert ProfileFieldKind.PHONE.value in fields

    async def test_an_empty_role_outranks_a_missing_number(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        # Nothing can be tailored from a role with no achievements, so it is asked
        # about first. A fifth bullet without a number can wait.
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            gaps = await find_gaps(session, candidate_id)
        kinds = [gap.kind for gap in gaps]
        assert kinds.index(QuestionTemplate.MISSING_CONTEXT) < kinds.index(QuestionTemplate.MISSING_METRIC)

    async def test_a_bullet_that_already_has_a_number_is_not_a_gap(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await create_draft_fact(
                session,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                text="Cut the nightly export from 40 minutes to 6 minutes",
                source=GraphSource.RESUME_IMPORT,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            gaps = await find_gaps(session, candidate_id)
        assert not any("nightly export" in gap.prompt for gap in gaps)

    async def test_finding_gaps_writes_nothing(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        # Seeing what is missing should not fill anybody's queue.
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            before = len(await open_questions(session, candidate_id))
            await find_gaps(session, candidate_id)
        async with tenant_transaction(engine, tenant_a) as session:
            assert len(await open_questions(session, candidate_id)) == before


class TestTheInterviewAsksFewQuestions:
    async def test_a_round_is_capped(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            queued = await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id)
        assert 0 < len(queued) <= MAX_QUESTIONS_PER_ROUND

    async def test_the_limit_is_honoured(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            queued = await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=2)
        assert len(queued) == 2

    async def test_a_second_round_asks_the_next_gaps_not_the_same_ones(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        # The cap is a pace, not a limit on what gets asked: the gaps a round skips
        # are found again next time, and the ones already asked are not repeated.
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            first = await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=3)
        async with tenant_transaction(engine, tenant_a) as session:
            second = await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=3)
        assert len(first) == 3
        assert second
        asked = {gap.dedupe_key for gap in first}
        assert not asked & {gap.dedupe_key for gap in second}

    async def test_once_everything_has_been_asked_it_asks_nothing(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=50)
        async with tenant_transaction(engine, tenant_a) as session:
            again = await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=50)
        assert again == []

    async def test_the_questions_reach_the_queue(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=2)
        async with tenant_transaction(engine, tenant_a) as session:
            waiting = await open_questions(session, candidate_id)
        assert any(
            question.kind in {gap for gap in vars(QuestionTemplate).values() if isinstance(gap, str)}
            for question in waiting
        )


class TestAnswersBecomeNewDraftFacts:
    async def test_the_answer_arrives_as_a_draft_fact(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionTemplate.MISSING_METRIC
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="It cut the export from 40 minutes to 6 minutes",
                answered_by=user_a,
            )

        assert outcome.subject_kind == "fact"
        async with tenant_transaction(engine, tenant_a) as session:
            created = await get_fact(session, outcome.subject_id)  # type: ignore[arg-type]
        assert created.status is ApprovalStatus.DRAFT
        assert created.source is GraphSource.ENRICHMENT_INTERVIEW
        assert created.text == "It cut the export from 40 minutes to 6 minutes"

    async def test_the_original_bullet_is_left_alone(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # The alternative -- rewriting the bullet that was asked about -- would mean a
        # sentence typed into a prompt silently replacing one already approved.
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionTemplate.MISSING_METRIC
            )
            original_id = question.subject_id
            before = await get_fact(session, original_id)  # type: ignore[arg-type]
            await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="Four teams depend on it now",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            after = await get_fact(session, original_id)  # type: ignore[arg-type]
        assert after.text == before.text
        assert after.version == before.version

    async def test_the_new_fact_is_anchored_to_the_same_role(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionTemplate.MISSING_METRIC
            )
            original = await get_fact(session, question.subject_id)  # type: ignore[arg-type]
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="It saved about 6 hours a week",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            created = await get_fact(session, outcome.subject_id)  # type: ignore[arg-type]
        assert created.experience_id == original.experience_id

    async def test_a_number_in_the_answer_is_captured(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # The answer goes through the same extraction as an imported bullet, so the
        # number is stored with the text that states it.
        from aria_core.db.models import FactMetric

        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionTemplate.MISSING_METRIC
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="Four teams use it and it saved 6 hours a week",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            values = (
                (
                    await session.execute(
                        select(FactMetric.exact_value).where(FactMetric.fact_id == outcome.subject_id)
                    )
                )
                .scalars()
                .all()
            )
        assert "6 hours" in values

    async def test_the_skill_asked_about_becomes_evidence(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # "I used it for the event pipeline" is an answer about Kafka even though it
        # does not spell Kafka out again.
        from aria_core.db.models import FactSkill

        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=50)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionTemplate.UNEVIDENCED_SKILL
                and q.payload.get("skill_canonical") == "kafka"
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="I used it for the event pipeline at Accenture",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = (
                (
                    await session.execute(
                        select(FactSkill.skill_canonical).where(FactSkill.fact_id == outcome.subject_id)
                    )
                )
                .scalars()
                .all()
            )
        assert "kafka" in skills

    async def test_a_skill_with_a_draft_fact_behind_it_is_not_asked_about_again(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # The gap is filled the moment the draft exists. Asking again because it has
        # not been approved yet would be asking about something already done.
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=50)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionTemplate.UNEVIDENCED_SKILL
                and q.payload.get("skill_canonical") == "kafka"
            )
            await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="I used Kafka for the event pipeline",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            gaps = await find_gaps(session, candidate_id)
        assert not any(gap.payload.get("skill_canonical") == "kafka" for gap in gaps)


class TestContactDetails:
    async def test_an_answer_becomes_a_draft_profile_field(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=50)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.payload.get("field_kind") == ProfileFieldKind.PHONE.value
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="+1 469 555 0134",
                answered_by=user_a,
            )

        assert outcome.subject_kind == "profile_field"
        async with tenant_transaction(engine, tenant_a) as session:
            row = (
                await session.execute(
                    select(
                        ProfileField.kind, ProfileField.value, ProfileField.status, ProfileField.source
                    ).where(ProfileField.id == outcome.subject_id)
                )
            ).first()
        assert row is not None
        assert row[0] == ProfileFieldKind.PHONE.value
        assert row[1] == "+1 469 555 0134"
        # Still a draft: a phone number is the field where a typo costs an interview
        # and nobody notices.
        assert row[2] == ApprovalStatus.DRAFT.value
        assert row[3] == GraphSource.ENRICHMENT_INTERVIEW.value


class TestTheMotivationsInterview:
    async def test_it_asks_all_six(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            queued = await run_motivations_interview(session, tenant_id=tenant_a, candidate_id=candidate_id)
        assert set(queued) == set(MOTIVATION_PROMPTS)

    async def test_an_answer_becomes_a_draft_motivation_in_their_own_words(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await run_motivations_interview(session, tenant_id=tenant_a, candidate_id=candidate_id)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.payload.get("motivation_kind") == MotivationKind.AVOID.value
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="On-call without compensation",
                answered_by=user_a,
            )

        assert outcome.subject_kind == "motivation"
        async with tenant_transaction(engine, tenant_a) as session:
            row = (
                await session.execute(
                    select(Motivation.kind, Motivation.text, Motivation.status, Motivation.source).where(
                        Motivation.id == outcome.subject_id
                    )
                )
            ).first()
        assert row is not None
        assert row[0] == MotivationKind.AVOID.value
        # Exactly what they typed. A motivation is the one thing that cannot be
        # checked against a fact, so it is stored verbatim or not at all.
        assert row[1] == "On-call without compensation"
        assert row[2] == ApprovalStatus.DRAFT.value
        assert row[3] == GraphSource.MOTIVATIONS_INTERVIEW.value

    async def test_re_running_it_asks_only_what_is_still_missing(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await run_motivations_interview(session, tenant_id=tenant_a, candidate_id=candidate_id)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.payload.get("motivation_kind") == MotivationKind.WANTS_NEXT.value
            )
            await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="Owning a service end to end",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            assert await answered_kinds(session, candidate_id) == {MotivationKind.WANTS_NEXT}
            again = await run_motivations_interview(session, tenant_id=tenant_a, candidate_id=candidate_id)
        # Nothing new: the other five are already on the queue, and the answered one
        # is not asked again.
        assert again == []

    async def test_nothing_is_inferred_from_the_resume(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        # Importing a resume must not put a single row in the motivations table. What
        # somebody has done and what they want next are different things.
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id, limit=50)
            rows = (
                (await session.execute(select(Motivation.id).where(Motivation.candidate_id == candidate_id)))
                .scalars()
                .all()
            )
        assert rows == []


class TestTheTemplates:
    def test_every_prompt_says_why_it_is_being_asked(self) -> None:
        # "Why are you asking me this?" is a fair question to have an answer to, and
        # a question whose purpose is visible gets a better answer.
        for prompt in (
            *ENRICHMENT_PROMPTS.values(),
            *PROFILE_FIELD_PROMPTS.values(),
            *MOTIVATION_PROMPTS.values(),
        ):
            assert prompt.why.strip()
            assert prompt.detail().strip()

    def test_every_motivation_kind_has_a_question(self) -> None:
        # A kind with no question is a column nothing can ever fill.
        assert set(MOTIVATION_PROMPTS) == set(MotivationKind)

    def test_every_required_profile_field_has_a_question(self) -> None:
        from aria_core.interview.enrichment import REQUIRED_PROFILE_FIELDS

        assert set(REQUIRED_PROFILE_FIELDS) <= set(PROFILE_FIELD_PROMPTS)

    def test_the_prompts_are_deterministic(self) -> None:
        prompt = ENRICHMENT_PROMPTS[QuestionTemplate.MISSING_CONTEXT]
        first = prompt.render(employer="Accenture", title="Software Engineer")
        second = prompt.render(employer="Accenture", title="Software Engineer")
        assert first == second
        assert "Accenture" in first

    def test_every_template_kind_is_on_the_question_enum(self) -> None:
        # The table's CHECK constraint and the enum have to agree, or a question the
        # interview asks cannot be stored.
        kinds = {kind.value for kind in QuestionKind}
        templates = {
            value
            for name, value in vars(QuestionTemplate).items()
            if name.isupper() and isinstance(value, str)
        }
        assert templates <= kinds


class TestTheInterviewFitsTheRestOfTheFlow:
    async def test_an_approved_answer_closes_the_gap_it_came_from(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # The whole loop: import, ask, answer, approve. The role that had nothing
        # under it has a fact, so it stops being a gap.
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionTemplate.MISSING_CONTEXT
            )
            outcome = await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="I built the nightly claims reconciliation in Python",
                answered_by=user_a,
            )
            await approve_fact(
                session,
                tenant_id=tenant_a,
                fact_id=outcome.subject_id,  # type: ignore[arg-type]
                approved_by=user_a,
            )

        async with tenant_transaction(engine, tenant_a) as session:
            gaps = await find_gaps(session, candidate_id)
        assert not any(gap.kind == QuestionTemplate.MISSING_CONTEXT for gap in gaps)

    async def test_a_role_added_by_hand_is_asked_about(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        # The interview reads the graph, not the upload, so a role that arrived some
        # other way is treated the same.
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Experience).values(
                    id=new_id(),
                    tenant_id=tenant_a,
                    candidate_id=candidate_id,
                    employer="Carvana",
                    title="Contract Engineer",
                    kind=ExperienceKind.CONTRACT.value,
                    start_month=dt.date(2022, 1, 1),
                    end_month=dt.date(2022, 6, 1),
                    status=ApprovalStatus.DRAFT.value,
                    source=GraphSource.USER_EDIT.value,
                )
            )
        async with tenant_transaction(engine, tenant_a) as session:
            gaps = await find_gaps(session, candidate_id)
        assert any("Carvana" in gap.prompt for gap in gaps)

    async def test_the_draft_facts_show_up_in_the_review_queue(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await imported(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            before = len(await draft_facts(session, candidate_id))
            await run_enrichment(session, tenant_id=tenant_a, candidate_id=candidate_id)
            question = next(
                q
                for q in await open_questions(session, candidate_id)
                if q.kind == QuestionTemplate.MISSING_CONTEXT
            )
            await answer_question(
                session,
                tenant_id=tenant_a,
                question_id=question.question_id,
                answer="I built the nightly reconciliation job",
                answered_by=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            after = await draft_facts(session, candidate_id)
        assert len(after) == before + 1
        assert any(fact.source is GraphSource.ENRICHMENT_INTERVIEW for fact in after)
