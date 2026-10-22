"""The gateway service (SECURITY.md §2, §13).

The only component permitted to talk to a model provider, and therefore the only
one that has to be trusted with the rule that S2 and S3 data never leaves. It is
internal: loopback bind, a service token on every request, and no route that returns
anything about another tenant.

What it records per request is metadata — tenant, run, purpose, provider, model, tokens,
cost, outcome — and never the prompt, the completion or the untrusted content. A
refusal records the pattern or field names that caused it and nothing else.

**Spend is capped before anything is sent, from persisted usage** (DECISIONS 2026-10-20).
Each request reserves its worst-case cost (its counted input tokens, its whole output
allowance) in ``llm_requests`` under a per-tenant lock, after checking the month's and the
run's spend; only then is it sent, and its row is settled to what it actually cost. A
failure the provider answered is settled at nothing, since failed requests are not
billed; a timeout or a dropped connection may have been, so its row stays ``unsettled``
at the reservation. A process that dies after sending leaves its row ``dispatching``.
Both still count, so a crash loop cannot walk through a cap the way it would reset a
counter in memory, and both can be reconciled (:mod:`aria_llm_gateway.reservations`), so
fail-closed never means stuck. A request for a model the price table does not know is
refused outright: priced at zero, it would never trip a cap.
"""

from __future__ import annotations

import datetime as dt
import decimal
import functools
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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
from aria_llm_gateway.prices import PriceTable, Usage, load_prices
from aria_llm_gateway.prompt import assemble
from aria_llm_gateway.providers import (
    AnthropicProvider,
    EchoProvider,
    Effort,
    ModelDeclined,
    Provider,
    ProviderRejected,
)
from aria_llm_gateway.reservations import awaiting_reconciliation
from aria_llm_gateway.schemas import CompletionRequest, CompletionResponse, LlmPurpose, RefusalReason
from aria_llm_gateway.settings import ProviderSettings, check_sdk_environment

__all__ = ["EFFORT_BY_PURPOSE", "build_provider", "create_app"]

logger = structlog.get_logger(__name__)

#: The header name, not a secret. (S105 flags the assignment; it is a header.)
SERVICE_TOKEN_HEADER = "x-aria-service-token"  # noqa: S105

#: How hard the model thinks, per purpose. Thinking is billed as output, and reading a
#: posting into a schema needs little of it.
EFFORT_BY_PURPOSE: dict[LlmPurpose, Effort] = {LlmPurpose.PARSE_JD: "low"}
DEFAULT_EFFORT: Effort = "medium"


def build_provider(settings: ProviderSettings) -> Provider:
    if settings.llm_provider == "anthropic":
        assert settings.anthropic_api_key is not None  # the settings validator guarantees it
        return AnthropicProvider(
            api_key=settings.anthropic_api_key.get_secret_value(),
            inference_geo=settings.llm_inference_geo,
            timeout_s=settings.llm_request_timeout_s,
        )
    return EchoProvider()


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


def _payload(request: CompletionRequest, provider: str) -> dict[str, object]:
    return {
        "purpose": request.purpose.value,
        "model": request.model,
        "provider": provider,
        "run_id": request.run_id,
        "untrusted_blocks": len(request.untrusted_content),
    }


async def _audit(
    session: AsyncSession,
    tenant_id: TenantId,
    request: CompletionRequest,
    action: AuditAction,
    outcome: AuditOutcome,
    payload: dict[str, object],
    reason: str = "",
) -> None:
    await append_event(
        session,
        AuditEventDraft(
            action=action,
            actor_kind=ActorKind.SYSTEM,
            actor_id="llm_gateway",
            subject_kind=SubjectKind.LLM_REQUEST,
            subject_id=request.purpose.value,
            outcome=outcome,
            reason=reason,
            payload=payload,
        ),
        tenant_id=tenant_id,
    )


async def _record_refusal(
    engine: AsyncEngine, *, tenant_id: TenantId, request: CompletionRequest, provider: str, refusal: Refusal
) -> None:
    """One metadata row and one audit event, in the same transaction, for a request not sent.

    The audit payload names what was blocked and where — pattern names, field names —
    and never the value that matched. A log of what was too sensitive to send would
    be a poor place to keep it.
    """
    async with tenant_transaction(engine, tenant_id) as session:
        await session.execute(
            insert(LlmRequest).values(
                tenant_id=tenant_id,
                run_id=uuid.UUID(request.run_id),
                purpose=request.purpose.value,
                provider=provider,
                model=request.model,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=decimal.Decimal(0),
                outcome="refused",
                refusal_reason=refusal.code[:200],
            )
        )
        await _audit(
            session,
            tenant_id,
            request,
            AuditAction.LLM_REQUEST_REFUSED,
            AuditOutcome.DENIED,
            _payload(request, provider)
            | {
                "blocked": True,
                "refusal_reason": refusal.reason.value,
                "patterns": ",".join(refusal.details),
                "location": refusal.location,
            },
            reason=refusal.reason.value,
        )


