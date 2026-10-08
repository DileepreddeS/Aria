"""Model providers (SECURITY.md §13).

The gateway is the only component that will ever hold provider keys. Phase 0 ships
no real provider: an :class:`EchoProvider` makes the boundary testable, and the
first real one arrives in Phase 2 when the resume engine needs a model — together
with the due-diligence entry SECURITY.md §13 requires in DECISIONS.md (retention,
training use off, processing region, logging, zero-retention options, DPA).

Keeping the interface this narrow now means adding a provider later cannot change
what the gateway does with data: it hands over assembled blocks and gets back text
and token counts.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from typing import Protocol

from aria_llm_gateway.prompt import PromptBlock

__all__ = ["Completion", "EchoProvider", "Provider"]


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: decimal.Decimal


class Provider(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, blocks: tuple[PromptBlock, ...], *, model: str, max_tokens: int) -> Completion:
        """Send the assembled blocks and return the completion."""
        ...


class EchoProvider:
    """Returns a deterministic summary of what it was asked, and sends nothing.

    Used by the tests and by local development, so the whole path — guards, prompt
    assembly, accounting, audit — can be exercised with no provider account, no key
    and no network. It deliberately does not echo the blocks' text: a test that
    passes because the plaintext came back would be testing nothing useful.
    """

    name = "echo"

    async def complete(self, blocks: tuple[PromptBlock, ...], *, model: str, max_tokens: int) -> Completion:
        trusted = sum(1 for block in blocks if not block.untrusted)
        untrusted = sum(1 for block in blocks if block.untrusted)
        characters = sum(len(block.text) for block in blocks)
        return Completion(
            text=f"echo: model={model} blocks={trusted}+{untrusted} chars={characters}",
            # A rough stand-in so cost accounting has something to record. Real
            # providers report their own counts.
            prompt_tokens=characters // 4,
            completion_tokens=16,
            cost_usd=decimal.Decimal("0"),
        )
