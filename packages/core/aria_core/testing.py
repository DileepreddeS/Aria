"""Shared pytest fixtures, as a plugin.

Every workspace member's tests need a tenant, a user and a scoped engine. Defining
them once here and loading the module as a plugin from the root ``conftest.py`` means
there is one definition rather than one per package — four copies of "create a tenant"
would drift, and the one that drifted would be the one asserting isolation.

It imports pytest, which is a development dependency. Nothing in the product imports
this module, so a production install that lacks pytest never notices.

Tests that touch the database are marked ``db``, so ``pytest -m "not db"`` runs the
pure ones with no infrastructure. When Postgres is unreachable they skip with a
message saying what to start, rather than failing with a connection trace.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.config import MigrationSettings, Settings
from aria_core.db.models import Tenant, User
from aria_core.db.session import create_engine, platform_transaction, tenant_transaction
from aria_core.schemas.identity import TenantId, UserId, new_id

__all__ = ["engine", "migrate_engine", "settings", "tenant_a", "tenant_b", "user_a"]


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """An engine for the application role, or a skip if the database is not up."""
    engine = create_engine(settings.database_dsn())
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"local Postgres is not reachable ({type(error).__name__}); run ./tasks.ps1 up")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migrate_engine() -> AsyncIterator[AsyncEngine]:
    """An engine for the migration role, which owns the tables.

    Only the tamper-evidence tests use it: they need to alter the audit log the way
    database-level access could, which aria_app cannot do by design. aria_migrate is
    not a security boundary — it can drop the tables — so giving it a maintenance
    policy costs nothing and keeps backfills possible.
    """
    engine = create_engine(MigrationSettings().migrate_dsn())
    try:
        yield engine
    finally:
        await engine.dispose()


async def _create_tenant(engine: AsyncEngine, label: str) -> TenantId:
    tenant_id = TenantId(new_id())
    async with platform_transaction(engine) as session:
        await session.execute(insert(Tenant).values(id=tenant_id, name=f"test-{label}-{tenant_id}"))
    return tenant_id


async def _drop_tenant(engine: AsyncEngine, tenant_id: TenantId) -> None:
    async with platform_transaction(engine) as session:
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest_asyncio.fixture
async def tenant_a(engine: AsyncEngine) -> AsyncIterator[TenantId]:
    """A fresh tenant per test, so tests never see each other's rows."""
    tenant_id = await _create_tenant(engine, "a")
    try:
        yield tenant_id
    finally:
        await _drop_tenant(engine, tenant_id)


@pytest_asyncio.fixture
async def tenant_b(engine: AsyncEngine) -> AsyncIterator[TenantId]:
    tenant_id = await _create_tenant(engine, "b")
    try:
        yield tenant_id
    finally:
        await _drop_tenant(engine, tenant_id)


@pytest_asyncio.fixture
async def user_a(engine: AsyncEngine, tenant_a: TenantId) -> UserId:
    """A user inside tenant A, so tables under test are never empty."""
    user_id = UserId(new_id())
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_a,
                email=f"user-{uuid.uuid4().hex[:8]}@example.test",
                display_name="Test Person",
            )
        )
    return user_id