def _month_start(now: dt.datetime) -> dt.datetime:
    return now.astimezone(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _reserve(
    engine: AsyncEngine,
    *,
    tenant_id: TenantId,
    request: CompletionRequest,
    provider: str,
    reserve: decimal.Decimal,
    caps: ProviderSettings,
) -> uuid.UUID | Refusal:
    """Check both caps against persisted spend and, if the request fits, record it as sent.

    The advisory lock serialises one tenant's reservations, so two requests cannot both
    read the same total and both pass.
    """
    run_id = uuid.UUID(request.run_id)
    async with tenant_transaction(engine, tenant_id) as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"llm-spend:{tenant_id}"}
        )
        spent = func.coalesce(func.sum(LlmRequest.cost_usd), 0)
        month = decimal.Decimal(
            (
                await session.execute(
                    select(spent).where(LlmRequest.created_at >= _month_start(dt.datetime.now(dt.UTC)))
                )
            ).scalar_one()
        )
        run = decimal.Decimal(
            (await session.execute(select(spent).where(LlmRequest.run_id == run_id))).scalar_one()
        )
        over = None
        if month + reserve > caps.llm_monthly_cap_usd:
            over = (RefusalReason.SPEND_CAP_MONTH, month, caps.llm_monthly_cap_usd)
        elif run + reserve > caps.llm_run_cap_usd:
            over = (RefusalReason.SPEND_CAP_RUN, run, caps.llm_run_cap_usd)
        if over is not None:
            reason, so_far, cap = over
            # Says how much of the spend is reservations nobody has reconciled yet, so the
            # operator knows whether releasing them would make room.
            pending = await awaiting_reconciliation(session, ttl_s=caps.llm_reservation_ttl_s)
            return Refusal(
                reason,
                (
                    f"spent={so_far}",
                    f"request_up_to={reserve}",
                    f"cap={cap}",
                    f"awaiting_reconciliation={pending}",
                ),
                "spend",
            )
        row_id = uuid.uuid4()
        await session.execute(
            insert(LlmRequest).values(
                id=row_id,
                tenant_id=tenant_id,
                run_id=run_id,
                purpose=request.purpose.value,
                provider=provider,
                model=request.model,
                prompt_tokens=0,
                completion_tokens=0,
                # Counted at the worst case until settled; a crash leaves it counted.
                cost_usd=reserve,
                outcome="dispatching",
            )
        )
        await _audit(
            session,
            tenant_id,
            request,
            AuditAction.LLM_REQUEST_DISPATCHED,
            AuditOutcome.OK,
            _payload(request, provider) | {"reserved_usd": str(reserve)},
        )
    return row_id


async def _settle(
    engine: AsyncEngine,
    *,
    tenant_id: TenantId,
    request: CompletionRequest,
    provider: str,
    row_id: uuid.UUID,
    outcome: str,
    usage: Usage,
    cost: decimal.Decimal,
    error: Refusal | None = None,
) -> None:
    async with tenant_transaction(engine, tenant_id) as session:
        await session.execute(
            update(LlmRequest)
            .where(LlmRequest.id == row_id)
            .values(
                outcome=outcome,
                prompt_tokens=usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens,
                completion_tokens=usage.output_tokens,
                cost_usd=cost,
                refusal_reason=error.code[:200] if error else None,
            )
        )
        payload = _payload(request, provider) | {
            "outcome": outcome,
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "cost_usd": str(cost),
        }
        if error is not None:
            payload |= {"error": error.reason.value, "patterns": ",".join(error.details)}
        await _audit(
            session,
            tenant_id,
            request,
            AuditAction.LLM_REQUEST_SETTLED,
            AuditOutcome.OK if error is None else AuditOutcome.FAILED,
            payload,
            reason=error.reason.value if error else "",
        )


def _response_schema(request: CompletionRequest) -> tuple[dict[str, Any] | None, Refusal | None]:
    raw = request.inputs.get("response_schema")
    if raw is None:
        return None, None
    try:
        schema = json.loads(raw)
    except ValueError:
        schema = None
    if not isinstance(schema, dict):
        return None, Refusal(
            RefusalReason.INVALID_RESPONSE_SCHEMA, ("response_schema",), "inputs.response_schema"
        )
    return schema, None


