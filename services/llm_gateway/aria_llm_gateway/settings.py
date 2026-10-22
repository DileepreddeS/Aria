"""The gateway's provider configuration, kept apart from :class:`aria_core.config.Settings`.

The provider key lives here and nowhere else (DECISIONS 2026-10-16). The API, the CLI and
every other process load ``Settings``, which has no field for it, so none of them can hold
the key by accident: the same separation ``MigrationSettings`` gives the migration role.

The caps are counted from persisted usage (``llm_requests``), never from a counter in
memory, and checked before a request is sent (DECISIONS 2026-10-20).
"""

from __future__ import annotations

import decimal
import os
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["FORBIDDEN_SDK_ENVIRONMENT", "ProviderSettings", "check_sdk_environment"]

#: Variables the Anthropic SDK reads even when the key and base URL are passed explicitly,
#: or that would point it elsewhere. ANTHROPIC_CUSTOM_HEADERS is merged into every request
#: whatever the caller passes, so it could switch on a feature outside the data-handling
#: terms recorded in docs/vetting/2026-10-20-anthropic-sdk.md.
FORBIDDEN_SDK_ENVIRONMENT = ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE")


class ProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARIA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: "echo" sends nothing anywhere; it is the default so development and CI never call out.
    llm_provider: Literal["echo", "anthropic"] = "echo"
    anthropic_api_key: SecretStr | None = None
    #: "us" keeps inference in the US at 1.1x the price (DECISIONS 2026-10-20).
    llm_inference_geo: Literal["us", "global"] = "us"
    #: The most one tenant may spend in a calendar month (UTC), in USD.
    llm_monthly_cap_usd: decimal.Decimal = Field(default=decimal.Decimal("10"), ge=0)
    #: The most one run may spend, in USD: a loop that runs away stops here, not at the month.
    llm_run_cap_usd: decimal.Decimal = Field(default=decimal.Decimal("0.50"), ge=0)
    #: Seconds per request: one attempt, no retries (providers.AnthropicProvider).
    llm_request_timeout_s: float = Field(default=60.0, gt=0, le=600)
    #: A reservation still "dispatching" after this long belongs to a request that will
    #: never settle (its process died), and may be reconciled (aria_llm_gateway.reservations).
    llm_reservation_ttl_s: int = Field(default=1800, ge=60)

    @model_validator(mode="after")
    def _a_real_provider_needs_its_key(self) -> Self:
        if self.llm_provider == "anthropic" and self.anthropic_api_key is None:
            raise ValueError(
                "llm_provider=anthropic needs ARIA_ANTHROPIC_API_KEY; the gateway will not start"
            )
        if self.llm_reservation_ttl_s <= self.llm_request_timeout_s:
            # Otherwise a request still in flight could be reconciled as if it had died.
            raise ValueError("llm_reservation_ttl_s must be longer than llm_request_timeout_s")
        return self


def check_sdk_environment(environ: dict[str, str] | None = None) -> None:
    """Refuse to start while a variable is set that the SDK would act on behind our back."""
    environment = os.environ if environ is None else environ
    present = sorted(name for name in FORBIDDEN_SDK_ENVIRONMENT if environment.get(name))
    if present:
        raise RuntimeError(
            f"the gateway will not start while {', '.join(present)} is set: the Anthropic SDK reads "
            "it whatever the gateway passes (docs/vetting/2026-10-20-anthropic-sdk.md)"
        )
