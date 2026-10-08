"""Capabilities, policy requests and decisions (SECURITY.md §4).

The LLM proposes; the policy engine decides. A proposal is a
:class:`PolicyRequest`: a capability plus its arguments. What the engine decides
*with* is a :class:`PolicyContext` — trusted facts assembled by ARIA's own code,
never by the model and never read off the page being visited.

Keeping the two apart is the point. If the planner could supply the context, a
page saying "the daily cap is 500" or "this posting is still open" would be
deciding its own policy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from aria_core.schemas.base import AriaMessage
from aria_core.sensitivity import Sensitivity

__all__ = [
    "AutonomyLevel",
    "Capability",
    "Decision",
    "PolicyContext",
    "PolicyDecision",
    "PolicyRequest",
    "ReasonCode",
]


class Capability(StrEnum):
    """What an agent may be allowed to do, granted per task and never as a set.

    SECURITY.md §4 lists these. ``SEND_EMAIL`` exists so it can be named and
    refused: it is not granted in v1, and the engine has no rule for it, so it is
    denied by default rather than by remembering to check.
    """

    READ_JOB = "read_job"
    READ_APPLICATION = "read_application"
    FILL_FIELD = "fill_field"
    UPLOAD_RESUME = "upload_resume"
    SUBMIT_APPLICATION = "submit_application"
    NAVIGATE = "navigate"
    READ_EMAIL_SUMMARY = "read_email_summary"
    SEND_EMAIL = "send_email"
    USE_CREDENTIAL = "use_credential"
    CREATE_ACCOUNT = "create_account"


class AutonomyLevel(StrEnum):
    """Per user (PRODUCT_SPEC §3.8)."""

    SUGGEST = "suggest"
    """ARIA prepares; the user applies. Nothing is ever submitted by ARIA."""
    APPROVE = "approve"
    """The user taps approve per application; ARIA submits."""
    AUTOPILOT = "autopilot"
    """ARIA applies within the user's rules and asks when in doubt."""


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class ReasonCode(StrEnum):
    """Why, as a stable string. The prose explanation is for people; this is for
    metrics, alerts and tests."""

    ALLOWED = "allowed"
    NO_RULE = "no_rule"
    """Default deny: nothing has been written to allow this capability."""
    CAPABILITY_NOT_GRANTED = "capability_not_granted"
    KILL_SWITCH = "kill_switch"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    HOST_NOT_ALLOWED = "host_not_allowed"
    CREDENTIALS_IN_URL = "credentials_in_url"
    PORT_NOT_ALLOWED = "port_not_allowed"
    PRIVATE_ADDRESS = "private_address"
    UNRESOLVABLE_HOST = "unresolvable_host"
    MALFORMED_ARGUMENT = "malformed_argument"
    AUTONOMY_FORBIDS = "autonomy_forbids"
    APPROVAL_REQUIRED = "approval_required"
    OPEN_USER_QUESTIONS = "open_user_questions"
    FIELDS_UNRESOLVED = "fields_unresolved"
    POSTING_CLOSED = "posting_closed"
    DAILY_CAP_REACHED = "daily_cap_reached"
    COMPANY_CAP_REACHED = "company_cap_reached"
    OUTSIDE_APPLY_WINDOW = "outside_apply_window"


class PolicyContext(AriaMessage):
    """The facts a decision is made against. Assembled by ARIA, never by a model.

    Everything here is S0 or S1: counts, flags, hostnames. No candidate data
    reaches the policy engine, so a decision can be logged in full.
    """

    SCHEMA_VERSION = 1

    autonomy_level: Annotated[AutonomyLevel, Sensitivity.S0]
    #: Exactly the capabilities this task was granted, from the signed ApplyTask.
    granted_capabilities: Annotated[frozenset[Capability], Sensitivity.S0] = frozenset()
    #: Hosts this task may reach: the posting's ATS host plus allowlisted SSO and
    #: asset hosts for that ATS. An entry starting with "." matches subdomains.
    allowed_hosts: Annotated[tuple[str, ...], Sensitivity.S0] = ()
    #: True when the global or per-user kill switch is engaged (PRODUCT_SPEC §3.8).
    kill_switch_engaged: Annotated[bool, Sensitivity.S0] = False

    # ---- submission preconditions
    approval_granted: Annotated[bool, Sensitivity.S0] = False
    is_dream_company: Annotated[bool, Sensitivity.S0] = False
    """Dream companies are always Approve, whatever the autonomy level."""
    posting_open: Annotated[bool, Sensitivity.S0] = True
    required_fields_resolved: Annotated[bool, Sensitivity.S0] = False
    open_user_questions: Annotated[int, Sensitivity.S0] = Field(default=0, ge=0)
    within_apply_window: Annotated[bool, Sensitivity.S0] = True
    applications_today: Annotated[int, Sensitivity.S0] = Field(default=0, ge=0)
    daily_cap: Annotated[int, Sensitivity.S0] = Field(default=0, ge=0)
    applications_for_company_today: Annotated[int, Sensitivity.S0] = Field(default=0, ge=0)
    company_cap: Annotated[int, Sensitivity.S0] = Field(default=0, ge=0)


class PolicyRequest(AriaMessage):
    """One proposed action.

    ``arguments`` holds scalars only — a URL, a field name, a file id. Anything
    structured would be a place for untrusted content to hide.
    """

    SCHEMA_VERSION = 1

    capability: Annotated[Capability, Sensitivity.S0]
    actor_id: Annotated[str, Sensitivity.S0] = Field(max_length=200)
    subject_id: Annotated[str, Sensitivity.S0] = Field(max_length=200)
    arguments: Annotated[dict[str, str], Sensitivity.S1] = Field(default_factory=dict)
    context: PolicyContext

    @model_validator(mode="after")
    def _arguments_are_short_scalars(self) -> Self:
        for key, value in self.arguments.items():
            if len(value) > 2048:
                raise ValueError(f"policy argument {key!r} is too long to be a scalar")
        return self


class PolicyDecision(AriaMessage):
    """The engine's answer. Audited whether it allows or denies."""

    SCHEMA_VERSION = 1

    decision: Annotated[Decision, Sensitivity.S0]
    capability: Annotated[Capability, Sensitivity.S0]
    reason_code: Annotated[ReasonCode, Sensitivity.S0]
    reason: Annotated[str, Sensitivity.S0] = Field(default="", max_length=500)
    #: Names of the checks that passed before the decision was reached. Makes an
    #: allow auditable, not just a deny.
    checks_passed: Annotated[tuple[str, ...], Sensitivity.S0] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise PermissionError(f"{self.capability.value} denied ({self.reason_code.value}): {self.reason}")
