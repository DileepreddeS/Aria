"""What a request needs: a database transaction, a store, a scanner, a parser.

Everything is built once at start-up and held on ``app.state``, so a test can build
an app with its own engine or its own scanner and the routes do not know the
difference. The transaction is per request and tenant-scoped, which is what makes
row-level security do its job: a route cannot accidentally read another tenant's rows,
because the scope is set before its code runs and the policies raise when it is not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Protocol

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from aria_api.auth import Caller, require_caller
from aria_api.uploads.quarantine import QuarantineStore
from aria_api.uploads.scanner import MalwareScanner
from aria_core.config import Settings
from aria_core.db.session import tenant_transaction
from aria_core.taxonomy.model import SkillTaxonomy
from aria_document_parser.client import ParsedDocument

__all__ = [
    "CallerDep",
    "ParserDep",
    "ScannerDep",
    "SessionDep",
    "SettingsDep",
    "StoreDep",
    "TaxonomyDep",
    "session_for",
]


class DocumentParser(Protocol):
    """Structural type for the parser, mirrored from the upload pipeline.

    A Protocol rather than a class: the API is handed whatever parser the app was
    built with, and a test supplies an in-process one.
    """

    async def parse(self, content: bytes, detected_type: str) -> ParsedDocument: ...


async def session_for(
    request: Request,
    caller: Annotated[Caller, Depends(require_caller)],
) -> AsyncIterator[AsyncSession]:
    """One tenant-scoped transaction per request, committed on the way out.

    The scope comes from the authenticated caller, never from the request body. A
    tenant id a client could choose would make row-level security a formality.
    """
    engine: AsyncEngine = request.app.state.engine
    async with tenant_transaction(engine, caller.tenant_id) as session:
        yield session


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _store(request: Request) -> QuarantineStore:
    return request.app.state.quarantine  # type: ignore[no-any-return]


def _scanner(request: Request) -> MalwareScanner:
    return request.app.state.scanner  # type: ignore[no-any-return]


def _parser(request: Request) -> DocumentParser:
    return request.app.state.parser  # type: ignore[no-any-return]


def _taxonomy(request: Request) -> SkillTaxonomy:
    return request.app.state.taxonomy  # type: ignore[no-any-return]


CallerDep = Annotated[Caller, Depends(require_caller)]
SessionDep = Annotated[AsyncSession, Depends(session_for)]
SettingsDep = Annotated[Settings, Depends(_settings)]
StoreDep = Annotated[QuarantineStore, Depends(_store)]
ScannerDep = Annotated[MalwareScanner, Depends(_scanner)]
ParserDep = Annotated[DocumentParser, Depends(_parser)]
TaxonomyDep = Annotated[SkillTaxonomy, Depends(_taxonomy)]
