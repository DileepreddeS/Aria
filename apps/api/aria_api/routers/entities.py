"""Approving the rest of the graph over HTTP.

``/facts`` has its own router because facts have their own lifecycle module, immutable
revisions and a merge operation. Everything else — roles, degrees, projects, skills,
contact details, motivations — is proposed, then accepted or turned down, so one router
with the kind in the path serves all seven.

Without this the completeness report could not be satisfied: it counts approved roles,
education, skills and contact details, and nothing could approve them. A gate that
cannot be passed teaches you to ignore it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from aria_api.deps import CallerDep, SessionDep
from aria_api.schemas import EntityList, EntityOut, RejectRequest
from aria_core.graph.entities import (
    EntityKind,
    EntityNotFound,
    EntityView,
    IllegalEntityTransition,
    approve_entity,
    entities,
    get_entity,
    reconsider_entity,
    reject_entity,
)
from aria_core.graph.status import ApprovalStatus

__all__ = ["router"]

router = APIRouter(prefix="/entities", tags=["entities"])


def _out(view: EntityView) -> EntityOut:
    return EntityOut(
        entity_id=view.entity_id,
        kind=view.kind.value,
        status=view.status.value,
        summary=view.summary,
        source=view.source,
        source_line=view.source_line,
        source_upload_id=view.source_upload_id,
        approved_by=view.approved_by,
    )


async def _owned(session: SessionDep, kind: EntityKind, entity_id: uuid.UUID) -> EntityView:
    try:
        return await get_entity(session, kind, entity_id)
    except EntityNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no such {kind.value}") from error


@router.get("/{kind}", response_model=EntityList)
async def list_entities(
    kind: EntityKind,
    caller: CallerDep,
    session: SessionDep,
    status_filter: Annotated[ApprovalStatus | None, Query(alias="status")] = ApprovalStatus.DRAFT,
) -> EntityList:
    """Rows of one kind, oldest first. Defaults to the review queue."""
    found = await entities(session, caller.candidate_id, kind, status=status_filter)
    return EntityList(kind=kind.value, entities=[_out(view) for view in found], total=len(found))


@router.post("/{kind}/{entity_id}/approve", response_model=EntityOut)
async def approve(
    kind: EntityKind, entity_id: uuid.UUID, caller: CallerDep, session: SessionDep
) -> EntityOut:
    """The candidate says this is right. The approver is the caller, as for a fact."""
    await _owned(session, kind, entity_id)
    try:
        return _out(
            await approve_entity(
                session,
                tenant_id=caller.tenant_id,
                kind=kind,
                entity_id=entity_id,
                approved_by=caller.user_id,
            )
        )
    except IllegalEntityTransition as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/{kind}/{entity_id}/reject", response_model=EntityOut)
async def reject(
    kind: EntityKind,
    entity_id: uuid.UUID,
    body: RejectRequest,
    caller: CallerDep,
    session: SessionDep,
) -> EntityOut:
    """Not right. Kept, so an import does not propose it again."""
    await _owned(session, kind, entity_id)
    try:
        return _out(
            await reject_entity(
                session,
                tenant_id=caller.tenant_id,
                kind=kind,
                entity_id=entity_id,
                rejected_by=caller.user_id,
                reason=body.reason,
            )
        )
    except IllegalEntityTransition as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/{kind}/{entity_id}/reconsider", response_model=EntityOut)
async def reconsider(
    kind: EntityKind, entity_id: uuid.UUID, caller: CallerDep, session: SessionDep
) -> EntityOut:
    """Move a rejected row back to draft."""
    await _owned(session, kind, entity_id)
    try:
        return _out(
            await reconsider_entity(
                session,
                tenant_id=caller.tenant_id,
                kind=kind,
                entity_id=entity_id,
                actor=caller.user_id,
            )
        )
    except IllegalEntityTransition as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