def create_app(
    *,
    settings: Settings | None = None,
    provider: Provider | None = None,
    engine: AsyncEngine | None = None,
    provider_settings: ProviderSettings | None = None,
    prices: PriceTable | None = None,
) -> FastAPI:
    """Build the service. Dependencies are injected so tests need no real provider."""
    resolved_settings = settings or get_settings()
    caps = provider_settings or ProviderSettings()
    resolved_provider = provider or build_provider(caps)
    price_table = prices or load_prices()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The gateway holds provider keys; it must never also hold the migration
        # role's credentials (SECURITY.md §2.4).
        assert_no_migration_credentials()
        check_sdk_environment()
        own_engine = engine or create_engine(resolved_settings.database_dsn())
        app.state.engine = own_engine
        logger.info(
            "gateway_started",
            provider=resolved_provider.name,
            env=resolved_settings.env,
            prices=price_table.version,
            inference_geo=caps.llm_inference_geo,
        )
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

    async def refuse(
        app_engine: AsyncEngine, tenant_id: TenantId, body: CompletionRequest, refusal: Refusal, code: int
    ) -> HTTPException:
        await _record_refusal(
            app_engine, tenant_id=tenant_id, request=body, provider=resolved_provider.name, refusal=refusal
        )
        logger.warning(
            "llm_request_refused",
            purpose=body.purpose.value,
            reason=refusal.reason.value,
            patterns=list(refusal.details),
            location=refusal.location,
        )
        return HTTPException(
            status_code=code,
            detail={
                "error": "refused",
                "reason": refusal.reason.value,
                # Names only. The caller knows its own payload; the response does
                # not repeat the value back.
                "patterns": list(refusal.details),
                "location": refusal.location,
            },
        )

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
        if refusal is None and price_table.price(body.model) is None:
            refusal = Refusal(RefusalReason.MODEL_NOT_PRICED, (body.model,), "model")
        schema: dict[str, Any] | None = None
        if refusal is None:
            schema, refusal = _response_schema(body)
        if refusal is not None:
            raise await refuse(app_engine, tenant_id, body, refusal, status.HTTP_422_UNPROCESSABLE_CONTENT)

        blocks = assemble(body)
        geo = caps.llm_inference_geo
        reserve = decimal.Decimal(0)
        if resolved_provider.billed:
            try:
                input_tokens = await resolved_provider.count_input_tokens(
                    blocks, model=body.model, response_schema=schema
                )
            except Exception as error:
                # Fail closed: one token per character is more than any real text counts to.
                input_tokens = sum(len(block.text) for block in blocks) + len(
                    json.dumps(schema) if schema else ""
                )
                logger.warning(
                    "llm_token_count_failed", purpose=body.purpose.value, error=type(error).__name__
                )
            reserve = price_table.worst_case(
                body.model, geo, input_tokens=input_tokens, max_output_tokens=body.max_output_tokens
            )
        reserved = await _reserve(
            app_engine,
            tenant_id=tenant_id,
            request=body,
            provider=resolved_provider.name,
            reserve=reserve,
            caps=caps,
        )
        if isinstance(reserved, Refusal):
            raise await refuse(app_engine, tenant_id, body, reserved, status.HTTP_429_TOO_MANY_REQUESTS)

        def priced(usage: Usage) -> decimal.Decimal:
            return (
                price_table.cost(body.model, geo, usage) if resolved_provider.billed else decimal.Decimal(0)
            )

        settle = functools.partial(
            _settle,
            app_engine,
            tenant_id=tenant_id,
            request=body,
            provider=resolved_provider.name,
            row_id=reserved,
        )
        try:
            completion = await resolved_provider.complete(
                blocks,
                model=body.model,
                max_tokens=body.max_output_tokens,
                response_schema=schema,
                effort=EFFORT_BY_PURPOSE.get(body.purpose, DEFAULT_EFFORT),
            )
        except ModelDeclined as declined:
            await settle(
                outcome="declined",
                usage=declined.usage,
                cost=priced(declined.usage),
                error=Refusal(RefusalReason.MODEL_DECLINED, (declined.category or "none",), "provider"),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="the model declined"
            ) from declined
        except ProviderRejected as rejected:
            # The provider answered with an error: failed requests are not billed.
            await settle(
                outcome="failed",
                usage=Usage(),
                cost=decimal.Decimal(0),
                error=Refusal(
                    RefusalReason.PROVIDER_ERROR, (rejected.kind, str(rejected.status_code)), "provider"
                ),
            )
            logger.warning("llm_provider_failed", purpose=body.purpose.value, error=rejected.kind)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="the model provider failed"
            ) from rejected
        except Exception as error:
            # Recorded and reported as a gateway failure; the provider's own error text
            # never reaches the caller, since it can quote the payload back. A timeout or a
            # dropped connection may still have been billed, so the row keeps its
            # reservation as "unsettled" until it is reconciled.
            await settle(
                outcome="unsettled",
                usage=Usage(),
                cost=reserve,
                error=Refusal(RefusalReason.PROVIDER_ERROR, (type(error).__name__,), "provider"),
            )
            logger.warning("llm_provider_failed", purpose=body.purpose.value, error=type(error).__name__)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="the model provider failed"
            ) from error

        cost = priced(completion.usage)
        await settle(outcome="ok", usage=completion.usage, cost=cost)
        return CompletionResponse(
            text=completion.text,
            model=body.model,
            provider=resolved_provider.name,
            prompt_tokens=completion.usage.input_tokens,
            completion_tokens=completion.usage.output_tokens,
            cost_usd=str(cost),
            stop_reason=completion.stop_reason,
        )

    return app
