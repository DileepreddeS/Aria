"""Fixtures for the tests that need the local Postgres.

Tests that touch the database are marked ``db`` so ``pytest -m "not db"`` runs the
pure ones with no infrastructure. When Postgres is not reachable the marked tests
skip rather than fail, so a developer without Docker running is told what is
missing instead of reading a connection trace.

Each test works in its own freshly created tenant, identified by a random UUID, so
tests never see each other's rows even though they share one database. Audit rows
are deliberately not cleaned up: the table is append-only by design, and inventing
a way around that for tests would undo the thing being tested.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.config import Settings
from aria_core.db.models import Tenant, User
from aria_core.db.session import create_engine, platform_transaction, tenant_transaction
from aria_core.schemas.identity import TenantId, UserId, new_id


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
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"local Postgres is not reachable ({exc.__class__.__name__}); run ./tasks.ps1 up")
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
async def migrate_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """An engine for the migration role, which owns the tables.

    Only the tamper-evidence tests use it: they need to alter the audit log the way
    database-level access could, which aria_app cannot do by design. aria_migrate
    is not a security boundary — it can drop the tables — so giving it a
    maintenance policy costs nothing and keeps backfills possible.
    """
    engine = create_engine(settings.migrate_dsn())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_a(engine: AsyncEngine) -> AsyncIterator[TenantId]:
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
