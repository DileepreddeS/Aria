"""One real request to Anthropic, run only on purpose: it costs money.

Skipped unless ``ARIA_LIVE_LLM=1`` and ``ARIA_ANTHROPIC_API_KEY`` are both set, so CI and an
ordinary ``./tasks.ps1 test`` never call out. Before the first run, the Console checks in
DECISIONS 2026-10-20 must be done (organisation, retention, inference geos, spend limit).

What it sends is a synthetic posting and fixed instructions: no candidate data. It asks
for at most 512 output tokens, a fraction of a cent.
"""

from __future__ import annotations

import json
import os

import pytest

from aria_core.schemas.untrusted import UntrustedContent, UntrustedSourceKind
from aria_llm_gateway.prices import load_prices
from aria_llm_gateway.prompt import assemble
from aria_llm_gateway.providers import AnthropicProvider
from aria_llm_gateway.schemas import CompletionRequest, LlmPurpose
from aria_llm_gateway.settings import ProviderSettings, check_sdk_environment

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ARIA_LIVE_LLM") != "1" or not os.environ.get("ARIA_ANTHROPIC_API_KEY"),
        reason="a live call costs money: set ARIA_LIVE_LLM=1 and ARIA_ANTHROPIC_API_KEY to run it",
    ),
]

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "company": {"type": "string"}},
    "required": ["title", "company"],
    "additionalProperties": False,
}


async def test_one_structured_reading_of_a_synthetic_posting() -> None:
    check_sdk_environment()
    settings = ProviderSettings(llm_provider="anthropic")
    assert settings.anthropic_api_key is not None
    provider = AnthropicProvider(
        api_key=settings.anthropic_api_key.get_secret_value(), inference_geo=settings.llm_inference_geo
    )
    request = CompletionRequest(
        tenant_id="00000000-0000-4000-8000-000000000001",
        run_id="00000000-0000-4000-8000-000000000002",
        purpose=LlmPurpose.PARSE_JD,
        model="claude-sonnet-5-5",
        trusted_instructions="Read the job title and the company name from the posting into the schema.",
        untrusted_content=(
            UntrustedContent(
                source_kind=UntrustedSourceKind.JOB_DESCRIPTION,
                source_ref="synthetic:larkspur",
                text=(
                    "Backend Engineer, Larkspur Analytics\nPortland, OR (hybrid)\n"
                    "Requirements:\n- Python or Go"
                ),
            ),
        ),
        inputs={"response_schema": json.dumps(SCHEMA)},
        max_output_tokens=512,
    )
    completion = await provider.complete(
        assemble(request), model=request.model, max_tokens=512, response_schema=SCHEMA, effort="low"
    )
    reading = json.loads(completion.text)
    assert set(reading) == {"title", "company"}
    assert completion.usage.input_tokens > 0 and completion.usage.output_tokens > 0
    # The reply is priced from our own table, the same way the gateway prices it.
    assert load_prices().cost(request.model, settings.llm_inference_geo, completion.usage) > 0
