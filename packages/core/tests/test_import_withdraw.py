"""Withdrawing a superseded upload, against a real database: the per-row guards.

The HTTP tests cover the flow. These cover what only the core can stage: a row approved
between the count the candidate saw and the withdrawal, and a role the old reading made
that the new reading's facts now hang off.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Experience, Upload, UserQuestion
from aria_core.db.session import tenant_transaction
from aria_core.graph.facts import approve_fact, approved_facts, draft_facts
from aria_core.importer.apply import apply_import_plan
from aria_core.importer.resume import parse_resume_text
from aria_core.importer.withdraw import NotSuperseded, plan_withdrawal, withdraw
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.taxonomy.loader import load_default_taxonomy

pytestmark = pytest.mark.db

TODAY = dt.date(2026, 10, 12)
RESUME = """Jordan A. Rivera
jordan.rivera@example.com

EXPERIENCE
Nordvale Consulting - Software Engineer
Feb 2024 - Jul 2025
- Reduced the nightly batch from six hours to forty minutes
- Built a deployment pipeline that cut release time in half
"""
#: The new reading: the same role, and a bullet the old reading did not have.
NEW_READING = """EXPERIENCE
Nordvale Consulting - Software Engineer
Feb 2024 - Jul 2025
- Wrote the on-call runbook adopted by three teams
"""


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
    return created


async def _upload(engine: AsyncEngine, tenant: TenantId, candidate: uuid.UUID, text: str) -> uuid.UUID:
    upload_id = new_id()
    async with tenant_transaction(engine, tenant) as session:
        await session.execute(
            insert(Upload).values(
                id=upload_id,
                tenant_id=tenant,
                candidate_id=candidate,
                original_filename="resume.txt",
                detected_type="txt",
                size_bytes=len(text),
                sha256=upload_id.hex * 2,
                quarantine_name=f"{upload_id}.bin",
                scan_status="not_scanned",
                scan_detail="dev scanner",
                parse_status="parsed",
                extracted_text=text,
            )
        )
        plan = parse_resume_text(text, load_default_taxonomy(), today=TODAY)
        await apply_import_plan(session, plan, tenant_id=tenant, candidate_id=candidate, upload_id=upload_id)
    return upload_id


async def _supersede(engine: AsyncEngine, tenant: TenantId, old: uuid.UUID, new: uuid.UUID) -> None:
    async with tenant_transaction(engine, tenant) as session:
        await session.execute(update(Upload).where(Upload.id == old).values(superseded_by=new))


async def test_a_fact_approved_after_the_count_survives_the_withdrawal(
    engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
) -> None:
    old = await _upload(engine, tenant_a, candidate_id, RESUME)
    new = await _upload(engine, tenant_a, candidate_id, NEW_READING)
    await _supersede(engine, tenant_a, old, new)

    async with tenant_transaction(engine, tenant_a) as session:
        plan = await plan_withdrawal(session, candidate_id=candidate_id, upload_id=old)
        assert plan.counts()["facts"] == 2
        # The candidate approves one after seeing the count, before confirming.
        first = next(fact for fact in await draft_facts(session, candidate_id) if fact.fact_id in plan.facts)
        await approve_fact(session, tenant_id=tenant_a, fact_id=first.fact_id, approved_by=user_a)
        deleted = await withdraw(session, plan, tenant_id=tenant_a, actor_id=str(user_a))
        survivors = [fact.fact_id for fact in await approved_facts(session, candidate_id)]

    assert deleted["facts"] == 1
    assert survivors == [first.fact_id]


async def test_a_role_the_new_reading_uses_is_kept(
    engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
) -> None:
    old = await _upload(engine, tenant_a, candidate_id, RESUME)
    # The new reading's import matches the role by employer and months and reuses it.
    new = await _upload(engine, tenant_a, candidate_id, NEW_READING)
    await _supersede(engine, tenant_a, old, new)

    async with tenant_transaction(engine, tenant_a) as session:
        plan = await plan_withdrawal(session, candidate_id=candidate_id, upload_id=old)
        assert plan.counts()["roles"] == 0
        assert plan.kept["roles still holding facts that stay"] == 1
        await withdraw(session, plan, tenant_id=tenant_a, actor_id=str(user_a))
        roles = (await session.execute(select(Experience.id))).all()
        remaining = [fact.text for fact in await draft_facts(session, candidate_id)]

    assert len(roles) == 1
    assert remaining == ["Wrote the on-call runbook adopted by three teams"]


async def test_the_current_reading_is_refused(
    engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID
) -> None:
    current = await _upload(engine, tenant_a, candidate_id, RESUME)
    async with tenant_transaction(engine, tenant_a) as session:
        with pytest.raises(NotSuperseded):
            await plan_withdrawal(session, candidate_id=candidate_id, upload_id=current)


#: The new reading words one old bullet a little differently: its import asks whether
#: the two are the same achievement, naming the old draft by its text.
REWORDED = """EXPERIENCE
Nordvale Consulting - Software Engineer
Feb 2024 - Jul 2025
- Reduced the nightly batch job from six hours to forty minutes
"""


async def test_a_same_achievement_question_about_a_withdrawn_draft_goes_with_it(
    engine: AsyncEngine, tenant_a: TenantId, user_a: UserId, candidate_id: uuid.UUID
) -> None:
    old = await _upload(engine, tenant_a, candidate_id, RESUME)
    new = await _upload(engine, tenant_a, candidate_id, REWORDED)
    await _supersede(engine, tenant_a, old, new)

    async with tenant_transaction(engine, tenant_a) as session:
        asked = (
            await session.execute(
                select(UserQuestion.id).where(
                    UserQuestion.kind == "possible_duplicate", UserQuestion.source_upload_id == new
                )
            )
        ).all()
        assert len(asked) == 1, "the reworded bullet should have asked about the old one"
        plan = await plan_withdrawal(session, candidate_id=candidate_id, upload_id=old)
        assert asked[0][0] in plan.questions
        await withdraw(session, plan, tenant_id=tenant_a, actor_id=str(user_a))
        left = (await session.execute(select(UserQuestion.id).where(UserQuestion.id == asked[0][0]))).all()

    assert left == []
