"""The API: a thin HTTP surface over the graph, for one person on localhost.

Everything it can do, it does through the same functions the rest of the codebase
uses — :mod:`aria_core.graph.facts` for facts, :mod:`aria_core.graph.questions` for
questions, :mod:`aria_core.importer` for imports. There is no second path to the
graph, which is why the CLI is an HTTP client rather than a database client: a CLI
that talked to Postgres directly would bypass every check this layer applies, and
would drift from whatever the Phase 6 dashboard does.

Start-up refuses rather than warns. ``run_preflight_checks`` already rejects a dev
scanner outside dev; this adds the same treatment for an unset token, because an API
that holds S1 data and can approve facts must not serve anonymous callers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from aria_api.routers import drafts, entities, graph, interview, questions, report, uploads
from aria_api.startup import run_preflight_checks
from aria_api.uploads.quarantine import QuarantineStore
from aria_api.uploads.scanner import MalwareScanner, assert_scanner_is_allowed, build_scanner
from aria_core.config import Settings, get_settings
from aria_core.db.session import create_engine
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_document_parser.client import DockerDocumentParser, SubprocessDocumentParser

__all__ = ["create_app"]

logger = structlog.get_logger(__name__)


def _scanner_for(settings: Settings) -> MalwareScanner:
    """The configured scanner, checked against the environment.

    ``build_scanner`` already picks ClamAV when a host is set and the dev scanner
    otherwise. The check is the part that matters here: the dev scanner records
    ``not_scanned`` and has no code path to ``clean``, so serving with it outside dev
    would mean accepting files nothing had looked at.
    """
    scanner = build_scanner(settings)
    assert_scanner_is_allowed(scanner, settings)
    return scanner


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Takes settings so a test can supply its own token and engine."""
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        run_preflight_checks(resolved)
        if resolved.api_service_token is None:
            # Not a warning. The alternative is an unauthenticated service that can
            # put claims on a resume.
            raise RuntimeError(
                "api_service_token is not set; the API will not serve unauthenticated "
                "requests. Set it in .env (see .env.example)."
            )

        app.state.settings = resolved
        app.state.engine = create_engine(resolved.database_url.get_secret_value())
        app.state.quarantine = QuarantineStore(
            resolved.upload_quarantine_path, max_bytes=resolved.max_upload_bytes
        )
        app.state.scanner = _scanner_for(resolved)
        app.state.parser = (
            DockerDocumentParser(resolved.parser_image)
            if resolved.parser_isolation == "docker"
            else SubprocessDocumentParser()
        )
        app.state.taxonomy = load_default_taxonomy()
        logger.info(
            "api_started",
            bind=f"{resolved.api_bind_host}:{resolved.api_bind_port}",
            env=resolved.env,
            scanner=app.state.scanner.name,
            parser_isolation=resolved.parser_isolation,
            taxonomy=app.state.taxonomy.version,
        )
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(
        title="ARIA",
        version="0.1.0",
        summary="Candidate graph, approval flow and question queue.",
        lifespan=lifespan,
        # No docs in production: the schema describes every endpoint that can read
        # this person's data, and nothing needs it on a machine serving one user.
        docs_url="/docs" if resolved.env in {"dev", "ci"} else None,
        redoc_url=None,
    )
    app.state.settings = resolved

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Unauthenticated on purpose: it says nothing about anybody's data."""
        return {"status": "ok", "env": resolved.env}

    app.include_router(uploads.router)
    app.include_router(drafts.router)
    app.include_router(entities.router)
    app.include_router(questions.router)
    app.include_router(interview.router)
    app.include_router(report.router)
    app.include_router(graph.router)
    return app
