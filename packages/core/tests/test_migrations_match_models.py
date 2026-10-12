"""The migrations and the ORM models describe the same schema.

Each is written by hand, and either can drift: a column added to a model with no
migration exists in tests built from the models and nowhere in a real database, and a
CHECK added to a migration but not to the model is invisible to anyone reading the
model. Alembic's autogenerate comparison is the same check a person would run before
writing the next migration; here it runs on every test run against the migrated
database, and any difference fails.
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Base

pytestmark = pytest.mark.db


def _differences(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


async def test_the_migrated_database_matches_the_models(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        differences = await connection.run_sync(_differences)
    assert differences == [], "migrations and models disagree:\n" + "\n".join(map(repr, differences))
