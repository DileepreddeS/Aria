"""The LLM gateway's HTTP client: an :class:`~aria_core.schemas.llm.LlmClient` for callers.

The engine is handed one of these and never builds it (its boundary test refuses this
module). A caller (the eval now, the API from step 11) builds it with the tenant and the
run the requests belong to, so every request carries both: the gateway caps spend per run
and per month and refuses a request without a run (DECISIONS 2026-10-20).

Every failure becomes :class:`~aria_core.schemas.llm.LlmUnavailable`, which a stage meets by
falling back, never by guessing. Its message names the status and the gateway's reason code
(``spend_cap_run``, ``model_not_priced``...), never text from the request or the response:
the gateway does not repeat payloads, and this client does not start.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from pydantic import SecretStr

from aria_core.schemas.llm import LlmReply, LlmRequest, LlmUnavailable

__all__ = ["DEFAULT_MODEL", "SERVICE_TOKEN_HEADER", "GatewayLlmClient"]

#: The model the gateway prices and the vetting covers (docs/vetting/2026-10-20-anthropic-sdk.md).
DEFAULT_MODEL = "claude-sonnet-5-5"
#: The gateway's header name (aria_llm_gateway.app), not a secret.
SERVICE_TOKEN_HEADER = "x-aria-service-token"  # noqa: S105


class GatewayLlmClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: SecretStr,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        model: str = DEFAULT_MODEL,
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/v1/complete"
        self._token = service_token
        self.tenant_id = tenant_id
        self.run_id = run_id
        self.model = model
        self._timeout = timeout_s
        self._transport = transport

    def __repr__(self) -> str:
        return f"GatewayLlmClient(url={self._url!r}, tenant={self.tenant_id}, run={self.run_id})"

    def body(self, request: LlmRequest) -> dict[str, Any]:
        """The gateway's CompletionRequest, as JSON. A test holds it to the gateway's schema."""
        return {
            "tenant_id": str(self.tenant_id),
            "run_id": str(self.run_id),
            "purpose": request.purpose.value,
            "model": self.model,
            "trusted_instructions": request.trusted_instructions,
            "untrusted_content": [block.model_dump(mode="json") for block in request.untrusted_content],
            "inputs": dict(request.inputs),
            "max_output_tokens": request.max_output_tokens,
        }

    async def complete(self, request: LlmRequest) -> LlmReply:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as http:
                response = await http.post(
                    self._url,
                    json=self.body(request),
                    headers={SERVICE_TOKEN_HEADER: self._token.get_secret_value()},
                )
        except httpx.HTTPError as error:
            raise LlmUnavailable(f"the gateway could not be reached ({type(error).__name__})") from None
        if response.status_code != 200:
            reason = ""
            try:
                detail = response.json().get("detail")
                if isinstance(detail, dict):
                    reason = str(detail.get("reason") or detail.get("error") or "")
            except ValueError:
                pass
            raise LlmUnavailable(
                f"the gateway answered {response.status_code}{f' ({reason})' if reason else ''}"
            )
        payload = response.json()
        return LlmReply(
            text=str(payload["text"]),
            model=str(payload["model"]),
            provider=str(payload["provider"]),
            stop_reason=str(payload.get("stop_reason", "")),
        )
