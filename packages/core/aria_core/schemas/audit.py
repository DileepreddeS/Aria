"""The audit event (PRODUCT_SPEC §4, SECURITY.md §16).

What a caller builds is an :class:`AuditEventDraft`: what happened, who did it, to
what, and what the outcome was. The chain fields — sequence number, previous hash,
hash — are the writer's business, not the caller's, so they live on
:class:`AuditRecord` and cannot be set by the code being audited.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import Field, model_validator

from aria_core.schemas.base import AriaMessage, utc_now
from aria_core.sensitivity import Sensitivity

__all__ = ["ActorKind", "AuditAction", "AuditEventDraft", "AuditOutcome", "AuditRecord", "SubjectKind"]


class ActorKind(StrEnum):
    """Who acted. Distinguishing these is the point of the log."""

    USER = "user"
    SYSTEM = "system"
    """ARIA's own scheduled or reactive code, with no human in the loop."""
    AGENT = "agent"
    """An LLM-driven step. What it proposed, and what the policy engine decided."""
    RUNNER = "runner"
    """The local device, acting on a signed ApplyTask."""
    ADMIN = "admin"


class SubjectKind(StrEnum):
    APPLICATION = "application"
    TENANT = "tenant"
    USER = "user"
    SENSITIVE_FIELD = "sensitive_field"
    FACT = "fact"
    UPLOAD = "upload"
    CANDIDATE = "candidate"
    CREDENTIAL = "credential"
    LLM_REQUEST = "llm_request"
    APPLY_TASK = "apply_task"
    SERVICE = "service"
    USER_QUESTION = "user_question"


class AuditOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    OK = "ok"
    FAILED = "failed"


class AuditAction(StrEnum):
    """Every action ARIA records. Extended by the phase that adds the behaviour.

    A closed set rather than free strings: the audit verifier reconstructs events
    from stored rows and converts this column back through the enum, so an action it
    does not recognise is a loud failure rather than an unexplained row.
    """

    APPLICATION_STATE_CHANGED = "application.state_changed"
    POLICY_DECIDED = "policy.decided"
    FACT_APPROVED = "fact.approved"
    FACT_EDITED = "fact.edited"
    FACT_REJECTED = "fact.rejected"
    FACT_RECONSIDERED = "fact.reconsidered"
    FACT_MERGED = "fact.merged"
    GRAPH_IMPORTED = "graph.imported"
    #: A §5.5 gate refused an assembled plan; nothing was written.
    GRAPH_IMPORT_REFUSED = "graph.import_refused"
    #: A superseded upload's drafts and open questions deleted at the candidate's request.
    GRAPH_WITHDRAWN = "graph.withdrawn"
    GRAPH_RETAGGED = "graph.retagged"
    GRAPH_ENTITY_APPROVED = "graph_entity.approved"
    GRAPH_ENTITY_REJECTED = "graph_entity.rejected"
    GRAPH_ENTITY_RECONSIDERED = "graph_entity.reconsidered"
    USER_QUESTION_ASKED = "user_question.asked"
    USER_QUESTION_ANSWERED = "user_question.answered"
    USER_QUESTION_DISMISSED = "user_question.dismissed"
    #: An answer or a dismissal taken back: the question reopened, or withdrawn so the
    #: next import asks it afresh.
    USER_QUESTION_RECONSIDERED = "user_question.reconsidered"
    UPLOAD_RECEIVED = "upload.received"
    UPLOAD_SCANNED = "upload.scanned"
    UPLOAD_PARSED = "upload.parsed"
    UPLOAD_REFUSED = "upload.refused"
    UPLOAD_ORIGINAL_DELETED = "upload.original_deleted"
    #: A flagged upload's lines checked against the original by the candidate.
    UPLOAD_REVIEWED = "upload.reviewed"
    SENSITIVE_FIELD_READ = "sensitive_field.read"
    SENSITIVE_FIELD_WRITTEN = "sensitive_field.written"
    DATA_KEY_ROTATED = "data_key.rotated"
    LLM_REQUEST_DISPATCHED = "llm_request.dispatched"
    LLM_REQUEST_REFUSED = "llm_request.refused"
    APPLY_TASK_SIGNED = "apply_task.signed"
    APPLY_TASK_CONSUMED = "apply_task.consumed"
    APPLY_TASK_REJECTED = "apply_task.rejected"
    SERVICE_STARTED = "service.started"


class AuditEventDraft(AriaMessage):
    """One thing that happened, as the caller describes it.

    ``payload`` carries S0/S1 context only — ids, state names, decision reasons.
    The writer scans it and the reason for sensitive patterns and redacts rather
    than refuses, because losing an event would be worse than storing a marker
    (see :mod:`aria_core.redaction`).
    """

    SCHEMA_VERSION = 1

    action: Annotated[AuditAction, Sensitivity.S0]
    actor_kind: Annotated[ActorKind, Sensitivity.S0]
    #: A user id, a service name, a model id — never a name or an email.
    actor_id: Annotated[str, Sensitivity.S0] = Field(max_length=200)
    subject_kind: Annotated[SubjectKind, Sensitivity.S0]
    subject_id: Annotated[str, Sensitivity.S0] = Field(max_length=200)
    outcome: Annotated[AuditOutcome, Sensitivity.S0]
    reason: Annotated[str, Sensitivity.S1] = Field(default="", max_length=2000)
    payload: Annotated[dict[str, Any], Sensitivity.S1] = Field(default_factory=dict)
    occurred_at: Annotated[dt.datetime, Sensitivity.S0] = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _payload_is_shallow_and_timestamped(self) -> Self:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        for key, value in self.payload.items():
            if not isinstance(value, str | int | float | bool | None):
                raise ValueError(
                    f"audit payload[{key!r}] is {type(value).__name__}; payloads hold scalars so the "
                    "canonical form used for hashing stays unambiguous"
                )
        return self


class AuditRecord(AriaMessage):
    """A written event, with its place in the chain.

    ``prev_hash`` is the preceding event's hash in the same chain, and ``hash``
    covers this event including ``prev_hash``, so altering or removing any event
    breaks verification from that point on.
    """

    SCHEMA_VERSION = 1

    chain_id: Annotated[str, Sensitivity.S0]
    seq: Annotated[int, Sensitivity.S0] = Field(ge=1)
    prev_hash: Annotated[str, Sensitivity.S0] = Field(min_length=64, max_length=64)
    hash: Annotated[str, Sensitivity.S0] = Field(min_length=64, max_length=64)
    event: AuditEventDraft
    #: Pattern names redacted from ``reason``/``payload`` before writing, if any.
    redactions: Annotated[list[str], Sensitivity.S0] = Field(default_factory=list)
