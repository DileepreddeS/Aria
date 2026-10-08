"""``SUBMIT_APPLICATION`` (SECURITY.md §4, PRODUCT_SPEC §3.8).

The one irreversible action in the product: once an application reaches a real
employer it cannot be taken back. Every precondition is checked here, in code, and
none of them can be supplied by a model or read off the page.

Checks, in the order a denial is most worth hearing about:

* the user's autonomy level permits ARIA to submit at all, and Approve mode — or a
  dream company, at any level — has an actual approval;
* no question to the user is still open: ARIA never submits over an unanswered
  question, because the answer might change the application (PRODUCT_SPEC §1.2);
* every required field is resolved, so nothing is submitted half-filled;
* the posting is still open;
* the daily cap and the per-company cap are not exceeded;
* it is inside the user's apply window.
"""

from __future__ import annotations

from aria_core.schemas.policy import AutonomyLevel, PolicyDecision, PolicyRequest, ReasonCode

__all__ = ["check"]


def check(request: PolicyRequest) -> PolicyDecision:
    from aria_core.policy.engine import allow, deny

    context = request.context
    passed: list[str] = []

    if context.autonomy_level is AutonomyLevel.SUGGEST:
        return deny(
            request,
            ReasonCode.AUTONOMY_FORBIDS,
            "autonomy level is suggest: ARIA shortlists and prepares, the user applies",
        )

    needs_approval = context.autonomy_level is AutonomyLevel.APPROVE or context.is_dream_company
    if needs_approval and not context.approval_granted:
        why = (
            "this is a dream company, which is always Approve"
            if context.is_dream_company
            else "autonomy level is approve"
        )
        return deny(request, ReasonCode.APPROVAL_REQUIRED, f"{why}, and no approval has been given")
    passed.append("autonomy")

    if context.open_user_questions:
        return deny(
            request,
            ReasonCode.OPEN_USER_QUESTIONS,
            f"{context.open_user_questions} question(s) to the user are still open",
            *passed,
        )
    passed.append("no_open_questions")

    if not context.required_fields_resolved:
        return deny(
            request,
            ReasonCode.FIELDS_UNRESOLVED,
            "not every required field has a resolved answer",
            *passed,
        )
    passed.append("fields_resolved")

    if not context.posting_open:
        return deny(request, ReasonCode.POSTING_CLOSED, "the posting has closed", *passed)
    passed.append("posting_open")

    if context.daily_cap and context.applications_today >= context.daily_cap:
        return deny(
            request,
            ReasonCode.DAILY_CAP_REACHED,
            f"{context.applications_today} of {context.daily_cap} applications already sent today",
            *passed,
        )
    passed.append("daily_cap")

    if context.company_cap and context.applications_for_company_today >= context.company_cap:
        return deny(
            request,
            ReasonCode.COMPANY_CAP_REACHED,
            f"{context.applications_for_company_today} of {context.company_cap} already sent to this "
            "company today",
            *passed,
        )
    passed.append("company_cap")

    if not context.within_apply_window:
        return deny(
            request,
            ReasonCode.OUTSIDE_APPLY_WINDOW,
            "outside the user's apply window; applications go out at human hours",
            *passed,
        )
    passed.append("apply_window")

    return allow(request, *passed)
