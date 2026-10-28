"""The question queue over HTTP.

Answering is a ``POST`` that changes the graph, which is why the response says what it
created. "OK" would leave the candidate guessing whether their answer did anything,
and the one thing an answer must never do is disappear quietly.

A refused answer is a **422 with what was expected**. "Invalid answer" is not
actionable; "I need a start and an end, like '2023-06 to 2023-08'" is.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from aria_api.deps import CallerDep, SessionDep
from aria_api.schemas import (
    AnswerRequest,
    AnswerResponse,
    QuestionList,
    QuestionOut,
    ReconsiderQuestionResponse,
    RejectRequest,
)
from aria_core.graph.questions import (
    QuestionNotFound,
    QuestionStatus,
    QuestionView,
    Reconsidered,
    UnansweredQuestion,
    answer_question,
    ask_counts_toward_years,
    dismiss_question,
    get_question,
    open_question_count,
    questions_for,
    reconsider_question,
)

__all__ = ["router"]

router = APIRouter(prefix="/questions", tags=["questions"])


def _out(view: QuestionView) -> QuestionOut:
    return QuestionOut(
        question_id=view.question_id,
        kind=view.kind,
        prompt=view.prompt,
        detail=view.detail,
        status=view.status.value,
        source_upload_id=view.source_upload_id,
        source_lines=list(view.source_lines),
        subject_kind=view.subject_kind,
        subject_id=view.subject_id,
        answer=view.answer,
        created_at=view.created_at,
    )


async def _owned(session: SessionDep, question_id: uuid.UUID, caller: CallerDep) -> QuestionView:
    try:
        return await get_question(session, question_id)
    except QuestionNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such question") from error


@router.get("", response_model=QuestionList)
async def list_questions(
    caller: CallerDep,
    session: SessionDep,
    status_filter: Annotated[QuestionStatus | None, Query(alias="status")] = QuestionStatus.OPEN,
) -> QuestionList:
    """Questions in one state, oldest first. Defaults to what is still waiting.

    Roles approved before the years flag existed get their question here, the first
    time the candidate looks; asking twice is a no-op.
    """
    await ask_counts_toward_years(session, tenant_id=caller.tenant_id, candidate_id=caller.candidate_id)
    views = await questions_for(session, caller.candidate_id, status=status_filter)
    return QuestionList(
        questions=[_out(view) for view in views],
        open_count=await open_question_count(session, caller.candidate_id),
    )


@router.post("/{question_id}/answer", response_model=AnswerResponse)
async def answer(
    question_id: uuid.UUID, body: AnswerRequest, caller: CallerDep, session: SessionDep
) -> AnswerResponse:
    """Record an answer and apply what it unblocks.

    Whatever the answer creates arrives as a draft, so the answer and the claim
    remain two separate deliberate acts.
    """
    await _owned(session, question_id, caller)
    try:
        outcome = await answer_question(
            session,
            tenant_id=caller.tenant_id,
            question_id=question_id,
            answer=body.answer,
            answered_by=caller.user_id,
        )
    except UnansweredQuestion as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"{error}. Expected {error.expected}." if error.expected else str(error)),
        ) from error

    return AnswerResponse(
        question_id=outcome.question_id,
        subject_kind=outcome.subject_kind,
        subject_id=outcome.subject_id,
        fact_ids=list(outcome.fact_ids),
        detail=outcome.detail,
        summary=outcome.summary(),
    )


@router.post("/{question_id}/dismiss", response_model=QuestionOut)
async def dismiss(
    question_id: uuid.UUID, body: RejectRequest, caller: CallerDep, session: SessionDep
) -> QuestionOut:
    """Not worth answering. Kept, so a re-import does not ask it again."""
    await _owned(session, question_id, caller)
    try:
        await dismiss_question(
            session,
            tenant_id=caller.tenant_id,
            question_id=question_id,
            actor=caller.user_id,
            reason=body.reason,
        )
    except UnansweredQuestion as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _out(await get_question(session, question_id))


@router.post("/{question_id}/reconsider", response_model=ReconsiderQuestionResponse)
async def reconsider(
    question_id: uuid.UUID, caller: CallerDep, session: SessionDep
) -> ReconsiderQuestionResponse:
    """Take back an answer or a dismissal.

    Reopened when what the answer did still stands; withdrawn (deleted, so the next
    import asks afresh) when what it made is gone; refused when answering again would
    create a second row.
    """
    await _owned(session, question_id, caller)
    try:
        outcome = await reconsider_question(
            session, tenant_id=caller.tenant_id, question_id=question_id, actor=caller.user_id
        )
    except UnansweredQuestion as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    detail = (
        "open again; answer it with aria answer"
        if outcome is Reconsidered.REOPENED
        else "what the answer made is gone, so the question was withdrawn; import the upload again "
        "and it will be asked afresh"
    )
    return ReconsiderQuestionResponse(question_id=question_id, outcome=outcome.value, detail=detail)
