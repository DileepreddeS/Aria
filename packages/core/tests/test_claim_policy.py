"""What "years of experience" is allowed to mean.

The default is full-time only. An internship is real, dated, paid work that a hiring
manager reads differently from a salaried year, so a new grad answering "2.6 years" on
the strength of internships has overstated — which is the specific failure this
product exists to avoid.

Internship months join the figure in exactly two cases, and both are somebody saying
so out loud: the question includes them, or the candidate has opted in. Neither is
ARIA deciding on their behalf.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Experience
from aria_core.db.session import tenant_transaction
from aria_core.graph.derive import (
    ClaimPolicy,
    SkillExperience,
    compute_career_totals,
    compute_skill_experience,
    load_claim_policy,
)
from aria_core.graph.facts import approve_fact, create_draft_fact
from aria_core.graph.months import MonthSpan
from aria_core.graph.status import ApprovalStatus, ExperienceBucket, ExperienceKind, GraphSource
from aria_core.schemas.identity import TenantId, UserId, new_id

NOW = dt.date(2026, 10, 15)


def month(year: int, month_number: int) -> dt.date:
    return dt.date(year, month_number, 1)


# --------------------------------------------------------------- the policy itself


class TestThePolicyIsExplicit:
    def test_the_default_is_full_time_only(self) -> None:
        policy = ClaimPolicy.default()
        assert policy.include_internships is False
        assert "full-time only" in policy.reason

    def test_an_unset_policy_behaves_as_the_default(self) -> None:
        skill = _skill(
            full_time=[(month(2024, 6), month(2025, 5))], internship=[(month(2023, 6), month(2023, 8))]
        )
        assert skill.claimable_months() == skill.claimable_months(ClaimPolicy.default()) == 12

    def test_a_question_that_includes_internships_opts_in(self) -> None:
        policy = ClaimPolicy.resolve(question_includes_internships=True)
        assert policy.include_internships is True
        assert "question" in policy.reason

    def test_the_candidate_setting_opts_in(self) -> None:
        policy = ClaimPolicy.resolve(candidate_setting=True)
        assert policy.include_internships is True
        assert "candidate" in policy.reason

    def test_both_at_once_is_still_opted_in_and_says_so(self) -> None:
        policy = ClaimPolicy.resolve(question_includes_internships=True, candidate_setting=True)
        assert policy.include_internships is True
        assert "question" in policy.reason and "opted in" in policy.reason

    def test_neither_is_the_default(self) -> None:
        assert (
            ClaimPolicy.resolve(question_includes_internships=False, candidate_setting=False)
            == ClaimPolicy.default()
        )

    def test_the_reason_travels_with_the_decision(self) -> None:
        # So an audit trail can say why a number was what it was, not just what it was.
        for policy in (
            ClaimPolicy.default(),
            ClaimPolicy.resolve(question_includes_internships=True),
            ClaimPolicy.resolve(candidate_setting=True),
        ):
            assert policy.reason


# ------------------------------------------------------- the rule, without a database


def _skill(
    *,
    full_time: list[tuple[dt.date, dt.date | None]] | None = None,
    part_time: list[tuple[dt.date, dt.date | None]] | None = None,
    internship: list[tuple[dt.date, dt.date | None]] | None = None,
    project: list[tuple[dt.date, dt.date | None]] | None = None,
) -> SkillExperience:
    return SkillExperience(
        canonical="python",
        full_time_spans=tuple(MonthSpan(start, end) for start, end in full_time or []),
        part_time_spans=tuple(MonthSpan(start, end) for start, end in part_time or []),
        internship_spans=tuple(MonthSpan(start, end) for start, end in internship or []),
        project_spans=tuple(MonthSpan(start, end) for start, end in project or []),
        as_of=NOW,
    )


class TestClaimableMonths:
    def test_by_default_only_full_time_counts(self) -> None:
        skill = _skill(
            full_time=[(month(2024, 6), month(2025, 5))],  # 12
            internship=[(month(2023, 6), month(2023, 8))],  # 3
            project=[(month(2022, 1), month(2022, 12))],  # 12
        )
        assert skill.claimable_months() == 12
        assert skill.claimable_years() == 1.0

    def test_internships_are_added_when_the_policy_allows(self) -> None:
        skill = _skill(
            full_time=[(month(2024, 6), month(2025, 5))],
            internship=[(month(2023, 6), month(2023, 8))],
        )
        assert skill.claimable_months(ClaimPolicy.resolve(candidate_setting=True)) == 15
        assert skill.claimable_years(ClaimPolicy.resolve(candidate_setting=True)) == 1.2

    def test_project_months_are_never_claimable(self) -> None:
        # Not under any policy. A class project is not experience, however it is asked.
        skill = _skill(project=[(month(2022, 1), month(2022, 12))])
        for policy in (
            ClaimPolicy.default(),
            ClaimPolicy.resolve(question_includes_internships=True),
            ClaimPolicy.resolve(candidate_setting=True),
        ):
            assert skill.claimable_months(policy) == 0

    def test_an_overlapping_internship_is_unioned_not_added(self) -> None:
        # The internship ran during the full-time role. Adding the totals would
        # invent three months the calendar does not contain.
        skill = _skill(
            full_time=[(month(2024, 1), month(2024, 12))],  # 12
            internship=[(month(2024, 6), month(2024, 8))],  # 3, inside the above
        )
        assert skill.full_time_months == 12
        assert skill.internship_months == 3
        assert skill.claimable_months(ClaimPolicy.resolve(candidate_setting=True)) == 12

    def test_a_partially_overlapping_internship_adds_only_its_new_months(self) -> None:
        skill = _skill(
            full_time=[(month(2024, 6), month(2024, 12))],  # 7
            internship=[(month(2024, 4), month(2024, 7))],  # 4, two of them new
        )
        assert skill.claimable_months(ClaimPolicy.resolve(candidate_setting=True)) == 9

    def test_with_no_full_time_evidence_the_default_claim_is_zero(self) -> None:
        # The honest answer for a new grad with internships only, when asked a plain
        # "years of experience" question.
        skill = _skill(internship=[(month(2023, 6), month(2023, 8))])
        assert skill.claimable_months() == 0
        assert skill.claimable_years() == 0.0
        assert skill.has_full_time_evidence is False
        # And the internship months are still there to be reported honestly.
        assert skill.internship_months == 3


class TestBucketsAreDisjoint:
    def test_the_three_buckets_cover_every_kind_exactly_once(self) -> None:
        mapping = {kind: kind.bucket for kind in ExperienceKind}
        assert set(mapping.values()) == set(ExperienceBucket)
        # Each kind maps to exactly one bucket, which is what makes a summary safe.
        assert len(mapping) == len(ExperienceKind)

    @pytest.mark.parametrize(
        ("kind", "bucket"),
        [
            (ExperienceKind.PROFESSIONAL, ExperienceBucket.FULL_TIME),
            (ExperienceKind.CONTRACT, ExperienceBucket.FULL_TIME),
            (ExperienceKind.PART_TIME, ExperienceBucket.PART_TIME),
            (ExperienceKind.INTERNSHIP, ExperienceBucket.INTERNSHIP),
            (ExperienceKind.RESEARCH, ExperienceBucket.PROJECT),
            (ExperienceKind.VOLUNTEER, ExperienceBucket.PROJECT),
        ],
    )
    def test_each_kind_lands_where_expected(self, kind: ExperienceKind, bucket: ExperienceBucket) -> None:
        assert kind.bucket is bucket

    def test_contract_work_is_full_time_not_an_internship(self) -> None:
        # A six-month contract is paid professional work.
        assert ExperienceKind.CONTRACT.bucket is ExperienceBucket.FULL_TIME

    def test_a_skill_reports_each_bucket_separately(self) -> None:
        skill = _skill(
            full_time=[(month(2024, 1), month(2024, 12))],
            part_time=[(month(2021, 9), month(2022, 5))],
            internship=[(month(2023, 1), month(2023, 3))],
            project=[(month(2020, 1), month(2020, 2))],
        )
        assert skill.months_in(ExperienceBucket.FULL_TIME) == 12
        assert skill.months_in(ExperienceBucket.PART_TIME) == 9
        assert skill.months_in(ExperienceBucket.INTERNSHIP) == 3
        assert skill.months_in(ExperienceBucket.PROJECT) == 2

    def test_describe_lists_the_buckets_and_never_a_total(self) -> None:
        skill = _skill(
            full_time=[(month(2024, 1), month(2024, 12))],
            part_time=[(month(2021, 9), month(2022, 5))],
            internship=[(month(2023, 1), month(2023, 3))],
        )
        described = skill.describe()
        assert "1.0y full-time" in described
        assert "9mo part-time" in described
        assert "3mo internship" in described
        # 24 months would be the sum; it must not appear as one figure.
        assert "2.0y" not in described


# -------------------------------------------------------------- against a real graph


@pytest.mark.db
class TestAgainstTheGraph:
    @pytest.fixture
    async def candidate_with_both(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        """A full-time role and an earlier internship, both approved, both using Python."""
        candidate_id, full_time_id, internship_id = new_id(), new_id(), new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Candidate).values(id=candidate_id, tenant_id=tenant_a, user_id=user_a)
            )
            common = {
                "tenant_id": tenant_a,
                "candidate_id": candidate_id,
                "status": ApprovalStatus.APPROVED.value,
                # An approved row names who approved it; the database enforces it.
                "approved_by": user_a,
                "source": GraphSource.RESUME_IMPORT.value,
                "approved_at": dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
            }
            await session.execute(
                insert(Experience).values(
                    id=full_time_id,
                    employer="Accenture",
                    title="Software Engineer",
                    kind=ExperienceKind.PROFESSIONAL.value,
                    start_month=month(2024, 6),
                    end_month=None,
                    **common,
                )
            )
            await session.execute(
                insert(Experience).values(
                    id=internship_id,
                    employer="Startup",
                    title="SWE Intern",
                    kind=ExperienceKind.INTERNSHIP.value,
                    start_month=month(2023, 6),
                    end_month=month(2023, 8),
                    **common,
                )
            )
            for experience_id in (full_time_id, internship_id):
                fact_id = await create_draft_fact(
                    session,
                    tenant_id=tenant_a,
                    candidate_id=candidate_id,
                    text=f"Python at {experience_id}",
                    source=GraphSource.RESUME_IMPORT,
                    experience_id=experience_id,
                    skills=[("python", "Python")],
                )
                await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)
        return candidate_id, full_time_id, internship_id

    async def test_the_summary_does_not_double_count_the_internship(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_with_both: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        candidate_id, _, _ = candidate_with_both
        async with tenant_transaction(engine, tenant_a) as session:
            totals = await compute_career_totals(session, candidate_id, now=NOW)

        # 29 full-time months and 3 internship months, printed side by side. The old
        # shape reported 32 and 3, which counted the internship twice.
        assert totals.full_time_months == 29
        assert totals.internship_months == 3
        assert totals.full_time_months + totals.internship_months == 32
        assert totals.claimable_months() == 29

        lines = "\n".join(totals.summary_lines())
        assert "full-time:  29 months" in lines
        assert "internship: 3 months" in lines
        assert "32" not in lines

    async def test_the_default_claim_is_full_time_only(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_with_both: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        candidate_id, _, _ = candidate_with_both
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, candidate_id, now=NOW)
            policy = await load_claim_policy(session, candidate_id)

        assert policy.include_internships is False
        assert skills["python"].claimable_months(policy) == 29
        assert skills["python"].claimable_years(policy) == 2.4

    async def test_a_question_including_internships_changes_the_answer(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_with_both: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        candidate_id, _, _ = candidate_with_both
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, candidate_id, now=NOW)
            policy = await load_claim_policy(session, candidate_id, question_includes_internships=True)

        assert policy.include_internships is True
        assert skills["python"].claimable_months(policy) == 32
        assert skills["python"].claimable_years(policy) == 2.6

    async def test_the_candidate_setting_changes_the_answer(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_with_both: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        candidate_id, _, _ = candidate_with_both
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                update(Candidate)
                .where(Candidate.id == candidate_id)
                .values(count_internships_as_experience=True)
            )
        async with tenant_transaction(engine, tenant_a) as session:
            policy = await load_claim_policy(session, candidate_id)
            totals = await compute_career_totals(session, candidate_id, now=NOW)

        assert policy.include_internships is True
        assert "candidate" in policy.reason
        assert totals.claimable_months(policy) == 32

    async def test_the_setting_defaults_to_off_for_a_new_candidate(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        # Opting in is a choice the candidate makes, so the column cannot default to
        # true and quietly inflate every answer.
        candidate_id = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Candidate).values(id=candidate_id, tenant_id=tenant_a, user_id=user_a)
            )
            policy = await load_claim_policy(session, candidate_id)
        assert policy.include_internships is False


class TestPartTime:
    """Paid work alongside something else: a campus job, a paid assistantship."""

    def test_part_time_is_not_in_a_default_claim(self) -> None:
        # Two years of a campus job at ten hours a week is not two years of
        # experience, and a plain "years of experience" question must not say it is.
        skill = _skill(
            full_time=[(month(2024, 6), month(2025, 5))],  # 12
            part_time=[(month(2021, 9), month(2023, 5))],  # 21
        )
        assert skill.part_time_months == 21
        assert skill.claimable_months() == 12

    def test_a_question_about_part_time_work_includes_it(self) -> None:
        skill = _skill(
            full_time=[(month(2024, 6), month(2025, 5))],
            part_time=[(month(2021, 9), month(2022, 5))],  # 9
        )
        policy = ClaimPolicy.resolve(question_includes_part_time=True)
        assert policy.include_part_time is True
        assert "part-time" in policy.reason
        assert skill.claimable_months(policy) == 21

    def test_a_general_work_experience_question_includes_everything_paid(self) -> None:
        skill = _skill(
            full_time=[(month(2024, 6), month(2025, 5))],  # 12
            part_time=[(month(2021, 9), month(2022, 5))],  # 9
            internship=[(month(2023, 6), month(2023, 8))],  # 3
            project=[(month(2020, 1), month(2020, 12))],  # never counted
        )
        policy = ClaimPolicy.general_work_experience()
        assert skill.claimable_months(policy) == 24
        assert policy.buckets == {
            ExperienceBucket.FULL_TIME,
            ExperienceBucket.PART_TIME,
            ExperienceBucket.INTERNSHIP,
        }

    def test_project_time_is_still_never_claimable(self) -> None:
        skill = _skill(project=[(month(2022, 1), month(2022, 12))])
        assert skill.claimable_months(ClaimPolicy.general_work_experience()) == 0

    def test_an_overlapping_part_time_job_is_unioned_not_added(self) -> None:
        # A campus job held during an internship. Adding them would invent months.
        skill = _skill(
            part_time=[(month(2023, 1), month(2023, 12))],  # 12
            internship=[(month(2023, 6), month(2023, 8))],  # 3, inside the above
        )
        policy = ClaimPolicy.general_work_experience()
        assert skill.part_time_months == 12
        assert skill.internship_months == 3
        assert skill.claimable_months(policy) == 12

    def test_the_candidate_setting_does_not_pull_in_part_time(self) -> None:
        # The setting is about internships. Part-time stays question-driven, so a
        # checkbox set months ago cannot answer a question it was not about.
        skill = _skill(
            full_time=[(month(2024, 6), month(2025, 5))],
            part_time=[(month(2021, 9), month(2022, 5))],
            internship=[(month(2023, 6), month(2023, 8))],
        )
        policy = ClaimPolicy.resolve(candidate_setting=True)
        assert policy.include_internships is True
        assert policy.include_part_time is False
        assert skill.claimable_months(policy) == 15

    def test_a_paid_assistantship_can_be_part_time_rather_than_project(self) -> None:
        # RESEARCH and PART_TIME both describe lab work; the difference is whether it
        # was paid, which is the candidate's to state, not a parser's to guess.
        assert ExperienceKind.RESEARCH.bucket is ExperienceBucket.PROJECT
        assert ExperienceKind.PART_TIME.bucket is ExperienceBucket.PART_TIME

    def test_the_four_buckets_are_still_disjoint(self) -> None:
        buckets = [kind.bucket for kind in ExperienceKind]
        assert set(buckets) == set(ExperienceBucket)
        assert len(set(ExperienceBucket)) == 4


@pytest.mark.db
class TestPartTimeAgainstTheGraph:
    @pytest.fixture
    async def candidate_with_assistantship(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> uuid.UUID:
        """A full-time role, a paid assistantship, and an internship — all approved."""
        candidate_id = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Candidate).values(id=candidate_id, tenant_id=tenant_a, user_id=user_a)
            )
            common = {
                "tenant_id": tenant_a,
                "candidate_id": candidate_id,
                "status": ApprovalStatus.APPROVED.value,
                # An approved row names who approved it; the database enforces it.
                "approved_by": user_a,
                "source": GraphSource.RESUME_IMPORT.value,
                "approved_at": dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
            }
            roles = [
                ("Accenture", "Software Engineer", ExperienceKind.PROFESSIONAL, month(2024, 6), None),
                (
                    "University",
                    "Graduate Research Assistant",
                    ExperienceKind.PART_TIME,
                    month(2022, 9),
                    month(2024, 5),
                ),
                ("Startup", "SWE Intern", ExperienceKind.INTERNSHIP, month(2022, 6), month(2022, 8)),
            ]
            for employer, title, kind, start, end in roles:
                experience_id = new_id()
                await session.execute(
                    insert(Experience).values(
                        id=experience_id,
                        employer=employer,
                        title=title,
                        kind=kind.value,
                        start_month=start,
                        end_month=end,
                        **common,
                    )
                )
                fact_id = await create_draft_fact(
                    session,
                    tenant_id=tenant_a,
                    candidate_id=candidate_id,
                    text=f"Python at {employer}",
                    source=GraphSource.RESUME_IMPORT,
                    experience_id=experience_id,
                    skills=[("python", "Python")],
                )
                await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)
        return candidate_id

    async def test_the_summary_keeps_all_four_apart(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_with_assistantship: uuid.UUID
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            totals = await compute_career_totals(session, candidate_with_assistantship, now=NOW)

        # Full-time Jun 2024 - Oct 2026 = 29. Assistantship Sep 2022 - May 2024 = 21.
        # Internship Jun-Aug 2022 = 3. Nothing overlaps, nothing is summed.
        assert totals.full_time_months == 29
        assert totals.part_time_months == 21
        assert totals.internship_months == 3
        assert totals.project_months == 0

        lines = "\n".join(totals.summary_lines())
        assert "full-time:  29 months" in lines
        assert "part-time:  21 months" in lines
        assert "internship: 3 months" in lines
        # 53 is the sum of the three; it must never be printed as one figure.
        assert "53" not in lines

    async def test_the_default_claim_ignores_the_assistantship(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_with_assistantship: uuid.UUID
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            totals = await compute_career_totals(session, candidate_with_assistantship, now=NOW)
            policy = await load_claim_policy(session, candidate_with_assistantship)

        assert policy.include_part_time is False
        assert totals.claimable_months(policy) == 29
        assert totals.claimable_years(policy) == 2.4

    async def test_a_general_work_experience_question_counts_all_paid_work(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_with_assistantship: uuid.UUID
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            totals = await compute_career_totals(session, candidate_with_assistantship, now=NOW)
            policy = await load_claim_policy(
                session, candidate_with_assistantship, question_is_general_work_experience=True
            )

        assert totals.claimable_months(policy) == 53
        assert totals.claimable_years(policy) == 4.4

    async def test_per_skill_buckets_match_the_career_totals(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_with_assistantship: uuid.UUID
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, candidate_with_assistantship, now=NOW)

        python = skills["python"]
        assert python.full_time_months == 29
        assert python.part_time_months == 21
        assert python.internship_months == 3
        assert python.claimable_months() == 29
        assert python.claimable_months(ClaimPolicy.general_work_experience()) == 53
