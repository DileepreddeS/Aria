"""Skill years, against a real graph (PRODUCT_SPEC §7 Phase 1 gate).

"Done when: skill-years computed correctly." Every expected number here is worked out
by hand in the test's own comment, so a failure says which rule broke rather than
which function changed.

The scenario is one career, built once and reused: two professional roles that
overlap, an internship, a research post, and a dated project.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Experience, Project
from aria_core.db.session import tenant_transaction
from aria_core.graph.derive import (
    compute_career_totals,
    compute_skill_experience,
)
from aria_core.graph.facts import approve_fact, create_draft_fact
from aria_core.graph.status import (
    ApprovalStatus,
    ExperienceKind,
    GraphSource,
    ProjectKind,
)
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.taxonomy.loader import load_default_taxonomy

pytestmark = pytest.mark.db

#: Fixed clock, so "Present" is a known month and the expected numbers never drift.
NOW = dt.date(2026, 10, 15)


def month(year: int, month_number: int) -> dt.date:
    return dt.date(year, month_number, 1)


@dataclass
class Career:
    """Ids for the graph built by the :func:`career` fixture."""

    candidate_id: uuid.UUID
    accenture: uuid.UUID
    """Professional, Jun 2024 - Present. 29 months to Oct 2026 inclusive."""
    moonlight: uuid.UUID
    """Contract, Jan 2025 - Jun 2025, overlapping Accenture. Adds no calendar months."""
    internship: uuid.UUID
    """Internship, Jun 2023 - Aug 2023. 3 months."""
    lab: uuid.UUID
    """Research, Sep 2022 - May 2023. 9 months, and not professional time."""
    capstone: uuid.UUID
    """Academic project, Jan 2023 - May 2023. 5 months."""


@pytest.fixture
async def career(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> Career:
    candidate_id = new_id()
    ids = {name: new_id() for name in ("accenture", "moonlight", "internship", "lab", "capstone")}

    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=candidate_id, tenant_id=tenant_a, user_id=user_a))
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
                id=ids["accenture"],
                employer="Accenture",
                title="Software Engineer",
                client="Carvana",
                kind=ExperienceKind.PROFESSIONAL.value,
                start_month=month(2024, 6),
                end_month=None,
                **common,
            )
        )
        await session.execute(
            insert(Experience).values(
                id=ids["moonlight"],
                employer="Side Contract",
                title="Contract Engineer",
                kind=ExperienceKind.CONTRACT.value,
                start_month=month(2025, 1),
                end_month=month(2025, 6),
                **common,
            )
        )
        await session.execute(
            insert(Experience).values(
                id=ids["internship"],
                employer="Startup",
                title="SWE Intern",
                kind=ExperienceKind.INTERNSHIP.value,
                start_month=month(2023, 6),
                end_month=month(2023, 8),
                **common,
            )
        )
        await session.execute(
            insert(Experience).values(
                id=ids["lab"],
                employer="University Lab",
                title="Research Assistant",
                kind=ExperienceKind.RESEARCH.value,
                start_month=month(2022, 9),
                end_month=month(2023, 5),
                **common,
            )
        )
        await session.execute(
            insert(Project).values(
                id=ids["capstone"],
                name="Capstone",
                kind=ProjectKind.ACADEMIC.value,
                start_month=month(2023, 1),
                end_month=month(2023, 5),
                **common,
            )
        )
    return Career(candidate_id=candidate_id, **ids)


async def _approved_fact(
    engine: AsyncEngine,
    tenant_a: TenantId,
    user_a: UserId,
    career: Career,
    *,
    text: str,
    skills: list[tuple[str, str]],
    experience_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> uuid.UUID:
    async with tenant_transaction(engine, tenant_a) as session:
        fact_id = await create_draft_fact(
            session,
            tenant_id=tenant_a,
            candidate_id=career.candidate_id,
            text=text,
            source=GraphSource.RESUME_IMPORT,
            experience_id=experience_id,
            project_id=project_id,
            skills=skills,
        )
        await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)
    return fact_id


class TestOnlyApprovedEvidenceCounts:
    async def test_a_draft_fact_contributes_nothing(
        self, engine: AsyncEngine, tenant_a: TenantId, career: Career
    ) -> None:
        # An imported bullet the candidate has not read is not evidence of anything.
        async with tenant_transaction(engine, tenant_a) as session:
            await create_draft_fact(
                session,
                tenant_id=tenant_a,
                candidate_id=career.candidate_id,
                text="Built React components",
                source=GraphSource.RESUME_IMPORT,
                experience_id=career.accenture,
                skills=[("react", "React")],
            )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)
        assert skills == {}

    async def test_approving_the_fact_makes_the_months_appear(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Built React components",
            skills=[("react", "React")],
            experience_id=career.accenture,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)

        # Jun 2024 through Oct 2026 inclusive = 29 months = 2.4 years truncated.
        assert skills["react"].full_time_months == 29
        assert skills["react"].claimable_years() == 2.4

    async def test_a_fact_on_an_unapproved_experience_contributes_no_months(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        # The fact is approved but the job it hangs on is not, so there are no dates
        # to trust yet. It still counts as a mention.
        draft_experience = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Experience).values(
                    id=draft_experience,
                    tenant_id=tenant_a,
                    candidate_id=career.candidate_id,
                    employer="Unconfirmed",
                    title="Engineer",
                    kind=ExperienceKind.PROFESSIONAL.value,
                    start_month=month(2020, 1),
                    end_month=month(2024, 1),
                    status=ApprovalStatus.DRAFT.value,
                    source=GraphSource.RESUME_IMPORT.value,
                )
            )
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Wrote Go services",
            skills=[("golang", "Go")],
            experience_id=draft_experience,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)

        assert skills["golang"].full_time_months == 0
        assert skills["golang"].fact_count == 1


class TestConcurrentRolesDoNotDoubleCount:
    async def test_two_overlapping_roles_count_the_calendar_once(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        # Accenture Jun 2024 - Oct 2026 (29 months) and a contract Jan-Jun 2025
        # (6 months) wholly inside it. The calendar still only has 29 months.
        for experience_id in (career.accenture, career.moonlight):
            await _approved_fact(
                engine,
                tenant_a,
                user_a,
                career,
                text=f"Used TypeScript on {experience_id}",
                skills=[("typescript", "TypeScript")],
                experience_id=experience_id,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)

        assert skills["typescript"].full_time_months == 29
        assert skills["typescript"].fact_count == 2

    async def test_separate_full_time_roles_with_a_gap_add_up(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        # Accenture Jun 2024 - Oct 2026 (29) and the contract inside it (0 extra).
        # The internship is a different bucket and is covered separately.
        for experience_id in (career.accenture, career.moonlight):
            await _approved_fact(
                engine,
                tenant_a,
                user_a,
                career,
                text=f"Python work at {experience_id}",
                skills=[("python", "Python")],
                experience_id=experience_id,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)

        assert skills["python"].full_time_months == 29
        assert skills["python"].claimable_years() == 2.4


class TestProfessionalAndProjectTimeStaySeparate:
    async def test_project_months_are_not_professional_months(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        # The capstone is 5 months of PyTorch. It is not 5 months of employment, and
        # a new grad's resume must not imply that it is.
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Trained a model for the capstone",
            skills=[("pytorch", "PyTorch")],
            project_id=career.capstone,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)

        assert skills["pytorch"].full_time_months == 0
        assert skills["pytorch"].project_months == 5
        assert skills["pytorch"].has_full_time_evidence is False

    async def test_research_time_is_project_time_not_full_time(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        # Sep 2022 - May 2023 is 9 months in a lab. Real, dated, and not a job.
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Published a segmentation paper",
            skills=[("image_segmentation", "image segmentation")],
            experience_id=career.lab,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)

        assert skills["image_segmentation"].full_time_months == 0
        assert skills["image_segmentation"].project_months == 9

    async def test_a_skill_used_in_both_reports_both(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        for kwargs in ({"experience_id": career.accenture}, {"project_id": career.capstone}):
            await _approved_fact(
                engine,
                tenant_a,
                user_a,
                career,
                text=f"Python in {kwargs}",
                skills=[("python", "Python")],
                **kwargs,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)

        assert skills["python"].full_time_months == 29
        assert skills["python"].project_months == 5
        # Separate fields, never summed: the describe() string shows them apart.
        assert "2.4y full-time" in skills["python"].describe()
        assert "0.4y projects" in skills["python"].describe()


class TestImpliedYearsAreNeverDirectYears:
    async def test_react_does_not_give_javascript_direct_months(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Built the front end in React",
            skills=[("react", "React")],
            experience_id=career.accenture,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(
                session, career.candidate_id, taxonomy=load_default_taxonomy(), now=NOW
            )

        # React implies JavaScript for matching. It does not make "2.4 years of
        # JavaScript" a true sentence, so the direct figure stays zero.
        assert skills["react"].full_time_months == 29
        assert skills["javascript"].full_time_months == 0
        assert skills["javascript"].implied_full_time_months == 29
        assert skills["javascript"].claimable_years() == 0.0

    async def test_implied_months_are_absent_without_a_taxonomy(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Built the front end in React",
            skills=[("react", "React")],
            experience_id=career.accenture,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)
        assert "javascript" not in skills

    async def test_a_direct_claim_is_not_overwritten_by_an_implication(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        # JavaScript has its own 3 internship months, plus 29 implied from React.
        # Three figures, three meanings, none of them merged.
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Wrote vanilla JS during the internship",
            skills=[("javascript", "JavaScript")],
            experience_id=career.internship,
        )
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Built the front end in React",
            skills=[("react", "React")],
            experience_id=career.accenture,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(
                session, career.candidate_id, taxonomy=load_default_taxonomy(), now=NOW
            )

        assert skills["javascript"].internship_months == 3
        assert skills["javascript"].full_time_months == 0
        assert skills["javascript"].implied_full_time_months == 29

    async def test_implication_does_not_flow_from_projects(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        # Only professional spans are propagated: an implied *project* year would be
        # two inferences stacked on each other.
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Used Next.js for the capstone site",
            skills=[("nextjs", "Next.js")],
            project_id=career.capstone,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(
                session, career.candidate_id, taxonomy=load_default_taxonomy(), now=NOW
            )
        assert skills["nextjs"].project_months == 5
        assert skills.get("react") is None or skills["react"].implied_full_time_months == 0


class TestRecency:
    async def test_last_used_is_the_current_month_for_an_ongoing_role(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Still writing Python",
            skills=[("python", "Python")],
            experience_id=career.accenture,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)
        assert skills["python"].last_used == month(2026, 10)

    async def test_last_used_is_the_end_month_for_a_finished_role(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Wrote Java at the internship",
            skills=[("java", "Java")],
            experience_id=career.internship,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)
        assert skills["java"].last_used == month(2023, 8)


class TestCareerTotals:
    async def test_professional_and_internship_and_project_time(
        self, engine: AsyncEngine, tenant_a: TenantId, career: Career
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            totals = await compute_career_totals(session, career.candidate_id, now=NOW)

        # Full-time = Accenture (Jun 2024 - Oct 2026, 29) + the contract inside it
        # (0 extra). The internship is NOT in here: the buckets are disjoint, so a
        # summary can print all three without counting its 3 months twice.
        assert totals.full_time_months == 29
        assert totals.internship_months == 3
        # The lab (9) and the capstone (Jan-May 2023, 5) overlap, so 9 months of
        # calendar, not 14.
        assert totals.project_months == 9
        assert totals.first_working_month == month(2023, 6)

    async def test_an_empty_graph_totals_zero_rather_than_failing(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        candidate_id = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Candidate).values(id=candidate_id, tenant_id=tenant_a, user_id=user_a)
            )
            totals = await compute_career_totals(session, candidate_id, now=NOW)

        assert totals.full_time_months == 0
        assert totals.internship_months == 0
        assert totals.project_months == 0
        assert totals.claimable_years() == 0.0
        assert totals.first_working_month is None
        assert totals.skills == {}

    async def test_experience_counts_towards_totals_before_its_bullets_exist(
        self, engine: AsyncEngine, tenant_a: TenantId, career: Career
    ) -> None:
        # A job the candidate approved is time they worked, even before the bullets
        # are written. Skills, by contrast, need facts.
        async with tenant_transaction(engine, tenant_a) as session:
            totals = await compute_career_totals(session, career.candidate_id, now=NOW)
        assert totals.full_time_months == 29
        assert totals.internship_months == 3
        assert totals.skills == {}


class TestDeterminism:
    async def test_two_computations_agree(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        await _approved_fact(
            engine,
            tenant_a,
            user_a,
            career,
            text="Python everywhere",
            skills=[("python", "Python")],
            experience_id=career.accenture,
        )
        async with tenant_transaction(engine, tenant_a) as session:
            first = await compute_skill_experience(session, career.candidate_id, now=NOW)
            second = await compute_skill_experience(session, career.candidate_id, now=NOW)
        assert first == second

    async def test_the_result_is_sorted_by_canonical_id(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, career: Career
    ) -> None:
        for canonical, surface in (("typescript", "TypeScript"), ("docker", "Docker"), ("react", "React")):
            await _approved_fact(
                engine,
                tenant_a,
                user_a,
                career,
                text=f"Used {surface}",
                skills=[(canonical, surface)],
                experience_id=career.accenture,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, career.candidate_id, now=NOW)
        assert list(skills) == sorted(skills)
