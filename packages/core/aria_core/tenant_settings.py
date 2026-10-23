"""Whether a tenant's candidate data may be sent to a model (DECISIONS 2026-10-23).

Step 9's writer, and later the answer engine and the 6-second check, send a candidate's own
facts or page to the model provider. That is allowed only for a tenant that has opted in:
off for every new tenant (migration 0009), turned on or off only with a reason and the name
of whoever did it, and audited. The gateway reads it before every such request.

    uv run python -m aria_core.tenant_settings model-writing --tenant <id> --on \\
        --by dileep --reason "own data; 30-day retention accepted (DECISIONS 2026-10-23)"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from aria_core.audit.log import append_event
from aria_core.config import get_settings
from aria_core.db.models import Tenant
from aria_core.db.session import create_engine, tenant_transaction
from aria_core.schemas.audit import ActorKind, AuditAction, AuditEventDraft, AuditOutcome, SubjectKind
from aria_core.schemas.identity import TenantId

__all__ = ["main", "model_writing_enabled", "set_model_writing"]


async def model_writing_enabled(session: AsyncSession, tenant_id: TenantId) -> bool:
    """False for a tenant that has not opted in, and for one the session cannot see."""
    found = await session.execute(select(Tenant.model_writing_opt_in).where(Tenant.id == tenant_id))
    return bool(found.scalar_one_or_none())


async def set_model_writing(
    engine: AsyncEngine, tenant_id: TenantId, *, enabled: bool, by: str, reason: str
) -> None:
    if not by.strip() or not reason.strip():
        raise ValueError("changing whether candidate data may reach a model needs a name and a reason")
    async with tenant_transaction(engine, tenant_id) as session:
        before = await model_writing_enabled(session, tenant_id)
        result = await session.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(model_writing_opt_in=enabled)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise ValueError(f"no tenant {tenant_id}")
        await append_event(
            session,
            AuditEventDraft(
                action=AuditAction.MODEL_WRITING_CHANGED,
                actor_kind=ActorKind.USER,
                actor_id=by.strip()[:80],
                subject_kind=SubjectKind.TENANT,
                subject_id=str(tenant_id),
                outcome=AuditOutcome.OK,
                reason=reason.strip()[:200],
                payload={"before": before, "after": enabled},
            ),
            tenant_id=tenant_id,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Whether a tenant's candidate data may be sent to a model.")
    commands = parser.add_subparsers(dest="command", required=True)
    switch = commands.add_parser("model-writing")
    switch.add_argument("--tenant", required=True, type=uuid.UUID)
    state = switch.add_mutually_exclusive_group(required=True)
    state.add_argument("--on", action="store_true")
    state.add_argument("--off", action="store_true")
    switch.add_argument("--by", required=True)
    switch.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    async def run() -> int:
        engine = create_engine(get_settings().database_dsn())
        try:
            await set_model_writing(
                engine, TenantId(args.tenant), enabled=args.on, by=args.by, reason=args.reason
            )
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        finally:
            await engine.dispose()
        print(f"model writing {'on' if args.on else 'off'} for tenant {args.tenant}")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
