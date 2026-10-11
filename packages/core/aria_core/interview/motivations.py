"""The motivations interview: six fixed questions, asked of everybody.

These are what the Answer Engine needs for "why do you want this role?" and they are
the things it must never invent. A motivation ARIA made up is a lie in the most
personal part of an application, and unlike an overstated number it cannot even be
checked against anything — which is why the ``motivations`` table records a source and
why nothing writes to it except an answer the candidate typed.

The six are fixed and in a fixed order. They are not derived from the resume: what
somebody has done and what they want to do next are different things, and inferring
the second from the first is how a cover letter ends up enthusiastic about work the
candidate was trying to leave.

Re-running the interview asks only what is still unanswered, because the queue
deduplicates per candidate on the kind.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.db.models import Motivation
from aria_core.graph.questions import ask
from aria_core.graph.status import ApprovalStatus, MotivationKind
from aria_core.interview.templates import MOTIVATION_PROMPTS, QuestionTemplate
from aria_core.schemas.identity import TenantId

__all__ = ["answered_kinds", "run_motivations_interview"]


async def answered_kinds(session: AsyncSession, candidate_id: uuid.UUID) -> set[MotivationKind]:
    """Which motivations the candidate has already given, in their own words."""
    rows = (
        await session.execute(
            select(Motivation.kind).where(
                Motivation.candidate_id == candidate_id,
                Motivation.status != ApprovalStatus.REJECTED.value,
            )
        )
    ).all()
    known = {kind.value for kind in MotivationKind}
    return {MotivationKind(row[0]) for row in rows if row[0] in known}


async def run_motivations_interview(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
) -> list[MotivationKind]:
    """Ask the motivations that are still missing. Returns the ones newly queued."""
    already = await answered_kinds(session, candidate_id)
    queued: list[MotivationKind] = []

    for kind, prompt in MOTIVATION_PROMPTS.items():
        if kind in already:
            continue
        question_id = await ask(
            session,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            kind=QuestionTemplate.MOTIVATION,
            prompt=prompt.render(),
            detail=prompt.detail(),
            # Keyed on the motivation, not on the question, so the six are distinct
            # and re-running the interview asks none of them twice.
            dedupe_key=f"motivation:{kind.value}",
            payload={"motivation_kind": kind.value},
        )
        if question_id is not None:
            queued.append(kind)
    return queued
