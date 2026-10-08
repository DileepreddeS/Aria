"""The ApplyTask and its results (PRODUCT_SPEC §4, SECURITY.md §2.2, §8).

The Runner executes on the user's own device, drives a browser through hostile
pages, and has no database credentials and no platform API keys. Everything it is
allowed to do for one application arrives in one signed, short-lived ApplyTask.

The task carries **references, not values**. It names the answer keys the form will
need, not the answers; the resume file id and its hash, not the file. The Runner
fetches each value through a narrow getter when it fills that field (SECURITY.md
§4), so a captured task is not a copy of the candidate's profile — it is a list of
what this one application needs. :class:`ApplyTask` therefore holds no S2 data and
no name, email or address at all, which a test asserts.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from aria_core.schemas.base import AriaMessage, utc_now
from aria_core.schemas.policy import AutonomyLevel, Capability
from aria_core.sensitivity import Sensitivity

__all__ = ["ActionKind", "ActionPlan", "ActionResult", "ApplyTask", "PlannedAction"]


class ApplyTask(AriaMessage):
    """One application's worth of permission, valid for minutes.

    ``jti`` is the task's identity and the basis of replay protection: the Runner
    records it when the task is consumed, and a second attempt with the same id is
    refused (see :mod:`aria_core.apply_task.replay`).
    """

    SCHEMA_VERSION = 1

    jti: Annotated[uuid.UUID, Sensitivity.S0]
    tenant_id: Annotated[uuid.UUID, Sensitivity.S0]
    application_id: Annotated[uuid.UUID, Sensitivity.S0]
    issued_at: Annotated[dt.datetime, Sensitivity.S0]
    expires_at: Annotated[dt.datetime, Sensitivity.S0]

    #: Exactly the capabilities this task needs. Never "everything" (SECURITY.md §4).
    capabilities: Annotated[frozenset[Capability], Sensitivity.S0]
    #: The posting's ATS host plus allowlisted SSO and asset hosts for it.
    allowed_hosts: Annotated[tuple[str, ...], Sensitivity.S0]
    target_url: Annotated[str, Sensitivity.S0] = Field(max_length=2048)
    autonomy_level: Annotated[AutonomyLevel, Sensitivity.S0]

    #: Keys of approved answers, not the answers. The Runner asks for each value
    #: when it fills that field, so this task is not a copy of the profile.
    answer_keys: Annotated[tuple[str, ...], Sensitivity.S0] = ()
    #: The one file this task may upload, with the hash the policy engine checks.
    resume_file_id: Annotated[uuid.UUID | None, Sensitivity.S0] = None
    resume_sha256: Annotated[str | None, Sensitivity.S0] = None

    @model_validator(mode="after")
    def _window_is_short_and_ordered(self) -> Self:
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("issued_at and expires_at must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if (self.expires_at - self.issued_at) > dt.timedelta(minutes=30):
            raise ValueError(
                "an ApplyTask is valid for minutes, not hours: a long-lived task is a long-lived "
                "permission to act on someone's behalf (SECURITY.md §8)"
            )
        if Capability.UPLOAD_RESUME in self.capabilities and not self.resume_sha256:
            raise ValueError("UPLOAD_RESUME was granted without naming the file's hash")
        return self

    def is_expired(self, *, now: dt.datetime | None = None) -> bool:
        return (now or utc_now()) >= self.expires_at


class ActionKind(StrEnum):
    """What the browser agent proposes to do. One kind per capability it needs."""

    NAVIGATE = "navigate"
    FILL_FIELD = "fill_field"
    SELECT_OPTION = "select_option"
    UPLOAD_FILE = "upload_file"
    CLICK = "click"
    SUBMIT = "submit"
    WAIT_FOR_USER = "wait_for_user"


class PlannedAction(AriaMessage):
    """One step a model proposed. Nothing here has happened yet.

    ``value_ref`` is an answer key, never a literal value: the policy engine checks
    that the reference was approved for this task, and the value is injected at the
    moment of filling (SECURITY.md §4).
    """

    SCHEMA_VERSION = 1

    kind: Annotated[ActionKind, Sensitivity.S0]
    #: The perception layer's id for the element, not a CSS selector the model wrote.
    element_id: Annotated[str, Sensitivity.S0] = Field(default="", max_length=200)
    value_ref: Annotated[str, Sensitivity.S0] = Field(default="", max_length=200)
    url: Annotated[str, Sensitivity.S0] = Field(default="", max_length=2048)
    #: The model's own reason, kept for the audit trail and for debugging a recipe.
    rationale: Annotated[str, Sensitivity.S0] = Field(default="", max_length=500)


class ActionPlan(AriaMessage):
    """What the planner returned for one page. Every step still passes the policy."""

    SCHEMA_VERSION = 1

    jti: Annotated[uuid.UUID, Sensitivity.S0]
    page_signature: Annotated[str, Sensitivity.S0] = Field(default="", max_length=200)
    actions: Annotated[tuple[PlannedAction, ...], Sensitivity.S0]
    #: Set when the model could not proceed: an unknown question, a CAPTCHA, a login
    #: wall. The orchestrator turns this into a UserQuestion rather than a guess.
    needs_user: Annotated[str, Sensitivity.S0] = Field(default="", max_length=500)


class ActionResult(AriaMessage):
    """What actually happened, as verified after the fact.

    ``verified`` is not "the click returned without error": it is "the value is
    really set, no new validation error appeared, and the expected state change
    happened" (PRODUCT_SPEC §3.6).
    """

    SCHEMA_VERSION = 1

    jti: Annotated[uuid.UUID, Sensitivity.S0]
    kind: Annotated[ActionKind, Sensitivity.S0]
    element_id: Annotated[str, Sensitivity.S0] = Field(default="", max_length=200)
    succeeded: Annotated[bool, Sensitivity.S0]
    verified: Annotated[bool, Sensitivity.S0] = False
    #: Error text read off the page. Untrusted in origin, so it is never an
    #: instruction and is redacted before it reaches a log line.
    error_text: Annotated[str, Sensitivity.S1] = Field(default="", max_length=2000)
    observed_at: Annotated[dt.datetime, Sensitivity.S0] = Field(default_factory=utc_now)
