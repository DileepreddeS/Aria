"""An undated project imports; an undated role does not.

The asymmetry is deliberate and it is worth stating plainly, because it looks like an
inconsistency until you ask what each row is for.

A **role** is a claim about time. "Software Engineer at Accenture" with no months
cannot be written at all — ``experiences.start_month`` is NOT NULL precisely because
every years-of-experience figure is built on it, and a role with an invented month
poisons all of them. So it is held back with a question.

A **project** is a claim about work. "Built a compiler for a course" is complete and
useful with no dates attached: it is evidence that the candidate has done the thing,
and the resume can show it. Its months are nullable, it contributes **zero project
months**, and its bullets import normally. Holding it back would lose real evidence to
protect a number nobody was going to quote.

Same for a season: "Spring 2026" is not a month, so a project dated that way imports
with no span rather than being held back.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Project, Upload
from aria_core.db.session import tenant_transaction
from aria_core.graph.derive import compute_career_totals, compute_skill_experience
from aria_core.graph.facts import approve_fact, draft_facts
from aria_core.graph.status import ApprovalStatus, ProjectKind
from aria_core.importer.apply import apply_import_plan
from aria_core.importer.resume import parse_resume_text
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_core.taxonomy.model import SkillTaxonomy

TODAY = dt.date(2026, 10, 8)

#: One project with no dates at all, one dated by season, one properly dated.
PROJECTS = """PROJECTS
Toy Compiler - a compiler for a university course
- Wrote a recursive-descent parser in C++ and a bytecode VM

Campus Scheduler
Spring 2026
- Built a React scheduling app used by 300 students

Telemetry Pipeline
Jan 2025 - Mar 2025
- Shipped a Kafka ingest pipeline in Python handling 5,000 req/s
"""

#: The same three shapes, but as roles. None of these may be written undated.
ROLES = """EXPERIENCE
Acme Inc - Software Engineer
- Did the work

