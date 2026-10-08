"""Alembic environment.

The database URL is never written in alembic.ini. It comes from
``ARIA_MIGRATE_DATABASE_URL`` through :class:`aria_core.config.MigrationSettings`,
which reads ``.env.migrate`` — a file the API and the gateway never load, and whose
presence in their environment makes them refuse to start. aria_migrate owns the
tables and is not subject to tenant isolation, so its credentials are kept out of
reach of anything that serves requests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from aria_core.config import MigrationSettings
from aria_core.db.models import Base

config = context.config
target_metadata = Base.metadata


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """Keep autogenerate out of objects Alembic cannot see properly.

    Row-level security policies, grants and functions are not ORM objects; they are
    written by hand in the migrations and must not be dropped because autogenerate
    did not find them in the metadata.
    """
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=MigrationSettings().migrate_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section: dict[str, Any] = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = MigrationSettings().migrate_dsn()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()


__all__: Iterable[str] = ()
