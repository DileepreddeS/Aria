"""Model providers (SECURITY.md §13).

The gateway is the only component that holds provider keys. :class:`EchoProvider` sends
nothing and keeps the boundary testable with no account; :class:`AnthropicProvider` is the
one real provider (DECISIONS 2026-10-16), vetted in docs/vetting/2026-10-20-anthropic-sdk.md.

The interface stays narrow so a provider cannot change what the gateway does with data: it
is handed assembled blocks and returns text and token counts. What those tokens cost is
the gateway's business, from its own price table, never the provider's.

A provider also says how a failure should be counted. :class:`ProviderRejected` means the
provider answered with an error, which is not billed; any other exception (a timeout, a
dropped connection) may have been billed, and the gateway keeps its reservation until it
is reconciled (DECISIONS 2026-10-20).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from aria_llm_gateway.prices import Usage
from aria_llm_gateway.prompt import PromptBlock

if TYPE_CHECKING:
    from anthropic.types import MessageParam, OutputConfigParam, TextBlockParam

__all__ = [
    "ANTHROPIC_BASE_URL",
    "AnthropicProvider",
    "Completion",
    "EchoProvider",
    "ModelDeclined",
    "Provider",
    "ProviderRejected",
]

Effort = Literal["low", "medium", "high"]

#: Passed explicitly, so ANTHROPIC_BASE_URL can never redirect the gateway's traffic.
ANTHROPIC_BASE_URL: Final = "https://api.anthropic.com"


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: Usage
    #: The provider's reason for stopping: "end_turn", "max_tokens" (the text is cut off)...
    stop_reason: str = ""


class ModelDeclined(Exception):
    """The model refused the request (stop_reason "refusal"). Carries what it used, no text."""

    def __init__(self, category: str | None, usage: Usage) -> None:
        super().__init__(f"the model declined ({category or 'no category'})")
        self.category = category
        self.usage = usage


class ProviderRejected(Exception):
    """The provider answered with an error status. Failed requests are not billed.

    Anthropic: "You're billed only for successful API calls and completed tasks. Failed
    requests aren't charged." A timeout or disconnect is not this: it "is still charged".
    """

    def __init__(self, kind: str, status_code: int) -> None:
        super().__init__(f"the provider answered {status_code} ({kind})")
        self.kind = kind
        self.status_code = status_code


class Provider(Protocol):
    @property
    def name(self) -> str: ...

    #: False for a provider that costs nothing; its requests reserve nothing against the caps.
    @property
    def billed(self) -> bool: ...

    async def count_input_tokens(
        self,
        blocks: tuple[PromptBlock, ...],
        *,
        model: str,
        response_schema: dict[str, Any] | None = None,
    ) -> int:
        """Exactly how many input tokens these blocks are, without generating anything."""
        ...

    async def complete(
        self,
        blocks: tuple[PromptBlock, ...],
        *,
        model: str,
        max_tokens: int,
        response_schema: dict[str, Any] | None = None,
        effort: Effort = "medium",
    ) -> Completion:
        """Send the assembled blocks and return the completion."""
        ...


class EchoProvider:
    """Returns a deterministic summary of what it was asked, and sends nothing.

    Used by the tests and by local development, so the whole path (guards, prompt
    assembly, accounting, audit) runs with no provider account, no key and no network.
    It deliberately does not echo the blocks' text: a test that passes because the
    plaintext came back would be testing nothing useful.
    """

    name = "echo"
    billed = False

    async def count_input_tokens(
        self,
        blocks: tuple[PromptBlock, ...],
        *,
        model: str,
        response_schema: dict[str, Any] | None = None,
    ) -> int:
        return sum(len(block.text) for block in blocks) // 4

    async def complete(
        self,
        blocks: tuple[PromptBlock, ...],
        *,
        model: str,
        max_tokens: int,
        response_schema: dict[str, Any] | None = None,
        effort: Effort = "medium",
    ) -> Completion:
        trusted = sum(1 for block in blocks if not block.untrusted)
        untrusted = sum(1 for block in blocks if block.untrusted)
        characters = sum(len(block.text) for block in blocks)
        return Completion(
            text=f"echo: model={model} blocks={trusted}+{untrusted} chars={characters}",
            # A rough stand-in so accounting has something to record.
            usage=Usage(input_tokens=characters // 4, output_tokens=16),
            stop_reason="end_turn",
        )


@dataclass
class AnthropicProvider:
    """Claude through the official SDK.

    The system block becomes the system prompt; the task inputs and each untrusted block
    become separate content blocks of one user message, in the assembler's order and with
    its labels, so nothing untrusted ever reaches the system prompt. The key and the base
    URL are passed explicitly, which turns off every credential and endpoint the SDK
    would otherwise read from the environment or a profile on disk.

    **No retries by default.** The SDK retries timeouts as well as errors, and an attempt
    that timed out may have been billed even when the last one returned an error. With one
    attempt per request, the gateway knows which kind of failure it is counting. A caller
    that wants another try makes another request, and the caps see it.

    ``http_client`` exists for the tests: an ``httpx2.AsyncClient`` over a mock transport,
    so they can see exactly what would be sent and answer however they like.
    """

    api_key: str = field(repr=False)
    inference_geo: Literal["us", "global"] = "us"
    timeout_s: float = 60.0
    max_retries: int = 0
    http_client: Any = None
    name: str = field(default="anthropic", init=False)
    billed: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        # Imported here: only the gateway's anthropic path needs the SDK loaded.
        import anthropic

        self._client = anthropic.AsyncAnthropic(
            api_key=self.api_key,
            base_url=ANTHROPIC_BASE_URL,
            timeout=self.timeout_s,
            max_retries=self.max_retries,
            http_client=self.http_client,
        )

    def _shape(
        self, blocks: tuple[PromptBlock, ...], response_schema: dict[str, Any] | None, effort: Effort
    ) -> tuple[str, list[MessageParam], OutputConfigParam]:
        (system,) = [block.text for block in blocks if block.role == "system"]
        content: list[TextBlockParam] = [
            {"type": "text", "text": block.text} for block in blocks if block.role == "user"
        ]
        messages: list[MessageParam] = [{"role": "user", "content": content}]
        output_config: OutputConfigParam = {"effort": effort}
        if response_schema is not None:
            # A convenience that saves retries. The caller still validates the reply against
            # its own schema and quotes; a reply that breaks it is that caller's failure path.
            output_config["format"] = {"type": "json_schema", "schema": response_schema}
        return system, messages, output_config

    async def count_input_tokens(
        self,
        blocks: tuple[PromptBlock, ...],
        *,
        model: str,
        response_schema: dict[str, Any] | None = None,
    ) -> int:
        import anthropic

        system, messages, output_config = self._shape(blocks, response_schema, "medium")
        try:
            counted = await self._client.messages.count_tokens(
                model=model, system=system, messages=messages, output_config=output_config
            )
        except anthropic.APIStatusError as error:
            raise ProviderRejected(type(error).__name__, error.status_code) from error
        return counted.input_tokens

    async def complete(
        self,
        blocks: tuple[PromptBlock, ...],
        *,
        model: str,
        max_tokens: int,
        response_schema: dict[str, Any] | None = None,
        effort: Effort = "medium",
    ) -> Completion:
        import anthropic

        system, messages, output_config = self._shape(blocks, response_schema, effort)
        try:
            message = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config=output_config,
                inference_geo=self.inference_geo,
            )
        except anthropic.APIStatusError as error:
            # The API answered: not billed. Timeouts and connection errors are not caught
            # here, so the gateway counts them as possibly billed.
            raise ProviderRejected(type(error).__name__, error.status_code) from error
        reported = message.usage
        usage = Usage(
            input_tokens=reported.input_tokens,
            output_tokens=reported.output_tokens,
            cache_write_tokens=reported.cache_creation_input_tokens or 0,
            cache_read_tokens=reported.cache_read_input_tokens or 0,
        )
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise ModelDeclined(getattr(details, "category", None), usage)
        text = "".join(block.text for block in message.content if block.type == "text")
        return Completion(text=text, usage=usage, stop_reason=message.stop_reason or "")
