"""The approval flow: see a draft, approve it, reject it, or rewrite it.

This is the endpoint where a proposal becomes a claim, so it is the one that matters
most in this file. Four properties it holds:

* **Approval names a person.** ``approved_by`` is the authenticated caller, recorded
  on the row and in the audit event. A fact that became true with nobody attached is
  not a verified fact.
* **The source travels with every draft.** A list of ids is useless to somebody
  deciding what to approve; the response carries the text, the upload and the line.
* **An edit appends a revision.** Nothing rewrites text in place, so a resume built
  from an earlier wording still explains itself.
* **The lifecycle is enforced in code**, not by the client. Approving a rejected fact
  is a 409, because the transition table in
  :mod:`aria_core.graph.facts` does not permit it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from aria_api.deps import CallerDep, SessionDep
from aria_api.schemas import DraftFact, DraftList, EditRequest, RejectRequest
from aria_core.graph.facts import (
    FactNotFound,
    FactView,
    IllegalFactTransition,
    approve_fact,
    approved_facts,
    draft_facts,
    edit_fact,
    get_fact,
    merge_facts,
    reconsider_fact,
    reject_fact,
    rejected_facts,
)
from aria_core.graph.status import ApprovalStatus

__all__ = ["router"]

router = APIRouter(prefix="/facts", tags=["facts"])

_READERS = {
    ApprovalStatus.DRAFT: draft_facts,
    ApprovalStatus.APPROVED: approved_facts,
    ApprovalStatus.REJECTED: rejected_facts,
}


def _out(view: FactView) -> DraftFact:
    return DraftFact(
        fact_id=view.fact_id,
        text=view.text,
        version=view.version,
        status=view.status.value,
        source=view.source.value,
        source_upload_id=view.source_upload_id,
        source_line=view.source_line,
        experience_id=view.experience_id,
        project_id=view.project_id,
        education_id=view.education_id,
    )


async def _owned(session: SessionDep, fact_id: uuid.UUID, caller: CallerDep) -> FactView:
    """Fetch a fact, or 404.

    Row-level security confines this to the tenant. The candidate check confines it
    further, which matters the moment one tenant has more than one candidate — and
    costs nothing now.
    """
    try:
        view = await get_fact(session, fact_id)
    except FactNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such fact") from error
    return view


@router.get("", response_model=DraftList)
async def list_facts(
    caller: CallerDep,
    session: SessionDep,
    status_filter: Annotated[ApprovalStatus, Query(alias="status")] = ApprovalStatus.DRAFT,
) -> DraftList:
    """Facts in one state, oldest first. Defaults to the review queue."""
    reader = _READERS.get(status_filter)
    if reader is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"cannot list facts that are {status_filter.value}",
        )
    views = await reader(session, caller.candidate_id)
    return DraftList(facts=[_out(view) for view in views], total=len(views))


@router.post("/{fact_id}/approve", response_model=DraftFact)
async def approve(fact_id: uuid.UUID, caller: CallerDep, session: SessionDep) -> DraftFact:
    """The candidate says this is true about them.

    The approver is the authenticated caller, never a field in the body: a client
    that could name its own approver would make the whole lifecycle decorative.
    """
    await _owned(session, fact_id, caller)
    try:
        await approve_fact(session, tenant_id=caller.tenant_id, fact_id=fact_id, approved_by=caller.user_id)
    except IllegalFactTransition as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _out(await get_fact(session, fact_id))


@router.post("/{fact_id}/reject", response_model=DraftFact)
async def reject(
    fact_id: uuid.UUID, body: RejectRequest, caller: CallerDep, session: SessionDep
) -> DraftFact:
    """Not right. Kept, so an import does not propose this wording again."""
    await _owned(session, fact_id, caller)
    try:
        await reject_fact(
            session,
            tenant_id=caller.tenant_id,
            fact_id=fact_id,
            rejected_by=caller.user_id,
            reason=body.reason,
        )
    except IllegalFactTransition as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _out(await get_fact(session, fact_id))


@router.post("/{fact_id}/edit", response_model=DraftFact)
async def edit(fact_id: uuid.UUID, body: EditRequest, caller: CallerDep, session: SessionDep) -> DraftFact:
    """Rewrite a fact. Appends a revision; never rewrites text in place."""
    await _owned(session, fact_id, caller)
    try:
        await edit_fact(
            session,
            tenant_id=caller.tenant_id,
            fact_id=fact_id,
            text=body.text,
            edited_by=caller.user_id,
            approve=body.approve,
        )
    except IllegalFactTransition as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return _out(await get_fact(session, fact_id))


@router.post("/{fact_id}/reconsider", response_model=DraftFact)
async def reconsider(fact_id: uuid.UUID, caller: CallerDep, session: SessionDep) -> DraftFact:
    """Move a rejected fact back to draft, so it can be edited and approved."""
    await _owned(session, fact_id, caller)
    try:
        await reconsider_fact(session, tenant_id=caller.tenant_id, fact_id=fact_id, actor=caller.user_id)
    except IllegalFactTransition as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _out(await get_fact(session, fact_id))


@router.post("/{fact_id}/merge-into/{keep_fact_id}", response_model=DraftFact)
async def merge(
    fact_id: uuid.UUID, keep_fact_id: uuid.UUID, caller: CallerDep, session: SessionDep
) -> DraftFact:
    """Two wordings of one achievement: keep ``keep_fact_id``, fold ``fact_id`` in.

    The merged fact is rejected and kept, pointing at the survivor. Nothing is
    deleted — a resume built from the other wording still has to explain itself.
    """
    await _owned(session, fact_id, caller)
    await _owned(session, keep_fact_id, caller)
    try:
        await merge_facts(
            session,
            tenant_id=caller.tenant_id,
            keep_fact_id=keep_fact_id,
            merge_fact_id=fact_id,
            actor=caller.user_id,
        )
    except IllegalFactTransition as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return _out(await get_fact(session, keep_fact_id))
