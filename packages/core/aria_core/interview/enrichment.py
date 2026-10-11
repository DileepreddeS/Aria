"""The enrichment interview: targeted questions about gaps that are actually there.

RESUME_ENGINE §5 describes it as "ARIA asks targeted questions to add missing metrics
and context ... Answers become new draft Facts". Both halves matter. The questions are
**targeted** — each one names a specific bullet, role or skill that is measurably
incomplete, not "tell me more about yourself", which produces prose nobody can verify.
And the answers become **drafts**, so a sentence the candidate typed into a prompt
still goes through approval before it can reach an employer.

Two decisions worth stating:

**It asks few questions.** Every gap is found, and only the most useful handful are
asked at a time (:data:`MAX_QUESTIONS_PER_ROUND`). A graph with thirty bullets and no
numbers would otherwise produce thirty questions in one go, and a queue of thirty is a
queue nobody answers — which leaves the graph exactly as incomplete as before, plus
thirty rows of guilt. The gaps that are not asked about now are found again next time.

**Nothing here writes to the graph.** It reads the graph, decides what is missing, and
asks. The answers are applied by :mod:`aria_core.graph.questions`, in one place, with
the same lifecycle as every other answer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.db.models import DeclaredSkill, Experience, ProfileField
from aria_core.graph.facts import approved_facts, draft_facts, fact_skill_canonicals
from aria_core.graph.questions import ask
from aria_core.graph.status import ApprovalStatus, ProfileFieldKind
from aria_core.interview.templates import (
    ENRICHMENT_PROMPTS,
    PROFILE_FIELD_PROMPTS,
    QuestionTemplate,
)
from aria_core.schemas.identity import TenantId

__all__ = ["MAX_QUESTIONS_PER_ROUND", "Gap", "find_gaps", "run_enrichment"]

#: How many questions one round of the interview asks. Small on purpose: a queue of
#: thirty is a queue nobody answers, and the gaps it skips are found again next time.
MAX_QUESTIONS_PER_ROUND = 8

#: Contact fields worth asking about, in the order they are asked. Matches what the
#: completeness report requires, so answering the interview can actually finish it.
REQUIRED_PROFILE_FIELDS = (
    ProfileFieldKind.FULL_NAME,
    ProfileFieldKind.EMAIL,
    ProfileFieldKind.PHONE,
    ProfileFieldKind.LOCATION_CITY,
    ProfileFieldKind.LOCATION_REGION,
)

#: A bullet shorter than this is a label, not a claim with a missing number.
_MIN_FACT_LENGTH = 25


@dataclass(frozen=True, slots=True)
class Gap:
    """Something missing from the graph, and the question that would fill it.

    ``priority`` orders the round: a role with nothing under it is worth more than a
    fifth bullet without a number, because nothing can be tailored from an empty role.
    Lower sorts first.
    """

    kind: str
    prompt: str
    detail: str
    dedupe_key: str
    priority: int
    subject_kind: str | None = None
    subject_id: uuid.UUID | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.prompt


async def _experiences_without_facts(
    session: AsyncSession, candidate_id: uuid.UUID, anchored: set[uuid.UUID]
) -> list[Gap]:
    """Roles on the graph with no achievements under them.

    The most valuable gap there is: a role with no facts contributes a date range and
    nothing a resume can say, so every application that should have used it cannot.
    """
    rows = (
        await session.execute(
            select(Experience.id, Experience.employer, Experience.title)
            .where(Experience.candidate_id == candidate_id)
            .order_by(Experience.start_month.desc())
        )
    ).all()

    prompt = ENRICHMENT_PROMPTS[QuestionTemplate.MISSING_CONTEXT]
    return [
        Gap(
            kind=QuestionTemplate.MISSING_CONTEXT,
            prompt=prompt.render(employer=row[1] or "this role", title=row[2] or "unknown title"),
            detail=prompt.detail(),
            dedupe_key=f"missing_context:{row[0]}",
            priority=0,
            subject_kind="experience",
            subject_id=row[0],
            payload={"experience_id": str(row[0]), "employer": row[1], "title": row[2]},
        )
        for row in rows
        if row[0] not in anchored
    ]


async def _missing_profile_fields(session: AsyncSession, candidate_id: uuid.UUID) -> list[Gap]:
    """Contact details an application cannot be submitted without."""
    present = {
        row[0]
        for row in (
            await session.execute(
                select(ProfileField.kind).where(
                    ProfileField.candidate_id == candidate_id,
                    ProfileField.status != ApprovalStatus.REJECTED.value,
                )
            )
        ).all()
    }

    gaps: list[Gap] = []
    for kind in REQUIRED_PROFILE_FIELDS:
        if kind.value in present:
            continue
        prompt = PROFILE_FIELD_PROMPTS[kind]
        gaps.append(
            Gap(
                kind=QuestionTemplate.MISSING_PROFILE_FIELD,
                prompt=prompt.render(),
                detail=prompt.detail(),
                dedupe_key=f"missing_profile_field:{kind.value}",
                priority=1,
                payload={"field_kind": kind.value},
            )
        )
    return gaps


async def _unevidenced_skills(session: AsyncSession, candidate_id: uuid.UUID) -> list[Gap]:
    """Skills the candidate listed that no fact mentions.

    A listed skill with nothing behind it is a keyword. It is not counted towards
    years of experience and cannot be cited on a resume, so asking what was built
    with it is the difference between a word and evidence.
    """
    declared = (
        await session.execute(
            select(DeclaredSkill.skill_canonical, DeclaredSkill.surface_form)
            .where(
                DeclaredSkill.candidate_id == candidate_id,
                DeclaredSkill.status != ApprovalStatus.REJECTED.value,
            )
            .order_by(DeclaredSkill.created_at)
        )
    ).all()
    evidenced = await fact_skill_canonicals(session, candidate_id)

    prompt = ENRICHMENT_PROMPTS[QuestionTemplate.UNEVIDENCED_SKILL]
    return [
        Gap(
            kind=QuestionTemplate.UNEVIDENCED_SKILL,
            prompt=prompt.render(skill=row[1]),
            detail=prompt.detail(),
            dedupe_key=f"unevidenced_skill:{row[0]}",
            priority=3,
            payload={"skill_canonical": row[0], "surface_form": row[1]},
        )
        for row in declared
        if row[0] not in evidenced
    ]


async def _facts_without_numbers(
    session: AsyncSession, candidate_id: uuid.UUID
) -> tuple[list[Gap], set[uuid.UUID]]:
    """Bullets with no number in them, and the set of roles that have any bullet.

    Both come from the same read, which is why they are returned together: the fact
    list is the only place that knows which roles are evidenced.
    """
    from aria_core.importer.metrics import extract_metrics

    facts = await approved_facts(session, candidate_id) + await draft_facts(session, candidate_id)
    anchored = {fact.experience_id for fact in facts if fact.experience_id is not None}

    prompt = ENRICHMENT_PROMPTS[QuestionTemplate.MISSING_METRIC]
    gaps: list[Gap] = []
    for fact in facts:
        if len(fact.text) < _MIN_FACT_LENGTH or extract_metrics(fact.text):
            continue
        gaps.append(
            Gap(
                kind=QuestionTemplate.MISSING_METRIC,
                prompt=prompt.render(text=fact.text),
                detail=prompt.detail(),
                dedupe_key=f"missing_metric:{fact.fact_id}",
                priority=2,
                subject_kind="fact",
                subject_id=fact.fact_id,
                payload={
                    "fact_id": str(fact.fact_id),
                    "experience_id": str(fact.experience_id) if fact.experience_id else None,
                    "project_id": str(fact.project_id) if fact.project_id else None,
                    "education_id": str(fact.education_id) if fact.education_id else None,
                },
            )
        )
    return gaps, anchored


async def find_gaps(session: AsyncSession, candidate_id: uuid.UUID) -> list[Gap]:
    """Every gap in the graph worth asking about, most useful first.

    Reads only. Finding a gap and deciding to ask about it are separate steps, so a
    report can show what is missing without filling anybody's queue.
    """
    metric_gaps, anchored = await _facts_without_numbers(session, candidate_id)
    gaps = [
        *await _experiences_without_facts(session, candidate_id, anchored),
        *await _missing_profile_fields(session, candidate_id),
        *metric_gaps,
        *await _unevidenced_skills(session, candidate_id),
    ]
    return sorted(gaps, key=lambda gap: (gap.priority, gap.dedupe_key))


async def run_enrichment(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    limit: int = MAX_QUESTIONS_PER_ROUND,
) -> list[Gap]:
    """Ask about the most useful gaps. Returns the ones that were newly queued.

    Already-asked questions are skipped by the queue's own deduplication, so running
    this twice does not double anything — and a gap that was dismissed stays
    dismissed.
    """
    queued: list[Gap] = []
    for gap in await find_gaps(session, candidate_id):
        if len(queued) >= limit:
            break
        question_id = await ask(
            session,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            kind=gap.kind,
            prompt=gap.prompt,
            detail=gap.detail,
            dedupe_key=gap.dedupe_key,
            payload=gap.payload,
            subject_kind=gap.subject_kind,
            subject_id=gap.subject_id,
        )
        if question_id is not None:
            queued.append(gap)
    return queued
