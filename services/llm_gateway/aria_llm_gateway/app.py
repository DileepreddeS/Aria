"""The gateway service (SECURITY.md §2, §13).

The only component permitted to talk to a model provider, and therefore the only
one that has to be trusted with the rule that S2 and S3 data never leaves. It is
internal: loopback bind, a service token on every request, and no route that returns
anything about another tenant.

What it records per request is metadata — tenant, purpose, provider, model, tokens,
cost, outcome — and never the prompt, the completion or the untrusted content. A
refusal records the pattern or field names that caused it and nothing else.
"""

from __future__ import annotations

import decimal
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.audit.log import append_event
from aria_core.config import Settings, assert_no_migration_credentials, get_settings
from aria_core.db.models import LlmRequest
from aria_core.db.session import create_engine, tenant_transaction
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    SubjectKind,
)
from aria_core.schemas.identity import TenantId
from aria_llm_gateway.guards import Refusal, inspect_request
from aria_llm_gateway.prompt import assemble
from aria_llm_gateway.providers import EchoProvider, Provider
from aria_llm_gateway.schemas import CompletionRequest, CompletionResponse, RefusalReason

__all__ = ["create_app"]

logger = structlog.get_logger(__name__)

#: The header name, not a secret. (S105 flags the assignment; it is a header.)
SERVICE_TOKEN_HEADER = "x-aria-service-token"  # noqa: S105


def _check_service_token(settings: Settings, presented: str | None) -> None:
    """Constant-time comparison, and a refusal when no token is configured.

    An unset token is a misconfiguration, not an invitation: the gateway can reach
    model providers and spend money, so it never serves an unauthenticated caller.
    """
    expected = settings.llm_gateway_service_token
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the gateway has no service token configured and will not serve requests",
        )
    if presented is None or not secrets.compare_digest(presented, expected.get_secret_value()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service token")


async def _record(
    engine: AsyncEngine,
    *,
    tenant_id: TenantId,
    request: CompletionRequest,
    provider: str,
    outcome: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: decimal.Decimal = decimal.Decimal(0),
    refusal: Refusal | None = None,
) -> None:
    """One metadata row and one audit event, in the same transaction.

    The audit payload names what was blocked and where — pattern names, field names —
    and never the value that matched. A log of what was too sensitive to send would
    be a poor place to keep it.
    """
    async with tenant_transaction(engine, tenant_id) as session:
        await session.execute(
            insert(LlmRequest).values(
                tenant_id=tenant_id,
                purpose=request.purpose.value,
                provider=provider,
                model=request.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
                outcome=outcome,
                refusal_reason=refusal.code if refusal else None,
            )
        )
        payload: dict[str, object] = {
            "purpose": request.purpose.value,
            "model": request.model,
            "provider": provider,
            "untrusted_blocks": len(request.untrusted_content),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        if refusal is not None:
            payload |= {
                "blocked": True,
                "refusal_reason": refusal.reason.value,
                "patterns": ",".join(refusal.details),
                "location": refusal.location,
            }
        await append_event(
            session,
            AuditEventDraft(
                action=(
                    AuditAction.LLM_REQUEST_REFUSED
                    if refusal is not None
                    else AuditAction.LLM_REQUEST_DISPATCHED
                ),
                actor_kind=ActorKind.SYSTEM,
                actor_id="llm_gateway",
                subject_kind=SubjectKind.LLM_REQUEST,
                subject_id=request.purpose.value,
                outcome=AuditOutcome.DENIED if refusal is not None else AuditOutcome.OK,
                reason=refusal.reason.value if refusal is not None else "",
                payload=payload,
            ),
            tenant_id=tenant_id,
        )


def create_app(
    *,
    settings: Settings | None = None,
    provider: Provider | None = None,
    engine: AsyncEngine | None = None,
) -> FastAPI:
    """Build the service. Dependencies are injected so tests need no real provider."""
    resolved_settings = settings or get_settings()
    resolved_provider = provider or EchoProvider()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The gateway holds provider keys; it must never also hold the migration
        # role's credentials (SECURITY.md §2.4).
        assert_no_migration_credentials()
        own_engine = engine or create_engine(resolved_settings.database_dsn())
        app.state.engine = own_engine
        logger.info("gateway_started", provider=resolved_provider.name, env=resolved_settings.env)
        try:
            yield
        finally:
            if engine is None:
                await own_engine.dispose()

    app = FastAPI(
        title="ARIA LLM gateway",
        version="0",
        lifespan=lifespan,
        # No interactive docs on an internal service.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def authorize(
        token: Annotated[str | None, Header(alias=SERVICE_TOKEN_HEADER)] = None,
    ) -> None:
        _check_service_token(resolved_settings, token)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Unauthenticated, and says nothing about configuration or tenants."""
        return {"status": "ok"}

    @app.post("/v1/complete", dependencies=[Depends(authorize)])
    async def complete(http_request: Request) -> CompletionResponse:
        # The raw body is validated with model_validate_json rather than letting
        # FastAPI hand over a parsed dict. ARIA messages run pydantic in strict mode,
        # and strict python-mode validation rejects the JSON representations of enums,
        # datetimes and tuples — which are exactly what a JSON request contains.
        # Validating the bytes applies strict JSON rules instead: no coercion beyond
        # what JSON itself forces.
        try:
            body = CompletionRequest.model_validate_json(await http_request.body())
        except ValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "invalid_request", "errors": error.errors(include_url=False)},
            ) from error

        app_engine: AsyncEngine = http_request.app.state.engine
        tenant_id = TenantId(uuid.UUID(body.tenant_id))

        refusal = inspect_request(body)
        if refusal is not None:
            await _record(
                app_engine,
                tenant_id=tenant_id,
                request=body,
                provider=resolved_provider.name,
                outcome="refused",
                refusal=refusal,
            )
            logger.warning(
                "llm_request_refused",
                purpose=body.purpose.value,
                reason=refusal.reason.value,
                patterns=list(refusal.details),
                location=refusal.location,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "refused",
                    "reason": refusal.reason.value,
                    # Names only. The caller knows its own payload; the response does
                    # not repeat the value back.
                    "patterns": list(refusal.details),
                    "location": refusal.location,
                },
            )

        blocks = assemble(body)
        try:
            completion = await resolved_provider.complete(
                blocks, model=body.model, max_tokens=body.max_output_tokens
            )
        except Exception as error:
            # Recorded and reported as a gateway failure; the provider's own error text
            # never reaches the caller, since it can quote the payload back.
            await _record(
                app_engine,
                tenant_id=tenant_id,
                request=body,
                provider=resolved_provider.name,
                outcome="failed",
                refusal=Refusal(RefusalReason.PROVIDER_ERROR, (type(error).__name__,), "provider"),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="the model provider failed"
            ) from error

        await _record(
            app_engine,
            tenant_id=tenant_id,
            request=body,
            provider=resolved_provider.name,
            outcome="ok",
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            cost_usd=completion.cost_usd,
        )
        return CompletionResponse(
            text=completion.text,
            model=body.model,
            provider=resolved_provider.name,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            cost_usd=str(completion.cost_usd),
        )

    return app
