"""How ARIA asks a model for something, without knowing which model or where.

The shared vocabulary of a model call. An engine or a service builds an :class:`LlmRequest`
and hands it to an :class:`LlmClient` its caller supplies: the gateway's HTTP client
(:mod:`aria_core.llm_client`) in production, a scripted double in tests. Only the LLM
gateway talks to a provider (CLAUDE.md); everything else reaches it through this protocol.

The request mirrors the gateway's own: developer-written instructions, untrusted content in
its own labelled field (SECURITY.md §5), and minimised inputs whose keys the gateway checks
against the purpose. The purposes are defined here once, and the gateway uses these.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import Field

from aria_core.schemas.base import AriaMessage
from aria_core.schemas.untrusted import UntrustedContent
from aria_core.sensitivity import Sensitivity

__all__ = ["LlmClient", "LlmPurpose", "LlmReply", "LlmRequest", "LlmUnavailable"]


class LlmPurpose(StrEnum):
    """Why a model is being called. Decides what may be sent (data minimisation)."""

    PARSE_JD = "parse_jd"
    WRITE_BULLET = "write_bullet"
    SIX_SECOND_CHECK = "six_second_check"
    CLASSIFY_QUESTION = "classify_question"
    ANSWER_OPEN_ENDED = "answer_open_ended"
    SUMMARISE_PAGE = "summarise_page"
    CLASSIFY_EMAIL = "classify_email"


class LlmRequest(AriaMessage):
    purpose: Annotated[LlmPurpose, Sensitivity.S0]
    #: Written by ARIA's developers. Never built from a posting, a page or an email.
    trusted_instructions: Annotated[str, Sensitivity.S0] = Field(max_length=20_000)
    #: Content from outside, each block labelled with its source.
    untrusted_content: Annotated[tuple[UntrustedContent, ...], Sensitivity.S1] = ()
    inputs: Annotated[dict[str, str], Sensitivity.S1] = Field(default_factory=dict)
    max_output_tokens: Annotated[int, Sensitivity.S0] = Field(default=1024, ge=1, le=16_000)


class LlmReply(AriaMessage):
    text: Annotated[str, Sensitivity.S1]
    model: Annotated[str, Sensitivity.S0]
    provider: Annotated[str, Sensitivity.S0]
    #: "max_tokens" means the text is cut off; a caller expecting JSON will find it invalid.
    stop_reason: Annotated[str, Sensitivity.S0] = ""


class LlmUnavailable(Exception):
    """No model answered. A stage that needs one falls back or returns needs_review; it never guesses."""


class LlmClient(Protocol):
    async def complete(self, request: LlmRequest) -> LlmReply: ...
