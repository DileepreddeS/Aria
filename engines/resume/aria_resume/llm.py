"""How the engine asks a model for something, without knowing which model or where.

Three stages use a model (RESUME_ENGINE §2): the JD parser (1), the bullet writer (5b) and
the 6-second check (6c). Each sends an :class:`LlmRequest` through an :class:`LlmClient`
the caller supplies. In production that client is the LLM gateway, the only component
allowed to call a provider (CLAUDE.md); in tests it is :class:`ScriptedLlm`, which answers
from a script and never invents a reply.

The request mirrors the gateway's own: developer-written instructions, untrusted content
in its own labelled field (SECURITY.md §5), and minimised inputs whose keys the gateway
checks against the purpose. A test compares the purposes and keys here with the
gateway's lists, so the engine cannot build a request the gateway would refuse.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import Field

from aria_core.schemas.base import AriaMessage
from aria_core.schemas.untrusted import UntrustedContent
from aria_core.sensitivity import Sensitivity

__all__ = ["LlmClient", "LlmPurpose", "LlmReply", "LlmRequest", "LlmUnavailable", "ScriptedLlm"]


class LlmPurpose(StrEnum):
    """The engine's three model calls, by the gateway's purpose names."""

    PARSE_JD = "parse_jd"
    WRITE_BULLET = "write_bullet"
    SIX_SECOND_CHECK = "six_second_check"


#: The inputs each purpose may send. Must stay within the gateway's PURPOSE_INPUT_KEYS.
PURPOSE_INPUTS: dict[LlmPurpose, frozenset[str]] = {
    LlmPurpose.PARSE_JD: frozenset({"response_schema"}),
    LlmPurpose.WRITE_BULLET: frozenset(
        {"fact_texts", "metrics", "allowed_tech", "target_requirement", "style_rules"}
    ),
    LlmPurpose.SIX_SECOND_CHECK: frozenset({"top_requirements", "rendered_top_third"}),
}


class LlmRequest(AriaMessage):
    purpose: Annotated[LlmPurpose, Sensitivity.S0]
    #: Written by the engine's developers. Never built from a JD.
    trusted_instructions: Annotated[str, Sensitivity.S0] = Field(max_length=20_000)
    #: The JD and anything else from outside, each block labelled with its source.
    untrusted_content: Annotated[tuple[UntrustedContent, ...], Sensitivity.S1] = ()
    inputs: Annotated[dict[str, str], Sensitivity.S1] = Field(default_factory=dict)
    max_output_tokens: Annotated[int, Sensitivity.S0] = Field(default=1024, ge=1, le=16_000)


class LlmReply(AriaMessage):
    text: Annotated[str, Sensitivity.S1]
    model: Annotated[str, Sensitivity.S0]
    provider: Annotated[str, Sensitivity.S0]


class LlmUnavailable(Exception):
    """No model answered. A stage that needs one returns needs_review; it never guesses."""


class LlmClient(Protocol):
    async def complete(self, request: LlmRequest) -> LlmReply: ...


Reply = str | Callable[[LlmRequest], str]


class ScriptedLlm:
    """A test double: answers each purpose from a script, in order, and records every request.

    A request with no scripted answer left raises :class:`LlmUnavailable`, the same thing
    a stage sees when the gateway is down, so a test exercises the real failure path
    rather than a reply someone forgot to write.
    """

    def __init__(self, script: dict[LlmPurpose, list[Reply]] | None = None) -> None:
        self._script = {purpose: list(replies) for purpose, replies in (script or {}).items()}
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmReply:
        self.requests.append(request)
        unexpected = sorted(set(request.inputs) - PURPOSE_INPUTS[request.purpose])
        if unexpected:
            raise ValueError(f"{request.purpose.value} does not send {unexpected}")
        replies = self._script.get(request.purpose)
        if not replies:
            raise LlmUnavailable(f"no scripted reply for {request.purpose.value}")
        reply = replies.pop(0)
        text = reply(request) if callable(reply) else reply
        return LlmReply(text=text, model="scripted", provider="test")
