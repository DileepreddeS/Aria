"""Structured logs and traces (SECURITY.md §3, PRODUCT_SPEC §5 observability).

Logs are JSON, and every value in every line goes through a redaction processor
before it is rendered. The rule from SECURITY.md §3 — "logs never contain S2/S3
data; S1 is redacted to IDs" — is enforced here rather than left to whoever writes
the log call, because the log call that leaks is always the one nobody reviewed.

What the processor does with each value:

* an ARIA message becomes its ``safe_summary()``: S0 kept, S1 reduced to a shape or
  an id, S2/S3 dropped;
* a ``SecretStr`` renders as its own mask, never resolved;
* an ``UntrustedContent`` block renders as its marker, never its text;
* a plain string is scanned for sensitive patterns and redacted in place, which
  catches the S2 value that arrived inside a message someone formatted by hand;
* bytes are reduced to a length, since ciphertext and keys are bytes.

Traces use the OpenTelemetry SDK with a console exporter in development. No
observability vendor has been chosen, so nothing is exported anywhere yet; the span
ids are in the log lines so a request can still be followed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TextIO

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from pydantic import SecretStr

from aria_core.config import Settings
from aria_core.redaction import redact_sensitive
from aria_core.schemas.base import AriaMessage
from aria_core.schemas.untrusted import UntrustedContent

__all__ = ["configure_logging", "configure_tracing", "redact_event"]

#: Keys whose value is replaced outright, whatever it is. Belt and braces for the
#: names that recur across the codebase and must never be rendered.
_ALWAYS_MASK = frozenset(
    {
        "password",
        "token",
        "secret",
        "api_key",
        "cookie",
        "session",
        "authorization",
        "service_token",
        "data_key",
        # Substring match, so this also covers wrapped keys.
        "dek",
        "private_key",
    }
)
MASK = "**********"


def _redact_value(key: str, value: Any) -> Any:
    if any(marker in key.lower() for marker in _ALWAYS_MASK):
        return MASK
    if isinstance(value, SecretStr):
        return MASK
    if isinstance(value, UntrustedContent):
        return value.marker
    if isinstance(value, AriaMessage):
        return value.safe_summary()
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        cleaned, _ = redact_sensitive(value)
        return cleaned
    if isinstance(value, dict):
        return {inner_key: _redact_value(str(inner_key), inner) for inner_key, inner in value.items()}
    if isinstance(value, list | tuple | set):
        return type(value)(_redact_value(key, item) for item in value)
    return value


def redact_event(
    _logger: object, _name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """structlog processor: nothing reaches the renderer unredacted."""
    return {key: _redact_value(str(key), value) for key, value in event_dict.items()}


def _add_trace_context(
    _logger: object, _name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Put the current span's ids in the line, so logs and traces line up."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """Install the processor chain. Call once, at start-up.

    ``stream`` redirects the rendered lines; the default is stdout, which is what a
    container collects. Tests pass a buffer so they can assert on the real output of
    the real chain rather than on a stand-in.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_trace_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Last before rendering: everything above may add values, and all of
            # them are redacted, including an exception's formatted traceback.
            redact_event,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}[settings.log_level]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        # Caching binds the first logger for the process's lifetime, which is right
        # in production and wrong for a test that reconfigures.
        cache_logger_on_first_use=stream is None,
    )


def configure_tracing(settings: Settings, *, service_name: str = "aria") -> None:
    """Install a tracer provider. Console exporter in development, or none.

    No vendor is chosen yet (PRODUCT_SPEC §5), so this deliberately does not export
    off the machine. Spans still carry ids into the logs.
    """
    if settings.otel_exporter == "none":
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name, "deployment.environment": settings.env})
    )
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
