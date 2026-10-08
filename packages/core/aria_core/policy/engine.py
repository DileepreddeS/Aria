"""The policy engine (SECURITY.md §4).

Every agent action passes through :func:`evaluate` before it happens. The engine is
**default-deny**: a capability with no registered rule is refused, so adding a
capability to the enum does not accidentally permit it — the refusal has to be
noticed and a rule written deliberately.

:func:`evaluate` is a pure function of its request, which is what makes the rules
testable in isolation. :func:`decide_and_audit` wraps it and records the decision,
allow or deny, because an audit log that only holds refusals cannot answer "why did
it submit that?".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.audit.log import append_event
from aria_core.policy.rules import navigate, submit_application
from aria_core.schemas.audit import ActorKind, AuditAction, AuditEventDraft, AuditOutcome, SubjectKind
from aria_core.schemas.identity import TenantId
from aria_core.schemas.policy import (
    Capability,
    Decision,
    PolicyDecision,
    PolicyRequest,
    ReasonCode,
)

__all__ = ["RULES", "allow", "decide_and_audit", "deny", "evaluate"]

Rule = Callable[[PolicyRequest], PolicyDecision]


def allow(request: PolicyRequest, *checks: str) -> PolicyDecision:
    return PolicyDecision(
        decision=Decision.ALLOW,
        capability=request.capability,
        reason_code=ReasonCode.ALLOWED,
        checks_passed=checks,
    )


def deny(request: PolicyRequest, code: ReasonCode, reason: str, *checks: str) -> PolicyDecision:
    return PolicyDecision(
        decision=Decision.DENY,
        capability=request.capability,
        reason_code=code,
        reason=reason,
        checks_passed=checks,
    )


#: Phase 0 implements two rules, enough to prove the framework end to end. The rest
#: arrive with the component that performs them: FILL_FIELD, UPLOAD_RESUME,
#: USE_CREDENTIAL and CREATE_ACCOUNT in Phase 5 with the browser agent,
#: READ_EMAIL_SUMMARY in Phase 8. SEND_EMAIL is not granted in v1 and gets no rule.
RULES: Final[dict[Capability, Rule]] = {
    Capability.NAVIGATE: navigate.check,
    Capability.SUBMIT_APPLICATION: submit_application.check,
}


def evaluate(request: PolicyRequest) -> PolicyDecision:
    """Decide one proposed action. Pure: no I/O, no clock, no network.

    Three gates before any capability-specific rule runs, in this order, because
    each makes the next one irrelevant: the kill switch, whether the task was
    granted the capability at all, and whether a rule exists.
    """
    if request.context.kill_switch_engaged:
        return deny(
            request,
            ReasonCode.KILL_SWITCH,
            "the kill switch is engaged; no agent action is permitted",
        )

    if request.capability not in request.context.granted_capabilities:
        return deny(
            request,
            ReasonCode.CAPABILITY_NOT_GRANTED,
            f"{request.capability.value} was not granted to this task",
            "kill_switch",
        )

    rule = RULES.get(request.capability)
    if rule is None:
        return deny(
            request,
            ReasonCode.NO_RULE,
            f"no policy rule is registered for {request.capability.value}, so it is refused",
            "kill_switch",
            "capability_granted",
        )

    return rule(request)


async def decide_and_audit(
    session: AsyncSession,
    request: PolicyRequest,
    *,
    tenant_id: TenantId,
    actor_kind: ActorKind = ActorKind.AGENT,
) -> PolicyDecision:
    """Decide and record. The decision is returned; acting on it is the caller's job.

    The event is written in the caller's transaction, so a decision that was acted
    on is always in the log.
    """
    decision = evaluate(request)
    await append_event(
        session,
        AuditEventDraft(
            action=AuditAction.POLICY_DECIDED,
            actor_kind=actor_kind,
            actor_id=request.actor_id,
            subject_kind=SubjectKind.APPLICATION,
            subject_id=request.subject_id,
            outcome=AuditOutcome.ALLOWED if decision.allowed else AuditOutcome.DENIED,
            reason=decision.reason,
            payload={
                "capability": request.capability.value,
                "reason_code": decision.reason_code.value,
                "checks_passed": ",".join(decision.checks_passed),
                **{f"arg.{key}": value for key, value in request.arguments.items()},
            },
        ),
        tenant_id=tenant_id,
    )
    return decision
