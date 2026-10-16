"""The Resume Engine's input holds approved rows only (DECISIONS 2026-10-16).

The engine never reads the database, so these tests are the whole guarantee that a draft,
a rejected bullet, or a true bullet under a role the candidate has not approved can never
reach a resume.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Experience, ProfileField, Upload
from aria_core.db.session import tenant_transaction
from aria_core.graph.entities import EntityKind, approve_entity
from aria_core.graph.facts import approve_fact, draft_facts, reject_fact
from aria_core.graph.snapshot import build_candidate_snapshot
from aria_core.importer.apply import apply_import_plan
from aria_core.importer.resume import parse_resume_text
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotContact, SnapshotFact
from aria_core.taxonomy.loader import load_default_taxonomy

TODAY = dt.date(2026, 10, 16)
RESUME = """Jordan A. Rivera
jordan.rivera@example.com | (555) 014-2288 | Portland, OR

EXPERIENCE
Nordvale Consulting - Software Engineer
Feb 2024 - Jul 2025
- Cut p95 API latency by 30% with Redis caching in Python
- Built 15+ React screens for the checkout platform

Brightfold Systems - Associate Software Engineer
Jul 2022 - Dec 2023
- Built 8 full-stack applications with Node.js and PostgreSQL
"""


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
        upload_id = new_id()
        await session.execute(
            insert(Upload).values(
                id=upload_id,
                tenant_id=tenant_a,
                candidate_id=created,
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
        await apply_import_plan(session, plan, tenant_id=tenant_a, candidate_id=created, upload_id=upload_id)
    return created


async def _snapshot(engine: AsyncEngine, tenant: TenantId, candidate: uuid.UUID) -> CandidateSnapshot:
    async with tenant_transaction(engine, tenant) as session:
        return await build_candidate_snapshot(session, candidate, now=TODAY)


async def _approve_role(engine: AsyncEngine, tenant: TenantId, user: UserId, employer: str) -> uuid.UUID:
    async with tenant_transaction(engine, tenant) as session:
        role: uuid.UUID = (
            await session.execute(select(Experience.id).where(Experience.employer == employer))
        ).scalar_one()
        await approve_entity(
            session, tenant_id=tenant, kind=EntityKind.EXPERIENCE, entity_id=role, approved_by=user
        )
    return role


async def _approve_facts_under(
    engine: AsyncEngine, tenant: TenantId, user: UserId, candidate: uuid.UUID, role: uuid.UUID
) -> list[uuid.UUID]:
    async with tenant_transaction(engine, tenant) as session:
        ids = [fact.fact_id for fact in await draft_facts(session, candidate) if fact.experience_id == role]
        for fact_id in ids:
            await approve_fact(session, tenant_id=tenant, fact_id=fact_id, approved_by=user)
    return ids


@pytest.mark.db
class TestOnlyApprovedRowsAreInTheSnapshot:
    async def test_an_imported_but_unapproved_graph_gives_the_engine_nothing(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
    ) -> None:
        snapshot = await _snapshot(engine, tenant_a, candidate_id)
        assert snapshot.experiences == () and snapshot.facts == ()
        assert snapshot.contact == SnapshotContact()
        assert snapshot.declared_skills == () and snapshot.skill_years == ()

    async def test_an_approved_role_and_its_approved_bullets_are_in(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        role = await _approve_role(engine, tenant_a, user_a, "Nordvale Consulting")
        approved = await _approve_facts_under(engine, tenant_a, user_a, candidate_id, role)
        snapshot = await _snapshot(engine, tenant_a, candidate_id)
        assert [(item.employer, item.title) for item in snapshot.experiences] == [
            ("Nordvale Consulting", "Software Engineer")
        ]
        assert sorted(fact.fact_id for fact in snapshot.facts) == sorted(approved)

    async def test_metrics_and_skills_travel_exactly_as_written(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        role = await _approve_role(engine, tenant_a, user_a, "Nordvale Consulting")
        await _approve_facts_under(engine, tenant_a, user_a, candidate_id, role)
        snapshot = await _snapshot(engine, tenant_a, candidate_id)
        latency = next(fact for fact in snapshot.facts if "p95" in fact.text)
        assert [metric.exact_value for metric in latency.metrics] == ["30%"]
        assert {skill.canonical for skill in latency.skills} >= {"redis", "python"}
        screens = next(fact for fact in snapshot.facts if "React" in fact.text)
        assert [metric.exact_value for metric in screens.metrics] == ["15+"]

    async def test_draft_bullets_under_an_approved_role_are_out(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        # The role is approved, so only the facts' own status keeps them off the page.
        await _approve_role(engine, tenant_a, user_a, "Nordvale Consulting")
        snapshot = await _snapshot(engine, tenant_a, candidate_id)
        assert [item.employer for item in snapshot.experiences] == ["Nordvale Consulting"]
        assert snapshot.facts == ()

    async def test_a_rejected_bullet_is_out(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        role = await _approve_role(engine, tenant_a, user_a, "Nordvale Consulting")
        async with tenant_transaction(engine, tenant_a) as session:
            drafts = [fact for fact in await draft_facts(session, candidate_id) if fact.experience_id == role]
            await approve_fact(session, tenant_id=tenant_a, fact_id=drafts[0].fact_id, approved_by=user_a)
            await reject_fact(session, tenant_id=tenant_a, fact_id=drafts[1].fact_id, rejected_by=user_a)
        snapshot = await _snapshot(engine, tenant_a, candidate_id)
        assert [fact.fact_id for fact in snapshot.facts] == [drafts[0].fact_id]

    async def test_an_approved_bullet_under_an_unapproved_role_is_out(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        # The bullet is approved; its role (employer, title, dates) is not, and a resume
        # would print the role around it.
        async with tenant_transaction(engine, tenant_a) as session:
            role = (
                await session.execute(
                    select(Experience.id).where(Experience.employer == "Brightfold Systems")
                )
            ).scalar_one()
            for fact in await draft_facts(session, candidate_id):
                if fact.experience_id == role:
                    await approve_fact(session, tenant_id=tenant_a, fact_id=fact.fact_id, approved_by=user_a)
        snapshot = await _snapshot(engine, tenant_a, candidate_id)
        assert snapshot.facts == () and snapshot.experiences == ()

    async def test_only_approved_contact_details_are_printed(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            email = (
                await session.execute(select(ProfileField.id).where(ProfileField.kind == "email"))
            ).scalar_one()
            await approve_entity(
                session,
                tenant_id=tenant_a,
                kind=EntityKind.PROFILE_FIELD,
                entity_id=email,
                approved_by=user_a,
            )
        snapshot = await _snapshot(engine, tenant_a, candidate_id)
        assert snapshot.contact == SnapshotContact(email="jordan.rivera@example.com")


@pytest.mark.db
class TestSkillYearsAreDerived:
    async def test_months_come_from_the_approved_roles_dates(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
    ) -> None:
        role = await _approve_role(engine, tenant_a, user_a, "Nordvale Consulting")
        await _approve_facts_under(engine, tenant_a, user_a, candidate_id, role)
        snapshot = await _snapshot(engine, tenant_a, candidate_id)
        python = next(item for item in snapshot.skill_years if item.canonical == "python")
        assert python.full_time_months == 18  # Feb 2024 to Jul 2025, both included
        assert python.last_used == dt.date(2025, 7, 1)
        assert snapshot.full_time_months == 18


class TestTheTypeRefusesAnIncoherentSnapshot:
    def _bare(self, *facts: SnapshotFact) -> CandidateSnapshot:
        return CandidateSnapshot(
            candidate_id=uuid.uuid4(),
            taken_at=dt.datetime(2026, 10, 16, tzinfo=dt.UTC),
            as_of=TODAY,
            taxonomy_version="test",
            contact=SnapshotContact(),
            facts=facts,
        )

    def test_a_fact_under_a_role_the_snapshot_lacks_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="role that is not in the snapshot"):
            self._bare(SnapshotFact(fact_id=uuid.uuid4(), text="A true bullet", experience_id=uuid.uuid4()))

    def test_a_fact_listed_twice_is_refused(self) -> None:
        fact = SnapshotFact(fact_id=uuid.uuid4(), text="A true bullet")
        with pytest.raises(ValidationError, match="appears twice"):
            self._bare(fact, fact)

    def test_a_fact_with_no_anchor_is_allowed(self) -> None:
        assert len(self._bare(SnapshotFact(fact_id=uuid.uuid4(), text="A true bullet")).facts) == 1