Beta Corp - Software Engineering Intern
Spring 2026
- Did the other work
"""


@pytest.fixture(scope="module")
def taxonomy() -> SkillTaxonomy:
    return load_default_taxonomy()


class TestTheParserKeepsUndatedProjects:
    def test_all_three_projects_are_proposed(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_resume_text(PROJECTS, taxonomy, today=TODAY)
        assert [project.name for project in plan.projects] == [
            "Toy Compiler",
            "Campus Scheduler",
            "Telemetry Pipeline",
        ]

    def test_an_undated_project_has_no_span_and_no_question(self, taxonomy: SkillTaxonomy) -> None:
        # Unlike a role: there is nothing to ask about, because nothing is blocked.
        plan = parse_resume_text(PROJECTS, taxonomy, today=TODAY)
        compiler = plan.projects[0]
        assert compiler.span is None
        assert compiler.date_text == ""
        assert compiler.questions == ()

    def test_a_season_dated_project_has_no_span_and_no_question(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_resume_text(PROJECTS, taxonomy, today=TODAY)
        scheduler = plan.projects[1]
        assert scheduler.span is None
        assert scheduler.questions == ()

    def test_a_properly_dated_project_keeps_its_months(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_resume_text(PROJECTS, taxonomy, today=TODAY)
        pipeline = plan.projects[2]
        assert pipeline.span is not None
        assert pipeline.span.start == dt.date(2025, 1, 1)
        assert pipeline.span.end == dt.date(2025, 3, 1)

    def test_every_project_keeps_its_bullets(self, taxonomy: SkillTaxonomy) -> None:
        # This is the point of not holding them back: the evidence arrives.
        plan = parse_resume_text(PROJECTS, taxonomy, today=TODAY)
        assert [len(project.facts) for project in plan.projects] == [2, 1, 1]

    def test_the_bullets_are_tagged_with_their_skills(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_resume_text(PROJECTS, taxonomy, today=TODAY)
        tagged = {skill.canonical for fact in plan.projects[0].facts for skill in fact.skills}
        assert "cpp" in tagged

    def test_a_metric_in_an_undated_project_still_counts(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_resume_text(PROJECTS, taxonomy, today=TODAY)
        scheduler = plan.projects[1]
        assert [metric.exact_value for fact in scheduler.facts for metric in fact.metrics] == ["300"]

    def test_an_undated_role_is_the_opposite_case(self, taxonomy: SkillTaxonomy) -> None:
        # Side by side with the projects above: a role without months cannot be
        # written, so both of these carry a question.
        plan = parse_resume_text(ROLES, taxonomy, today=TODAY)
        assert [role.span for role in plan.experiences] == [None, None]
        assert all(role.questions for role in plan.experiences)


@pytest.mark.db
class TestUndatedProjectsReachTheGraph:
    @pytest.fixture
    async def candidate_id(self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
        created = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
        return created

    async def _import(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
        text: str = PROJECTS,
    ) -> uuid.UUID:
        upload_id = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Upload).values(
                    id=upload_id,
                    tenant_id=tenant_a,
                    candidate_id=candidate_id,
                    original_filename="projects.pdf",
                    detected_type="pdf",
                    size_bytes=2048,
                    sha256=upload_id.hex * 2,
                    quarantine_name=f"{upload_id}.bin",
                    scan_status="not_scanned",
                    scan_detail="dev scanner",
                    parse_status="parsed",
                )
            )
        plan = parse_resume_text(text, taxonomy, today=TODAY)
        async with tenant_transaction(engine, tenant_a) as session:
            outcome = await apply_import_plan(
                session,
                plan,
                tenant_id=tenant_a,
                candidate_id=candidate_id,
                upload_id=upload_id,
            )
        assert outcome.held_back == ()
        return upload_id

    async def _approve_projects(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, user_a: UserId
    ) -> None:
        """Approve the project rows directly.

        There is no approve_project() yet — the approval flow for graph entities
        arrives with the API — and derived months only count approved rows, which is
        the behaviour under test here.
        """
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                update(Project)
                .where(Project.candidate_id == candidate_id)
                .values(status=ApprovalStatus.APPROVED.value, approved_by=user_a, approved_at=utc_now())
            )

    async def test_all_three_rows_are_written(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await self._import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            rows = (
                await session.execute(
                    select(Project.name, Project.start_month, Project.end_month)
                    .where(Project.candidate_id == candidate_id)
                    .order_by(Project.source_line)
                )
            ).all()
        assert [row[0] for row in rows] == ["Toy Compiler", "Campus Scheduler", "Telemetry Pipeline"]
        assert rows[0][1] is None and rows[0][2] is None
        assert rows[1][1] is None and rows[1][2] is None
        assert rows[2][1] == dt.date(2025, 1, 1)

    async def test_their_facts_are_written_too(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await self._import(engine, tenant_a, candidate_id, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            facts = await draft_facts(session, candidate_id)
        assert len(facts) == 4
        assert all(fact.project_id is not None for fact in facts)

    async def test_an_undated_project_contributes_zero_months(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # C++ appears only in the undated project, so its project months are zero
        # while the skill itself is still on the graph. Python appears in the dated
        # one and gets its three months.
        await self._import(engine, tenant_a, candidate_id, taxonomy)
        await self._approve_projects(engine, tenant_a, candidate_id, user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            for fact in await draft_facts(session, candidate_id):
                await approve_fact(session, tenant_id=tenant_a, fact_id=fact.fact_id, approved_by=user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            skills = await compute_skill_experience(session, candidate_id, now=TODAY)

        assert skills["cpp"].project_months == 0
        assert skills["python"].project_months == 3

    async def test_career_totals_count_only_the_dated_project(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await self._import(engine, tenant_a, candidate_id, taxonomy)
        await self._approve_projects(engine, tenant_a, candidate_id, user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            totals = await compute_career_totals(session, candidate_id, now=TODAY)

        # Only Jan-Mar 2025 is datable, so three months of project time and no
        # employment of any kind.
        assert totals.project_months == 3
        assert totals.full_time_months == 0
        assert totals.internship_months == 0

    async def test_the_kind_is_still_proposed(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # A course project is academic whether or not it is dated.
        plan = parse_resume_text(PROJECTS, taxonomy, today=TODAY)
        assert plan.projects[0].kind is ProjectKind.ACADEMIC
