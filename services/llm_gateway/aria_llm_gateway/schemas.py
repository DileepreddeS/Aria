"""The gateway's request and response contracts (SECURITY.md §5, §13).

The shape of :class:`CompletionRequest` is the first line of the prompt-injection
defence. Trusted instructions and untrusted content are separate fields of separate
types, and the assembler has no path that joins them, so "the job description told
the model to do something else" is not a thing a caller can accidentally arrange.

``inputs`` carries the minimised task data — the facts, the requirement, the
question — and nothing else. Every purpose declares the keys it is allowed to send,
so "why are you interested in this role?" cannot quietly arrive carrying an address
or a visa status (SECURITY.md §13).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, model_validator

from aria_core.schemas.base import AriaMessage

# Defined once, in aria_core, for every caller and the gateway alike.
from aria_core.schemas.llm import LlmPurpose
from aria_core.schemas.untrusted import UntrustedContent
from aria_core.sensitivity import Sensitivity

__all__ = [
    "CANDIDATE_DATA_PURPOSES",
    "PURPOSE_INPUT_KEYS",
    "CompletionRequest",
    "CompletionResponse",
    "LlmPurpose",
    "RefusalReason",
]


#: The only ``inputs`` keys each purpose may carry. A key outside its purpose's set
#: is refused rather than ignored: it means the caller is sending something the task
#: does not need, which is how an address or a visa status reaches a provider.
PURPOSE_INPUT_KEYS: Final[dict[LlmPurpose, frozenset[str]]] = {
    LlmPurpose.PARSE_JD: frozenset({"response_schema"}),
    LlmPurpose.WRITE_BULLET: frozenset(
        {"fact_texts", "metrics", "allowed_tech", "target_requirement", "style_rules"}
    ),
    LlmPurpose.SIX_SECOND_CHECK: frozenset({"top_requirements", "rendered_top_third"}),
    LlmPurpose.CLASSIFY_QUESTION: frozenset({"response_schema"}),
    LlmPurpose.ANSWER_OPEN_ENDED: frozenset(
        {"fact_texts", "motivations", "company_facts", "previous_answers", "length_limit"}
    ),
    LlmPurpose.SUMMARISE_PAGE: frozenset({"response_schema"}),
    LlmPurpose.CLASSIFY_EMAIL: frozenset({"response_schema"}),
}


#: Purposes that carry a candidate's own data (facts, answers, the rendered page): refused for a
#: tenant that has not opted in (aria_core.tenant_settings, DECISIONS 2026-10-23).
CANDIDATE_DATA_PURPOSES: Final[frozenset[LlmPurpose]] = frozenset(
    {LlmPurpose.WRITE_BULLET, LlmPurpose.ANSWER_OPEN_ENDED, LlmPurpose.SIX_SECOND_CHECK}
)


class RefusalReason(StrEnum):
    """Why the gateway would not dispatch. Recorded; never accompanied by the value."""

    SENSITIVE_FIELD = "sensitive_field"
    """A field declared S2 or S3 was present (the structural check)."""
    SENSITIVE_PATTERN = "sensitive_pattern"
    """Free text matched a sensitive pattern (the backstop)."""
    INPUT_NOT_ALLOWED_FOR_PURPOSE = "input_not_allowed_for_purpose"
    INVALID_RESPONSE_SCHEMA = "invalid_response_schema"
    """``inputs.response_schema`` is not a JSON object."""
    MODEL_NOT_PRICED = "model_not_priced"
    """The price table does not know the model, so the caps could not count it."""
    SPEND_CAP_RUN = "spend_cap_run"
    """The request could take the run past its cap."""
    SPEND_CAP_MONTH = "spend_cap_month"
    """The request could take the tenant past its monthly cap."""
    PROVIDER_ERROR = "provider_error"
    MODEL_DECLINED = "model_declined"
    MODEL_WRITING_NOT_ENABLED = "model_writing_not_enabled"
    """The purpose carries candidate data and the tenant has not opted in."""
    """The model refused (stop_reason "refusal"). Recorded with its cost; never retried elsewhere."""


class CompletionRequest(AriaMessage):
    """One call to a model, on behalf of one tenant, for one declared purpose."""

    SCHEMA_VERSION = 1

    tenant_id: Annotated[str, Sensitivity.S0]
    #: The run this request belongs to: one resume build, one eval posting. Its spend is
    #: capped as a whole (DECISIONS 2026-10-20), so every request must say which run it is.
    run_id: Annotated[str, Sensitivity.S0] = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    purpose: Annotated[LlmPurpose, Sensitivity.S0]
    model: Annotated[str, Sensitivity.S0] = Field(max_length=120)
    #: Developer-authored. Never built from anything a page, posting or email said.
    trusted_instructions: Annotated[str, Sensitivity.S0] = Field(max_length=20_000)
    #: Content from outside ARIA's trust boundary. Each block keeps its own label.
    untrusted_content: Annotated[tuple[UntrustedContent, ...], Sensitivity.S1] = ()
    #: Minimised task data. Keys are checked against the purpose.
    inputs: Annotated[dict[str, str], Sensitivity.S1] = Field(default_factory=dict)
    max_output_tokens: Annotated[int, Sensitivity.S0] = Field(default=1024, ge=1, le=16_000)

    @model_validator(mode="after")
    def _inputs_match_the_purpose(self) -> Self:
        allowed = PURPOSE_INPUT_KEYS[self.purpose]
        unexpected = sorted(set(self.inputs) - allowed)
        if unexpected:
            raise ValueError(
                f"purpose {self.purpose.value} does not send {unexpected}; allowed keys are "
                f"{sorted(allowed)} (SECURITY.md \u00a713 data minimisation)"
            )
        return self


class CompletionResponse(AriaMessage):
    """What the gateway returns. Model output, never executed and never a URL."""

    SCHEMA_VERSION = 1

    text: Annotated[str, Sensitivity.S1]
    model: Annotated[str, Sensitivity.S0]
    provider: Annotated[str, Sensitivity.S0]
    prompt_tokens: Annotated[int, Sensitivity.S0] = 0
    completion_tokens: Annotated[int, Sensitivity.S0] = 0
    cost_usd: Annotated[str, Sensitivity.S0] = "0"
    """A decimal string: the wire format should not depend on float formatting."""
    #: Why the model stopped. "max_tokens" means the text is cut off: a caller that wanted
    #: JSON will find it invalid and take its own fallback.
    stop_reason: Annotated[str, Sensitivity.S0] = ""
