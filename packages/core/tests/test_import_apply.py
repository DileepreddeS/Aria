"""Applying an import plan writes drafts, and only drafts.

This is the end of the import path, against a real database. What it proves:

* every row lands as ``draft`` and carries its upload and line, so the approval view
  can show the candidate the document behind each claim;
* a role the resume did not date is **held back** rather than given an invented month,
  and its bullets wait with it;
* importing a second resume reuses the role already in the graph instead of doubling
  the calendar, and proposes rather than performs a merge of similar bullets;
* a wording the candidate has already rejected is never proposed again;
* the audit event records counts and no imported text.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.audit.log import read_chain
from aria_core.db.models import (
    Achievement,
    Candidate,
    DeclaredSkill,
    Education,
    Experience,
    ProfileField,
    Project,
    Upload,
)
from aria_core.db.session import tenant_transaction
from aria_core.graph.facts import (
    approve_fact,
    approved_facts,
    draft_facts,
    get_fact,
    merge_facts,
    reject_fact,
    rejected_facts,
)
from aria_core.graph.status import ApprovalStatus
from aria_core.importer.apply import ImportOutcome, apply_import_plan
from aria_core.importer.resume import parse_resume_text
from aria_core.schemas.audit import AuditAction
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_core.taxonomy.model import SkillTaxonomy

pytestmark = pytest.mark.db

TODAY = dt.date(2026, 10, 8)

RESUME_ONE = """Jordan A. Rivera
jordan.rivera@example.com | github.com/jordan-rivera-example

EXPERIENCE
Accenture (client: Carvana) - Software Engineer
Jun 2024 - Present
- Built 20+ React components used by 4 teams
- Cut p95 API latency by 30% by adding Redis caching

EDUCATION
BS Computer Science, Cascadia State University
Aug 2020 - May 2024

TECHNICAL SKILLS
Languages: Python, TypeScript
"""

#: The same career, written for a different kind of role. The employer and months
#: match; one bullet is reworded and one is new.
RESUME_TWO = """Jordan A. Rivera
jordan.rivera@example.com

EXPERIENCE
Accenture - Software Engineer
Jun 2024 - Present
- Developed 20+ React components used by 4 teams
- Wrote the deployment pipeline for three services

PROJECTS
ARIA - a job-search agent | github.com/jordan-rivera-example/aria
- Built a deterministic resume importer in Python
"""

UNDATED = """EXPERIENCE
Acme Inc - Software Engineering Intern
Summer 2023
- Shipped an internal tool used by 30 people
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


async def make_upload(engine: AsyncEngine, tenant_id: TenantId, candidate_id: uuid.UUID) -> uuid.UUID:
    """A real uploads row, because every imported draft has a foreign key to one.

    The link is the point: the approval view shows the candidate the document and the
    line behind each claim, which only works if the upload is really there.
    """
    upload_id = new_id()
    async with tenant_transaction(engine, tenant_id) as session:
        await session.execute(
            insert(Upload).values(
                id=upload_id,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                original_filename="resume.pdf",
                detected_type="pdf",
                size_bytes=4096,
                # Unique per upload: the uploads table has a unique index on
                # (candidate_id, sha256), which is how a byte-identical re-upload is
                # recognised. A fixed digest here made the second upload a duplicate.
                sha256=upload_id.hex * 2,
                quarantine_name=f"{upload_id}.bin",
                scan_status="not_scanned",
                scan_detail="dev scanner",
                parse_status="parsed",
            )
        )
    return upload_id


async def run_import(
    engine: AsyncEngine,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    text: str,
    taxonomy: SkillTaxonomy,
    *,
    upload_id: uuid.UUID | None = None,
    actor_id: str | None = None,
) -> ImportOutcome:
    plan = parse_resume_text(text, taxonomy, today=TODAY)
    if upload_id is None:
        upload_id = await make_upload(engine, tenant_id, candidate_id)
    async with tenant_transaction(engine, tenant_id) as session:
        return await apply_import_plan(
            session,
            plan,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            upload_id=upload_id,
            actor_id=actor_id,
        )


