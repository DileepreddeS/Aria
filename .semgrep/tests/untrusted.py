"""Cases for .semgrep/untrusted.yml, checked by `semgrep --test` (make sast, ./tasks.ps1 sast).

A `ruleid:` comment says the next line must be reported; an `ok:` comment says it must not.
semgrep's default ignore list skips tests/ directories, so the main scan never reads this file.
"""

from __future__ import annotations

from aria_core.schemas.llm import LlmPurpose, LlmRequest
from aria_core.schemas.untrusted import UntrustedContent
from aria_llm_gateway.prompt import PREAMBLE, PromptBlock
from aria_llm_gateway.schemas import CompletionRequest

INSTRUCTIONS = "Read the posting and return the schema."


# --- untrusted text reaching an instruction: reported ---


def posting_in_an_f_string(posting: UntrustedContent) -> LlmRequest:
    # ruleid: aria-untrusted-text-not-in-instructions
    return LlmRequest(purpose=LlmPurpose.PARSE_JD, trusted_instructions=f"{INSTRUCTIONS}\n{posting.text}")


def posting_through_a_variable(posting: UntrustedContent) -> LlmRequest:
    instructions = INSTRUCTIONS + "\nThe posting says: " + posting.text
    # ruleid: aria-untrusted-text-not-in-instructions
    return LlmRequest(purpose=LlmPurpose.PARSE_JD, trusted_instructions=instructions)


def postings_joined(postings: list[UntrustedContent]) -> LlmRequest:
    joined = "\n".join(posting.text for posting in postings)
    instructions = "Read: {}".format(joined)  # noqa: UP032  (str.format is the case under test)
    # ruleid: aria-untrusted-text-not-in-instructions
    return LlmRequest(purpose=LlmPurpose.PARSE_JD, trusted_instructions=instructions)


def gateway_request_with_the_posting(posting: UntrustedContent) -> CompletionRequest:
    return CompletionRequest(
        tenant_id="t",
        run_id="00000000-0000-0000-0000-000000000000",
        purpose=LlmPurpose.PARSE_JD,
        model="m",
        # ruleid: aria-untrusted-text-not-in-instructions
        trusted_instructions=f"{INSTRUCTIONS} {posting.text}",
    )


def system_block_with_the_posting(content: UntrustedContent) -> PromptBlock:
    # ruleid: aria-untrusted-text-not-in-instructions
    return PromptBlock(role="system", label="instructions", text=f"{PREAMBLE}\n\n{content.text}")


# --- untrusted text kept as data, and .text that is not near a prompt: not reported ---


def posting_as_its_own_block(posting: UntrustedContent) -> LlmRequest:
    # ok: aria-untrusted-text-not-in-instructions
    return LlmRequest(
        purpose=LlmPurpose.PARSE_JD,
        trusted_instructions=INSTRUCTIONS,
        untrusted_content=(posting,),
    )


def untrusted_block_in_the_gateway(content: UntrustedContent) -> PromptBlock:
    # ok: aria-untrusted-text-not-in-instructions
    return PromptBlock(role="user", label="untrusted", text=f"--- BEGIN ---\n{content.text}\n--- END ---")


def an_error_message(item: UntrustedContent, url: str) -> str:
    # ok: aria-untrusted-text-not-in-instructions
    return f"{item.text} has no link to {url}"


def a_continuation_line_joined(last: UntrustedContent, line: UntrustedContent) -> str:
    # ok: aria-untrusted-text-not-in-instructions
    return f"{last.text} {line.text}"
