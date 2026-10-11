"""``python -m aria_api.seed`` — create the tenant, user and candidate to work as.

The API's token resolves to a person, which is what lets the audit log say who
approved a fact rather than "anonymous". Something has to create that person, and
doing it by hand means three inserts with the right foreign keys in the right scope.

**Configuration is the source of truth, not the email address.** Once ``.env`` names
an identity this verifies it and prints it back; it creates nothing. That matters more
than it sounds: a command that quietly made a *second* candidate would leave the graph
split across two of them with no error anywhere, and the next import would go to
whichever one the environment happened to name.

An earlier version claimed to be idempotent by looking the user up by email. It was
not, for two reasons that only showed up when it was run twice:

* ``users`` is unique on ``(tenant_id, email)``, not on ``email`` — the same address in
  a new tenant is perfectly legal;
* a platform-scoped transaction cannot read tenant rows at all, by design, so the
  lookup matched nothing every time.

Finding the user by email would therefore mean searching every tenant for an address,
which is the one thing row-level security exists to prevent. Reading the ids from
configuration avoids the question entirely.

It writes nothing to ``.env`` itself. Rewriting the file somebody is reading is how a
half-typed line gets lost, so it prints the three settings and lets them paste.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import insert, select

from aria_core.config import Settings, get_settings
from aria_core.db.models import Candidate, Tenant, User
from aria_core.db.session import create_engine, platform_transaction, tenant_transaction
from aria_core.schemas.identity import TenantId, new_id

__all__ = ["Identity", "StaleIdentity", "main", "seed"]


class StaleIdentity(RuntimeError):
    """``.env`` names an identity that is not in the database."""


@dataclass(frozen=True, slots=True)
class Identity:
    """Who the API acts as."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    candidate_id: uuid.UUID
    #: False when this already existed and was only verified.
    created: bool


def _configured(settings: Settings) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None:
    """The three ids from configuration, or ``None`` if they are not all set."""
    values = (settings.api_tenant_id, settings.api_user_id, settings.api_candidate_id)
    if not all(values):
        return None
    try:
        tenant_id, user_id, candidate_id = (uuid.UUID(value) for value in values)
    except ValueError as error:
        raise StaleIdentity(
            "ARIA_API_TENANT_ID, ARIA_API_USER_ID and ARIA_API_CANDIDATE_ID must be UUIDs"
        ) from error
    return tenant_id, user_id, candidate_id


async def seed(settings: Settings, *, email: str, display_name: str) -> Identity:
    """Verify the configured identity, or create a fresh one.

    Creating is the branch taken only when ``.env`` names nothing. Everything else is
    a check, so running this twice cannot produce two candidates.
    """
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        existing = _configured(settings)
        if existing is not None:
            tenant_id, user_id, candidate_id = existing
            async with tenant_transaction(engine, TenantId(tenant_id)) as session:
                found = (
                    await session.execute(
                        select(Candidate.id, Candidate.user_id).where(Candidate.id == candidate_id)
                    )
                ).first()
            if found is None:
                raise StaleIdentity(
                    f"ARIA_API_CANDIDATE_ID is {candidate_id}, which is not in tenant "
                    f"{tenant_id}. Either the database was reset or the ids were edited; "
                    "clear the three ARIA_API_*_ID lines in .env to create a new identity."
                )
            if found[1] != user_id:
                raise StaleIdentity(
                    f"candidate {candidate_id} belongs to user {found[1]}, but "
                    f"ARIA_API_USER_ID is {user_id}. Fix .env rather than seeding again: "
                    "an action audited against the wrong user is worse than no action."
                )
            return Identity(tenant_id, user_id, candidate_id, created=False)

        tenant_id = new_id()
        user_id, candidate_id = new_id(), new_id()
        async with platform_transaction(engine) as session:
            await session.execute(insert(Tenant).values(id=tenant_id, name=display_name))
        async with tenant_transaction(engine, TenantId(tenant_id)) as session:
            await session.execute(
                insert(User).values(id=user_id, tenant_id=tenant_id, email=email, display_name=display_name)
            )
            await session.execute(
                insert(Candidate).values(id=candidate_id, tenant_id=tenant_id, user_id=user_id)
            )
        return Identity(tenant_id, user_id, candidate_id, created=True)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    email = arguments[0] if arguments else "me@localhost"
    display_name = arguments[1] if len(arguments) > 1 else "ARIA owner"

    settings = get_settings()
    try:
        identity = asyncio.run(seed(settings, email=email, display_name=display_name))
    except StaleIdentity as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if identity.created:
        print(f"created a new identity for {email}\n")
    else:
        print("this identity already exists and .env already names it; nothing was created\n")

    print(f"tenant    {identity.tenant_id}")
    print(f"user      {identity.user_id}")
    print(f"candidate {identity.candidate_id}")

    if identity.created:
        print("\nPut these three in .env:\n")
        print(f"ARIA_API_TENANT_ID={identity.tenant_id}")
        print(f"ARIA_API_USER_ID={identity.user_id}")
        print(f"ARIA_API_CANDIDATE_ID={identity.candidate_id}")
        if settings.api_service_token is None:
            print(
                '\nAnd a token:\n  python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
                "  ARIA_API_SERVICE_TOKEN=..."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
