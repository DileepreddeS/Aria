"""Graph-wide maintenance the candidate asks for: re-tagging skill edges (DECISIONS 2026-10-17).

GET shows every change and writes nothing. POST writes exactly what GET showed: it carries
the preview's fingerprint, and a graph or taxonomy that changed in between is a 409, not
a different write.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from aria_api.deps import CallerDep, SessionDep
from aria_api.schemas import RetagDeclared, RetagEdge, RetagFact, RetagRequest, RetagResponse
from aria_core.graph.retag import Retag, StaleRetag, apply_retag, plan_retag
from aria_core.taxonomy.loader import load_default_taxonomy

__all__ = ["router"]

router = APIRouter(prefix="/graph", tags=["graph"])


def _response(plan: Retag, *, applied: bool) -> RetagResponse:
    return RetagResponse(
        taxonomy_version=plan.taxonomy_version,
        fingerprint=plan.fingerprint,
        applied=applied,
        changes=plan.changes,
        facts=[
            RetagFact(
                fact_id=item.fact_id,
                status=item.status,
                text=item.text,
                removed=[
                    RetagEdge(skill=canonical, surface_form=surface) for canonical, surface in item.removed
                ],
                added=[RetagEdge(skill=canonical, surface_form=surface) for canonical, surface in item.added],
            )
            for item in plan.facts
        ],
        declared=[
            RetagDeclared(
                declared_id=item.declared_id,
                surface_form=item.surface_form,
                old=item.old,
                new=item.new,
                left_because=item.left_because,
            )
            for item in plan.declared
        ],
    )


@router.get("/retag", response_model=RetagResponse)
async def preview_retag(caller: CallerDep, session: SessionDep) -> RetagResponse:
    """Every skill edge re-tagging would remove, add or re-point. Changes nothing."""
    plan = await plan_retag(session, candidate_id=caller.candidate_id, taxonomy=load_default_taxonomy())
    return _response(plan, applied=False)


@router.post("/retag", response_model=RetagResponse)
async def retag(body: RetagRequest, caller: CallerDep, session: SessionDep) -> RetagResponse:
    """Write the previewed re-tag. Skill edges only: no fact's text or approval state changes."""
    try:
        plan = await apply_retag(
            session,
            tenant_id=caller.tenant_id,
            candidate_id=caller.candidate_id,
            actor_id=caller.actor_id,
            taxonomy=load_default_taxonomy(),
            fingerprint=body.fingerprint,
        )
    except StaleRetag:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the graph or the taxonomy changed since the preview; nothing was written. Preview again.",
        ) from None
    return _response(plan, applied=True)
