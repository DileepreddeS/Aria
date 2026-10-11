"""Seeding an identity must never produce a second candidate.

The bug this file exists to prevent was real and quiet: running ``seed-me`` twice with
the same email created two tenants, two users and two candidates. The graph then splits
across them with no error anywhere — one import goes to the candidate ``.env`` names
and the next goes wherever the ids were last pasted from.

Two reasons it happened, both only visible on the second run: ``users`` is unique on
``(tenant_id, email)`` rather than on ``email``, so the same address in a new tenant is
legal; and a platform-scoped transaction cannot read tenant rows at all, by design, so
the lookup matched nothing every time.

The fix is to treat configuration as the source of truth. Searching every tenant for an
email address would be the one thing row-level security exists to prevent.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_api.seed import Identity, StaleIdentity, seed
from aria_core.config import Settings
from aria_core.db.models import Candidate, Tenant, User
from aria_core.db.session import platform_transaction, tenant_transaction
from aria_core.schemas.identity import TenantId, UserId, new_id

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def seeded(engine: AsyncEngine) -> AsyncIterator[Callable[..., Awaitable[Identity]]]:
    """Call seed() and drop whatever tenants it created afterwards.

    Without this, every run of this file left a handful of tenants in the local
    database -- which is exactly the clutter that made "is my graph empty?" a
    question worth asking in the first place.
    """
    created: list[uuid.UUID] = []

    async def run(settings: Settings, **kwargs: str) -> Identity:
        identity = await seed(settings, **kwargs)
        if identity.created:
            created.append(identity.tenant_id)
        return identity

    try:
        yield run
    finally:
        async with platform_transaction(engine) as session:
            for tenant_id in created:
                await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


def configured(settings: Settings, identity: Identity) -> Settings:
    return settings.model_copy(
        update={
            "api_tenant_id": str(identity.tenant_id),
            "api_user_id": str(identity.user_id),
            "api_candidate_id": str(identity.candidate_id),
        }
    )


def unconfigured(settings: Settings) -> Settings:
    return settings.model_copy(update={"api_tenant_id": "", "api_user_id": "", "api_candidate_id": ""})


async def count(engine: AsyncEngine, tenant_id: uuid.UUID) -> int:
    async with tenant_transaction(engine, TenantId(tenant_id)) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Candidate).where(Candidate.tenant_id == tenant_id)
                )
            ).scalar_one()
        )


class TestSeedingTwice:
    async def test_an_unconfigured_seed_creates_an_identity(
        self, settings: Settings, engine: AsyncEngine, seeded: Callable[..., Awaitable[Identity]]
    ) -> None:
        identity = await seeded(unconfigured(settings), email="first@example.invalid", display_name="First")
        assert identity.created is True
        assert await count(engine, identity.tenant_id) == 1

    async def test_a_configured_seed_creates_nothing(
        self, settings: Settings, engine: AsyncEngine, seeded: Callable[..., Awaitable[Identity]]
    ) -> None:
        # The behaviour the bug violated: run it again and you get the same ids back.
        first = await seeded(unconfigured(settings), email="again@example.invalid", display_name="Again")
        second = await seeded(
            configured(settings, first), email="again@example.invalid", display_name="Again"
        )

        assert second.created is False
        assert (second.tenant_id, second.user_id, second.candidate_id) == (
            first.tenant_id,
            first.user_id,
            first.candidate_id,
        )
        assert await count(engine, first.tenant_id) == 1

    async def test_the_same_email_twice_without_configuration_is_two_identities(
        self, settings: Settings, seeded: Callable[..., Awaitable[Identity]]
    ) -> None:
        # Stated rather than prevented: the email is not the key, so two unconfigured
        # runs do make two identities. That is why the command reports which it did,
        # and why `.env` is the thing that stops it happening by accident.
        first = await seeded(unconfigured(settings), email="same@example.invalid", display_name="Same")
        second = await seeded(unconfigured(settings), email="same@example.invalid", display_name="Same")
        assert first.candidate_id != second.candidate_id
        assert first.created and second.created


class TestItRefusesAStaleIdentity:
    async def test_a_candidate_that_does_not_exist_is_an_error(
        self, settings: Settings, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        # The database was reset, or the ids were edited by hand. Creating a fresh
        # identity here would silently abandon whatever is already in the graph.
        stale = settings.model_copy(
            update={
                "api_tenant_id": str(tenant_a),
                "api_user_id": str(new_id()),
                "api_candidate_id": str(new_id()),
            }
        )
        with pytest.raises(StaleIdentity, match="not in tenant"):
            await seed(stale, email="stale@example.invalid", display_name="Stale")

    async def test_a_candidate_belonging_to_another_user_is_an_error(
        self, settings: Settings, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        # An action audited against the wrong user is worse than no action.
        candidate_id = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Candidate).values(id=candidate_id, tenant_id=tenant_a, user_id=user_a)
            )
        mismatched = settings.model_copy(
            update={
                "api_tenant_id": str(tenant_a),
                "api_user_id": str(new_id()),
                "api_candidate_id": str(candidate_id),
            }
        )
        with pytest.raises(StaleIdentity, match="belongs to user"):
            await seed(mismatched, email="x@example.invalid", display_name="X")

    async def test_ids_that_are_not_uuids_are_an_error(self, settings: Settings) -> None:
        broken = settings.model_copy(
            update={
                "api_tenant_id": "not-a-uuid",
                "api_user_id": "also-not",
                "api_candidate_id": "nope",
            }
        )
        with pytest.raises(StaleIdentity, match="must be UUIDs"):
            await seed(broken, email="x@example.invalid", display_name="X")

    async def test_a_half_configured_env_creates_a_fresh_identity(
        self, settings: Settings, seeded: Callable[..., Awaitable[Identity]]
    ) -> None:
        # Two of three set is not an identity. Treating it as one would mean reading a
        # candidate id out of a tenant that was never named.
        half = settings.model_copy(
            update={"api_tenant_id": str(new_id()), "api_user_id": "", "api_candidate_id": ""}
        )
        identity = await seeded(half, email="half@example.invalid", display_name="Half")
        assert identity.created is True


class TestWhatItWrites:
    async def test_the_tenant_user_and_candidate_are_linked(
        self, settings: Settings, engine: AsyncEngine, seeded: Callable[..., Awaitable[Identity]]
    ) -> None:
        identity = await seeded(unconfigured(settings), email="linked@example.invalid", display_name="Linked")
        async with tenant_transaction(engine, TenantId(identity.tenant_id)) as session:
            row = (
                await session.execute(
                    select(Candidate.user_id, Candidate.tenant_id).where(
                        Candidate.id == identity.candidate_id
                    )
                )
            ).first()
            email = (
                await session.execute(select(User.email).where(User.id == identity.user_id))
            ).scalar_one()
            name = (
                await session.execute(select(Tenant.name).where(Tenant.id == identity.tenant_id))
            ).scalar_one()
        assert row == (identity.user_id, identity.tenant_id)
        assert email == "linked@example.invalid"
        assert name == "Linked"

    async def test_a_new_candidate_has_an_empty_graph(
        self, settings: Settings, engine: AsyncEngine, seeded: Callable[..., Awaitable[Identity]]
    ) -> None:
        # What the operator needs to be able to trust before importing their resume:
        # a freshly seeded candidate carries nothing from anybody else.
        from aria_core.graph.facts import approved_facts, draft_facts
        from aria_core.graph.questions import open_questions

        identity = await seeded(unconfigured(settings), email="empty@example.invalid", display_name="Empty")
        async with tenant_transaction(engine, TenantId(identity.tenant_id)) as session:
            assert await draft_facts(session, identity.candidate_id) == []
            assert await approved_facts(session, identity.candidate_id) == []
            assert await open_questions(session, identity.candidate_id) == []
