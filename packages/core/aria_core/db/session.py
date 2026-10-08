"""Database access, always inside a scope (SECURITY.md §2.4, §12).

There is no public session factory. Every transaction is opened through
:func:`tenant_transaction` or :func:`platform_transaction`, each of which sets the
session variables the row-level security policies read. Code cannot reach the
database without saying whose data it is about.

Belt and braces, deliberately:

* the database refuses a query whose scope is unset — ``aria_current_tenant()``
  raises inside the policy (see the migration), so a forgotten scope is an error
  rather than an empty result set;
* ``aria_app`` has ``NOBYPASSRLS``, owns nothing and cannot alter the tables;
* the policies are the enforcement, and this module is the convenience.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from aria_core.schemas.identity import TenantId

__all__ = ["DatabaseScope", "create_engine", "platform_transaction", "tenant_transaction"]

#: Session variables the RLS policies read. Set with ``SET LOCAL`` semantics
#: (``set_config(..., is_local => true)``) so they are scoped to the transaction
#: and cannot leak to the next user of a pooled connection.
SCOPE_SETTING: Final = "aria.scope"
TENANT_SETTING: Final = "aria.tenant_id"


class DatabaseScope(StrEnum):
    TENANT = "tenant"
    PLATFORM = "platform"


def create_engine(dsn: str, *, echo: bool = False) -> AsyncEngine:
    """An engine for the application role.

    ``pool_pre_ping`` because a laptop sleeping mid-run is the normal case here.
    """
    return create_async_engine(dsn, echo=echo, pool_pre_ping=True, pool_size=5, max_overflow=5)


def _sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def _apply_scope(session: AsyncSession, scope: DatabaseScope, tenant_id: uuid.UUID | None) -> None:
    # set_config with is_local => true is SET LOCAL, but parameterizable; SET LOCAL
    # itself takes no bind parameters, and interpolating a tenant id into DDL-ish
    # SQL is exactly the habit we do not want anywhere in this codebase.
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": SCOPE_SETTING, "value": scope.value},
    )
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": TENANT_SETTING, "value": str(tenant_id) if tenant_id else ""},
    )


@asynccontextmanager
async def tenant_transaction(engine: AsyncEngine, tenant_id: TenantId) -> AsyncIterator[AsyncSession]:
    """One transaction that can only see and write one tenant's rows.

    Commits on success, rolls back on any exception.
    """
    async with _sessionmaker(engine)() as session, session.begin():
        await _apply_scope(session, DatabaseScope.TENANT, tenant_id)
        yield session


@asynccontextmanager
async def platform_transaction(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One transaction for rows that belong to no tenant.

    Used for the platform audit chain and for creating a tenant in the first
    place. It cannot read tenant rows: the tenant policies compare against an
    unset tenant id, which matches nothing.
    """
    async with _sessionmaker(engine)() as session, session.begin():
        await _apply_scope(session, DatabaseScope.PLATFORM, None)
        yield session


@asynccontextmanager
async def unscoped_transaction_for_tests(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A transaction with no scope set. Only for the test that proves it fails.

    Named so that it is obvious in a diff and greppable in review. Nothing in the
    product may call it.
    """
    async with _sessionmaker(engine)() as session, session.begin():
        yield session
