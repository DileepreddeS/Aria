"""Transitions are validated in code and audited (PRODUCT_SPEC §1.6, §3.7).

The legal moves are a table, not a scatter of ``if`` statements, so the lifecycle
can be read in one place and tested exhaustively. An LLM may decide *that* an
application should advance; whether it may is decided here.

Three rules the engine enforces that are easy to lose in handwritten checks:

* the move must be in the table, and a terminal state has no moves at all;
* the row is locked for the transition, so two workers cannot both advance the
  same application from the same state;
* the state change and its audit event are written in the same transaction, so
  there is no such thing as a state change with no event, or an event describing
  a change that did not happen.

``SUBMITTED`` additionally requires evidence. "Submitted" means a confirmation
page or email exists, and the engine will not take the word of whatever is
calling it (PRODUCT_SPEC §1.5).
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.audit.log import append_event
from aria_core.db.models import Application
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    AuditRecord,
    SubjectKind,
)
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId
from aria_core.state_machines.application import ApplicationState as S

__all__ = [
    "TRANSITIONS",
    "EvidenceRequired",
    "IllegalTransition",
    "UnknownApplication",
    "transition_application",
]


class IllegalTransition(Exception):
    """The requested move is not in the transition table."""

    def __init__(self, current: S, requested: S) -> None:
        allowed = ", ".join(sorted(state.value for state in TRANSITIONS[current])) or "nothing"
        super().__init__(f"{current.value} cannot move to {requested.value}; allowed from here: {allowed}")
        self.current = current
        self.requested = requested


class EvidenceRequired(Exception):
    """A submission was claimed without proof (PRODUCT_SPEC §1.5)."""


class UnknownApplication(Exception):
    """No such application in this tenant."""


#: Every legal move. A state mapped to an empty set is terminal.
TRANSITIONS: Final[dict[S, frozenset[S]]] = {
    S.DISCOVERED: frozenset({S.SCREENING, S.SKIPPED, S.BLOCKED_CLOSED, S.CANCELLED}),
    S.SCREENING: frozenset(
        {S.ASK_USER, S.TAILORING, S.SKIPPED, S.BLOCKED_POLICY, S.BLOCKED_CLOSED, S.CANCELLED}
    ),
    # The user answered the policy question, or never did.
    S.ASK_USER: frozenset(
        {S.SCREENING, S.TAILORING, S.SKIPPED, S.WAITING_FOR_USER, S.BLOCKED_CLOSED, S.CANCELLED}
    ),
    S.TAILORING: frozenset({S.VALIDATING, S.FAILED, S.BLOCKED_CLOSED, S.CANCELLED}),
    # Validators either pass, ask for a targeted repair, or give up and hand the
    # resume to the user with findings (needs_review).
    S.VALIDATING: frozenset({S.REPAIRING, S.READY, S.WAITING_FOR_USER, S.FAILED, S.CANCELLED}),
    S.REPAIRING: frozenset({S.VALIDATING, S.FAILED, S.CANCELLED}),
    # Approve mode and dream companies go through AWAITING_APPROVAL; autopilot
    # dispatches directly.
    S.READY: frozenset({S.AWAITING_APPROVAL, S.DISPATCHED_TO_RUNNER, S.BLOCKED_CLOSED, S.CANCELLED}),
    S.AWAITING_APPROVAL: frozenset({S.DISPATCHED_TO_RUNNER, S.SKIPPED, S.BLOCKED_CLOSED, S.CANCELLED}),
    S.DISPATCHED_TO_RUNNER: frozenset(
        {S.APPLYING, S.BLOCKED_CAPTCHA, S.BLOCKED_LOGIN, S.BLOCKED_CLOSED, S.FAILED, S.CANCELLED}
    ),
    S.APPLYING: frozenset(
        {
            S.VERIFYING,
            S.BLOCKED_CAPTCHA,
            S.BLOCKED_LOGIN,
            S.BLOCKED_CLOSED,
            S.WAITING_FOR_USER,
            S.FAILED,
            S.CANCELLED,
        }
    ),
    # No evidence, no SUBMITTED. VERIFYING may also time out into FAILED.
    S.VERIFYING: frozenset({S.SUBMITTED, S.WAITING_FOR_USER, S.FAILED, S.CANCELLED}),
    S.SUBMITTED: frozenset(
        {
            S.FOLLOW_UP,
            S.OUTCOME_REJECTED,
            S.OUTCOME_ASSESSMENT,
            S.OUTCOME_INTERVIEW,
            S.OUTCOME_OFFER,
            S.OUTCOME_GHOSTED,
        }
    ),
    S.FOLLOW_UP: frozenset(
        {
            S.OUTCOME_REJECTED,
            S.OUTCOME_ASSESSMENT,
            S.OUTCOME_INTERVIEW,
            S.OUTCOME_OFFER,
            S.OUTCOME_GHOSTED,
        }
    ),
    S.OUTCOME_ASSESSMENT: frozenset({S.OUTCOME_INTERVIEW, S.OUTCOME_REJECTED, S.OUTCOME_GHOSTED}),
    S.OUTCOME_INTERVIEW: frozenset({S.OUTCOME_OFFER, S.OUTCOME_REJECTED, S.OUTCOME_GHOSTED}),
    # The user solves the CAPTCHA or signs in, and the Runner carries on.
    S.BLOCKED_CAPTCHA: frozenset({S.APPLYING, S.FAILED, S.CANCELLED}),
    S.BLOCKED_LOGIN: frozenset({S.APPLYING, S.FAILED, S.CANCELLED}),
    S.WAITING_FOR_USER: frozenset(
        {S.SCREENING, S.TAILORING, S.APPLYING, S.VERIFYING, S.SKIPPED, S.FAILED, S.CANCELLED}
    ),
    # ---- terminal
    S.SKIPPED: frozenset(),
    S.BLOCKED_CLOSED: frozenset(),
    S.BLOCKED_POLICY: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.OUTCOME_REJECTED: frozenset(),
    S.OUTCOME_OFFER: frozenset(),
    S.OUTCOME_GHOSTED: frozenset(),
}


def is_legal(current: S, requested: S) -> bool:
    return requested in TRANSITIONS[current]


async def transition_application(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    application_id: uuid.UUID,
    to_state: S,
    actor_kind: ActorKind,
    actor_id: str,
    reason: str = "",
    evidence_ref: str | None = None,
) -> AuditRecord:
    """Move one application, or refuse. Returns the audit record written.

    The caller's transaction commits both the new state and the event.
    """
    current_value = await session.scalar(
        select(Application.state).where(Application.id == application_id).with_for_update()
    )
    if current_value is None:
        raise UnknownApplication(str(application_id))
    current = S(current_value)

    if not is_legal(current, to_state):
        raise IllegalTransition(current, to_state)
    if to_state is S.SUBMITTED and not evidence_ref:
        raise EvidenceRequired(
            "SUBMITTED needs evidence_ref: a confirmation page, URL or email. "
            'ARIA does not record "submitted" on a claim (PRODUCT_SPEC §1.5).'
        )

    await session.execute(
        update(Application)
        .where(Application.id == application_id)
        .values(state=to_state.value, updated_at=utc_now())
    )

    payload: dict[str, object] = {"from": current.value, "to": to_state.value}
    if evidence_ref:
        payload["evidence_ref"] = evidence_ref
    return await append_event(
        session,
        AuditEventDraft(
            action=AuditAction.APPLICATION_STATE_CHANGED,
            actor_kind=actor_kind,
            actor_id=actor_id,
            subject_kind=SubjectKind.APPLICATION,
            subject_id=str(application_id),
            outcome=AuditOutcome.OK,
            reason=reason,
            payload=payload,
        ),
        tenant_id=tenant_id,
    )
