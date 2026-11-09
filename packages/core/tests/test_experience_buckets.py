"""The four experience buckets: what a graph summary reports, side by side, never summed.

These used to sit beside ``ClaimPolicy``, which turned buckets into a years claim. That
decision now comes from the candidate's per-role answer (aria_core.graph.counted_years;
test_counted_years.py, test_years_answer.py). The buckets stay as a description of the
graph, and these are the properties that keep that description honest.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Experience
from aria_core.db.session import tenant_transaction
from aria_core.graph.derive import SkillExperience, compute_career_totals, compute_skill_experience
from aria_core.graph.facts import approve_fact, create_draft_fact
from aria_core.graph.months import MonthSpan
from aria_core.graph.status import ApprovalStatus, ExperienceBucket, ExperienceKind, GraphSource
from aria_core.schemas.identity import TenantId, UserId, new_id

NOW = dt.date(2026, 10, 15)


def month(year: int, month_number: int) -> dt.date:
    return dt.date(year, month_number, 1)


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


class TestBucketsAreDisjoint:
    def test_the_buckets_cover_every_kind_exactly_once(self) -> None:
        mapping = {kind: kind.bucket for kind in ExperienceKind}
        assert set(mapping.values()) == set(ExperienceBucket)
        assert len(mapping) == len(ExperienceKind)
        assert len(set(ExperienceBucket)) == 4

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


@pytest.mark.db
class TestAgainstTheGraph:
    @pytest.fixture
    async def candidate_id(self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
        """A full-time role, a paid assistantship and an internship, all approved, all Python."""
        candidate_id = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Candidate).values(id=candidate_id, tenant_id=tenant_a, user_id=user_a)
            )
            common = {
                "tenant_id": tenant_a,
                "candidate_id": candidate_id,
                "status": ApprovalStatus.APPROVED.value,
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
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            totals = await compute_career_totals(session, candidate_id, now=NOW)

        # Full-time Jun 2024 - Oct 2026 = 29. Assistantship Sep 2022 - May 2024 = 21.
        # Internship Jun-Aug 2022 = 3. Nothing overlaps, nothing is summed.
        assert (totals.full_time_months, totals.part_time_months) == (29, 21)
        assert (totals.internship_months, totals.project_months) == (3, 0)
        lines = "\n".join(totals.summary_lines())
        assert "full-time:  29 months" in lines
        assert "part-time:  21 months" in lines
        assert "internship: 3 months" in lines
        # 53 is the sum of the three; it must never be printed as one figure.
        assert "53" not in lines

    async def test_per_skill_buckets_match_the_career_totals(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            python = (await compute_skill_experience(session, candidate_id, now=NOW))["python"]
        assert (python.full_time_months, python.part_time_months, python.internship_months) == (29, 21, 3)
