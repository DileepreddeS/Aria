"""How the engine asks a model for something, without knowing which model or where.

Three stages use a model (RESUME_ENGINE §2): the JD parser (1), the bullet writer (5b) and
the 6-second check (6c). Each sends an :class:`LlmRequest` through an :class:`LlmClient`
the caller supplies. In production that client is the LLM gateway's HTTP client, built by
the caller; the engine never builds one, and its boundary test refuses the import. In
tests it is :class:`ScriptedLlm`, which answers from a script and never invents a reply.

The request, reply and protocol are aria_core's (:mod:`aria_core.schemas.llm`), shared with
the client and the gateway. What is the engine's own is which purposes it uses and which
inputs each may send; a test compares those with the gateway's lists, so the engine cannot
build a request the gateway would refuse.
"""

from __future__ import annotations

from collections.abc import Callable

from aria_core.schemas.llm import LlmClient, LlmPurpose, LlmReply, LlmRequest, LlmUnavailable

__all__ = [
    "PURPOSE_INPUTS",
    "LlmClient",
    "LlmPurpose",
    "LlmReply",
    "LlmRequest",
    "LlmUnavailable",
    "ScriptedLlm",
]

#: The engine's three model calls and the inputs each may send. Must stay within the
#: gateway's PURPOSE_INPUT_KEYS.
PURPOSE_INPUTS: dict[LlmPurpose, frozenset[str]] = {
    LlmPurpose.PARSE_JD: frozenset({"response_schema"}),
    LlmPurpose.WRITE_BULLET: frozenset(
        {"fact_texts", "metrics", "allowed_tech", "target_requirement", "style_rules"}
    ),
    LlmPurpose.SIX_SECOND_CHECK: frozenset({"top_requirements", "rendered_top_third"}),
}

Reply = str | LlmReply | Callable[[LlmRequest], str]


class ScriptedLlm:
    """A test double: answers each purpose from a script, in order, and records every request.

    A request with no scripted answer left raises :class:`LlmUnavailable`, the same thing
    a stage sees when the gateway is down, so a test exercises the real failure path
    rather than a reply someone forgot to write. A scripted :class:`LlmReply` is returned as
    it is, so a test can script a stop reason (a reply cut off at ``max_tokens``).
    """

    def __init__(self, script: dict[LlmPurpose, list[Reply]] | None = None) -> None:
        self._script = {purpose: list(replies) for purpose, replies in (script or {}).items()}
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmReply:
        self.requests.append(request)
        if request.purpose not in PURPOSE_INPUTS:
            raise ValueError(f"the engine makes no {request.purpose.value} calls")
        unexpected = sorted(set(request.inputs) - PURPOSE_INPUTS[request.purpose])
        if unexpected:
            raise ValueError(f"{request.purpose.value} does not send {unexpected}")
        replies = self._script.get(request.purpose)
        if not replies:
            raise LlmUnavailable(f"no scripted reply for {request.purpose.value}")
        reply = replies.pop(0)
        if isinstance(reply, LlmReply):
            return reply
        text = reply(request) if callable(reply) else reply
        return LlmReply(text=text, model="scripted", provider="test", stop_reason="end_turn")