class TestEverythingLandsAsADraft:
    async def test_the_rows_are_written(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        outcome = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        assert len(outcome.experience_ids) == 1
        assert len(outcome.education_ids) == 1
        assert len(outcome.fact_ids) == 2
        assert len(outcome.declared_skill_ids) == 2
        assert len(outcome.profile_field_ids) >= 2

    @pytest.mark.parametrize(
        "model", [Experience, Education, Project, ProfileField, DeclaredSkill], ids=lambda m: m.__name__
    )
    async def test_no_row_is_approved(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
        model: type[Experience] | type[Education] | type[Project] | type[ProfileField] | type[DeclaredSkill],
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            statuses = (
                await session.execute(select(model.status).where(model.candidate_id == candidate_id))
            ).scalars()
        assert all(status == ApprovalStatus.DRAFT.value for status in statuses)

    async def test_the_facts_are_drafts_and_nothing_is_approved(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            assert len(await draft_facts(session, candidate_id)) == 2
            assert await approved_facts(session, candidate_id) == []

    async def test_every_fact_points_back_at_the_upload_and_the_line(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        upload_id = await make_upload(engine, tenant_a, candidate_id)
        outcome = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy, upload_id=upload_id)
        async with tenant_transaction(engine, tenant_a) as session:
            for fact_id in outcome.fact_ids:
                view = await get_fact(session, fact_id)
                assert view.source_upload_id == upload_id
                assert view.source_line is not None
                assert view.text in RESUME_ONE

    async def test_a_bullet_is_anchored_to_its_role(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        outcome = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        (experience_id,) = outcome.experience_ids
        async with tenant_transaction(engine, tenant_a) as session:
            for fact_id in outcome.fact_ids:
                assert (await get_fact(session, fact_id)).experience_id == experience_id

    async def test_the_skills_and_metrics_arrive_with_the_fact(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        outcome = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            # An approved fact is needed to read skills back through the repository.
            for fact_id in outcome.fact_ids:
                await approve_fact(session, tenant_id=tenant_a, fact_id=fact_id, approved_by=user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            from aria_core.graph.facts import approved_skill_usage

            usage = await approved_skill_usage(session, candidate_id)
        assert "react" in {canonical for canonical, _, _ in usage}
        assert "redis" in {canonical for canonical, _, _ in usage}


class TestUndatedRolesAreHeldBack:
    async def test_the_role_is_not_written(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        # "Summer 2023" is a season. Writing June would be inventing the month that
        # every years-of-experience figure is then built on.
        outcome = await run_import(engine, tenant_a, candidate_id, UNDATED, taxonomy)
        assert outcome.experience_ids == ()
        assert len(outcome.held_back) == 1

    async def test_the_question_says_what_is_missing(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        outcome = await run_import(engine, tenant_a, candidate_id, UNDATED, taxonomy)
        (label, question) = outcome.held_back[0]
        assert "Intern" in label
        assert "start and end" in question.prompt
        assert "season" in question.detail

    async def test_its_bullets_wait_with_it(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        # A bullet approved without its role has no employer and no dates behind it.
        outcome = await run_import(engine, tenant_a, candidate_id, UNDATED, taxonomy)
        assert outcome.fact_ids == ()


class TestASecondResume:
    async def test_the_role_is_reused_not_duplicated(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        first = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        second = await run_import(engine, tenant_a, candidate_id, RESUME_TWO, taxonomy)

        assert second.experience_ids == ()
        assert second.reused_experience_ids == first.experience_ids

        async with tenant_transaction(engine, tenant_a) as session:
            roles = (
                (await session.execute(select(Experience.id).where(Experience.candidate_id == candidate_id)))
                .scalars()
                .all()
            )
        assert len(roles) == 1

    async def test_a_reworded_bullet_is_proposed_as_a_merge_not_applied(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        second = await run_import(engine, tenant_a, candidate_id, RESUME_TWO, taxonomy)

        (proposal,) = second.merge_proposals
        assert "Developed 20+ React components" in proposal.new_text
        assert "Built 20+ React components" in proposal.existing_text
        # Both rows exist: the candidate reads the two wordings and keeps one.
        async with tenant_transaction(engine, tenant_a) as session:
            assert await get_fact(session, proposal.new_fact_id) is not None
            assert await get_fact(session, proposal.existing_fact_id) is not None

    async def test_a_new_bullet_still_arrives(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        await run_import(engine, tenant_a, candidate_id, RESUME_TWO, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            texts = {fact.text for fact in await draft_facts(session, candidate_id)}
        assert "Wrote the deployment pipeline for three services" in texts

    async def test_an_identical_bullet_is_skipped_without_asking(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        again = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        assert again.duplicates_skipped == 2
        assert again.fact_ids == ()
        assert again.merge_proposals == ()

    async def test_a_declared_skill_is_not_declared_twice(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        again = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        assert again.declared_skill_ids == ()


class TestRejectedWordingsAreNotProposedAgain:
    async def test_a_rejected_bullet_does_not_come_back(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # Rejected facts are kept precisely so an import can check against them.
        # Re-proposing one with every upload is how a review queue stops being read.
        first = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            for fact_id in first.fact_ids:
                await reject_fact(session, tenant_id=tenant_a, fact_id=fact_id, rejected_by=user_a)

        again = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        assert again.rejected_again == 2
        assert again.fact_ids == ()

        async with tenant_transaction(engine, tenant_a) as session:
            assert await draft_facts(session, candidate_id) == []
            assert len(await rejected_facts(session, candidate_id)) == 2


class TestMerging:
    async def test_a_merged_fact_points_at_the_survivor(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        second = await run_import(engine, tenant_a, candidate_id, RESUME_TWO, taxonomy)
        (proposal,) = second.merge_proposals

        async with tenant_transaction(engine, tenant_a) as session:
            await merge_facts(
                session,
                tenant_id=tenant_a,
                keep_fact_id=proposal.existing_fact_id,
                merge_fact_id=proposal.new_fact_id,
                actor=user_a,
            )

        async with tenant_transaction(engine, tenant_a) as session:
            merged = await get_fact(session, proposal.new_fact_id)
            survivor = await get_fact(session, proposal.existing_fact_id)
        # Nothing is deleted: a resume built from the merged wording still explains.
        assert merged.status is ApprovalStatus.REJECTED
        assert survivor.status is ApprovalStatus.DRAFT

    async def test_the_merge_is_audited(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        second = await run_import(engine, tenant_a, candidate_id, RESUME_TWO, taxonomy)
        (proposal,) = second.merge_proposals
        async with tenant_transaction(engine, tenant_a) as session:
            await merge_facts(
                session,
                tenant_id=tenant_a,
                keep_fact_id=proposal.existing_fact_id,
                merge_fact_id=proposal.new_fact_id,
                actor=user_a,
            )
        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)
        merges = [event for event in events if event.action == AuditAction.FACT_MERGED.value]
        assert len(merges) == 1
        assert merges[0].payload["merged_into"] == str(proposal.existing_fact_id)

    async def test_a_fact_cannot_be_merged_into_itself(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        user_a: UserId,
        taxonomy: SkillTaxonomy,
    ) -> None:
        outcome = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        fact_id = outcome.fact_ids[0]
        async with tenant_transaction(engine, tenant_a) as session:
            with pytest.raises(ValueError, match="merged into itself"):
                await merge_facts(
                    session,
                    tenant_id=tenant_a,
                    keep_fact_id=fact_id,
                    merge_fact_id=fact_id,
                    actor=user_a,
                )


class TestTheAuditTrail:
    async def test_an_import_is_recorded(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy, actor_id="jordan")
        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)
        imports = [event for event in events if event.action == AuditAction.GRAPH_IMPORTED.value]
        assert len(imports) == 1
        assert imports[0].payload["facts"] == 2
        assert imports[0].payload["experiences_created"] == 1

    async def test_the_payload_carries_counts_and_no_resume_text(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        # Audit payloads are S0 (SECURITY.md §3): an event may record that two facts
        # were proposed and must never record what they say.
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)
        (event,) = [e for e in events if e.action == AuditAction.GRAPH_IMPORTED.value]
        for key, value in event.payload.items():
            assert isinstance(value, int) or key == "source", f"{key} carries {value!r}"
        serialised = str(event.payload)
        assert "React" not in serialised
        assert "jordan.rivera@example.com" not in serialised


class TestTheApprovalQueueStaysReadable:
    async def test_a_repeated_profile_field_is_not_proposed_again(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # Both resumes carry the same name and email. Proposing them twice fills the
        # queue with decisions already made, and a queue full of noise stops being
        # read -- which is how an unreviewed draft eventually reaches a resume.
        first = await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        second = await run_import(engine, tenant_a, candidate_id, RESUME_TWO, taxonomy)
        assert first.profile_field_ids
        assert second.profile_field_ids == ()

    async def test_a_changed_value_is_still_proposed(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        # A newer email is news. Only an exact repeat is skipped.
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        moved = RESUME_ONE.replace("jordan.rivera@example.com", "new.address@example.test")
        again = await run_import(engine, tenant_a, candidate_id, moved, taxonomy)
        async with tenant_transaction(engine, tenant_a) as session:
            values = (
                (
                    await session.execute(
                        select(ProfileField.value).where(
                            ProfileField.candidate_id == candidate_id,
                            ProfileField.kind == "email",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert set(values) == {"jordan.rivera@example.com", "new.address@example.test"}
        assert len(again.profile_field_ids) == 1

    async def test_the_merge_question_points_at_the_line(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
    ) -> None:
        await run_import(engine, tenant_a, candidate_id, RESUME_ONE, taxonomy)
        second = await run_import(engine, tenant_a, candidate_id, RESUME_TWO, taxonomy)
        (proposal,) = second.merge_proposals
        bullet = "- Developed 20+ React components used by 4 teams"
        expected = RESUME_TWO.splitlines().index(bullet) + 1
        assert proposal.source_line == expected
        assert f"line {expected}" in str(proposal.as_question())


#: Every kind of entry, so a second import of it proves each one is reused.
EVERY_KIND = """Jordan A. Rivera
jordan.rivera@example.com

EXPERIENCE
Nordvale Consulting - Software Engineer
Feb 2024 - Jul 2025
- Reduced the nightly batch from six hours to forty minutes

EDUCATION
M.S. Computer Science, Cascadia State University
Aug 2025 - Dec 2026

PROJECTS
VERA-RAG | Python, FastAPI
- Built a closed-loop RAG system with hybrid retrieval

AWARDS
Dean's List, Cascadia State University
"""


class TestImportingTheSameResumeTwice:
    """The second import adds nothing: roles, degrees, projects and awards are reused."""

    async def _twice(
        self, engine: AsyncEngine, tenant: TenantId, candidate: uuid.UUID, taxonomy: SkillTaxonomy, text: str
    ) -> tuple[ImportOutcome, ImportOutcome]:
        upload_id = await make_upload(engine, tenant, candidate)
        first = await run_import(engine, tenant, candidate, text, taxonomy, upload_id=upload_id)
        second = await run_import(engine, tenant, candidate, text, taxonomy, upload_id=upload_id)
        return first, second

    async def test_nothing_new_is_written_the_second_time(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        first, second = await self._twice(engine, tenant_a, candidate_id, taxonomy, EVERY_KIND)
        assert (len(first.education_ids), len(first.project_ids), len(first.achievement_ids)) == (1, 1, 1)
        assert (second.experience_ids, second.education_ids, second.project_ids, second.achievement_ids) == (
            (),
            (),
            (),
            (),
        )
        assert second.reused_education_ids == first.education_ids
        assert second.reused_project_ids == first.project_ids
        assert second.reused_achievement_ids == first.achievement_ids
        assert second.fact_ids == ()

    @pytest.mark.parametrize("model", [Experience, Education, Project, Achievement], ids=lambda m: m.__name__)
    async def test_one_row_of_each(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        taxonomy: SkillTaxonomy,
        model: type[Experience] | type[Education] | type[Project] | type[Achievement],
    ) -> None:
        await self._twice(engine, tenant_a, candidate_id, taxonomy, EVERY_KIND)
        async with tenant_transaction(engine, tenant_a) as session:
            rows = (await session.execute(select(model.id).where(model.candidate_id == candidate_id))).all()
        assert len(rows) == 1

    async def test_a_new_bullet_lands_under_the_reused_project(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        first = await run_import(engine, tenant_a, candidate_id, EVERY_KIND, taxonomy)
        bullet = "- Built a closed-loop RAG system with hybrid retrieval\n"
        more = EVERY_KIND.replace(bullet, bullet + "- Served Llama-3-8B with structured decoding\n")
        second = await run_import(engine, tenant_a, candidate_id, more, taxonomy)
        assert second.reused_project_ids == first.project_ids
        assert len(second.fact_ids) == 1
        async with tenant_transaction(engine, tenant_a) as session:
            new = await get_fact(session, second.fact_ids[0])
        assert new.project_id == first.project_ids[0]

    async def test_the_same_project_name_over_other_months_is_another_project(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy
    ) -> None:
        dated = EVERY_KIND.replace(
            "VERA-RAG | Python, FastAPI", "VERA-RAG | Python, FastAPI | Jan 2025 - Jun 2025"
        )
        first = await run_import(engine, tenant_a, candidate_id, EVERY_KIND, taxonomy)
        second = await run_import(engine, tenant_a, candidate_id, dated, taxonomy)
        assert len(first.project_ids) == len(second.project_ids) == 1
        assert second.reused_project_ids == ()
