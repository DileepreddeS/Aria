"""Starting an interview. Answering one goes through ``/questions``.

Two endpoints that only queue questions, because that is all an interview is here: it
decides what to ask and puts it on the same queue as everything else. The answers go
back through ``POST /questions/{id}/answer``, so there is one code path from an answer
to the graph and one place where the lifecycle is enforced.

``GET /interview/gaps`` is separate from starting the interview on purpose. Seeing
what is missing should not fill anybody's queue — the report is read far more often
than the interview is run.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from aria_api.deps import CallerDep, SessionDep
from aria_api.schemas import GapList, GapOut, InterviewResponse
from aria_core.interview.enrichment import MAX_QUESTIONS_PER_ROUND, find_gaps, run_enrichment
from aria_core.interview.motivations import answered_kinds, run_motivations_interview
from aria_core.interview.templates import MOTIVATION_PROMPTS

__all__ = ["router"]

router = APIRouter(prefix="/interview", tags=["interview"])


@router.get("/gaps", response_model=GapList)
async def gaps(caller: CallerDep, session: SessionDep) -> GapList:
    """Everything missing from the graph that ARIA knows how to ask about.

    Read-only, and uncapped: this is the full picture, where starting the interview
    asks only the most useful handful.
    """
    found = await find_gaps(session, caller.candidate_id)
    return GapList(
        gaps=[
            GapOut(
                kind=gap.kind,
                prompt=gap.prompt,
                detail=gap.detail,
                priority=gap.priority,
                subject_kind=gap.subject_kind,
                subject_id=gap.subject_id,
            )
            for gap in found
        ],
        total=len(found),
    )


@router.post("/enrichment", response_model=InterviewResponse, status_code=status.HTTP_201_CREATED)
async def enrichment(
    caller: CallerDep,
    session: SessionDep,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=50,
            description=(
                "How many questions to queue. Few on purpose: a queue of thirty is one nobody answers."
            ),
        ),
    ] = MAX_QUESTIONS_PER_ROUND,
) -> InterviewResponse:
    """Ask about the most useful gaps. Answers become new draft facts."""
    queued = await run_enrichment(
        session, tenant_id=caller.tenant_id, candidate_id=caller.candidate_id, limit=limit
    )
    remaining = len(await find_gaps(session, caller.candidate_id))
    return InterviewResponse(
        interview="enrichment",
        queued=len(queued),
        prompts=[gap.prompt for gap in queued],
        remaining_gaps=remaining,
        summary=(
            f"{len(queued)} question(s) queued; {remaining} gap(s) known in total. "
            "Answer them with `aria questions` and `aria answer`."
            if queued
            else f"nothing new to ask; {remaining} gap(s) already on the queue or dismissed"
        ),
    )


@router.post("/motivations", response_model=InterviewResponse, status_code=status.HTTP_201_CREATED)
async def motivations(caller: CallerDep, session: SessionDep) -> InterviewResponse:
    """Ask the motivations that are still missing. Never inferred from the resume."""
    queued = await run_motivations_interview(
        session, tenant_id=caller.tenant_id, candidate_id=caller.candidate_id
    )
    answered = await answered_kinds(session, caller.candidate_id)
    return InterviewResponse(
        interview="motivations",
        queued=len(queued),
        prompts=[MOTIVATION_PROMPTS[kind].render() for kind in queued],
        remaining_gaps=len(MOTIVATION_PROMPTS) - len(answered),
        summary=(
            f"{len(queued)} of {len(MOTIVATION_PROMPTS)} motivation question(s) queued; "
            f"{len(answered)} already answered"
        ),
    )
