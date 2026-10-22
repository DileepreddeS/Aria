"""Reconciling reservations the gateway could not settle (DECISIONS 2026-10-20).

The spend caps fail closed: a request that timed out or lost its connection keeps its
worst-case reservation (``unsettled``), and one whose process died stays ``dispatching``,
because either may have been billed. Left alone, those rows would eat a cap with money
that was perhaps never spent. This module is how they are settled on evidence:

    uv run python -m aria_llm_gateway.reservations list --tenant <id>
    uv run python -m aria_llm_gateway.reservations release --tenant <id> --id <row> \\
        --cost 0.0123 --reason "Console usage for 2026-10-20 shows ..." --by dileep

``list`` shows each reconcilable row: every ``unsettled`` one, and every ``dispatching`` one
older than ``ARIA_LLM_RESERVATION_TTL_S`` (younger ones may still be in flight). ``release``
settles one row at the cost the operator read from the provider's Console, never more than
its reservation, with the reason and the person recorded in the audit log. Nothing is
released automatically: what a timed-out request cost is known only to the provider.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import decimal
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aria_core.audit.log import append_event
from aria_core.config import get_settings
from aria_core.db.models import LlmRequest
from aria_core.db.session import create_engine, tenant_transaction
from aria_core.schemas.audit import ActorKind, AuditAction, AuditEventDraft, AuditOutcome, SubjectKind
from aria_core.schemas.identity import TenantId

__all__ = [
    "NotReconcilable",
    "Reservation",
    "awaiting_reconciliation",
    "list_reservations",
    "main",
    "release",
]

#: Longest reason kept; the audit log is not a place for an essay.
MAX_REASON = 200


class NotReconcilable(Exception):
    """The row is settled, still in flight, another tenant's, or the cost is out of bounds."""


@dataclass(frozen=True, slots=True)
class Reservation:
    id: uuid.UUID
    run_id: uuid.UUID | None
    purpose: str
    model: str
    outcome: str
    reserved_usd: decimal.Decimal
    created_at: dt.datetime


def _reconcilable(ttl_s: int, now: dt.datetime | None = None) -> ColumnElement[bool]:
    cutoff = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(seconds=ttl_s)
    return or_(
        LlmRequest.outcome == "unsettled",
        and_(LlmRequest.outcome == "dispatching", LlmRequest.created_at < cutoff),
    )


async def awaiting_reconciliation(session: AsyncSession, *, ttl_s: int) -> decimal.Decimal:
    """What the session's tenant has reserved in rows that can be reconciled."""
    total = await session.execute(
        select(func.coalesce(func.sum(LlmRequest.cost_usd), 0)).where(_reconcilable(ttl_s))
    )
    return decimal.Decimal(total.scalar_one())


async def list_reservations(engine: AsyncEngine, tenant_id: TenantId, *, ttl_s: int) -> list[Reservation]:
    async with tenant_transaction(engine, tenant_id) as session:
        rows = (
            await session.execute(
                select(LlmRequest).where(_reconcilable(ttl_s)).order_by(LlmRequest.created_at)
            )
        ).scalars()
        return [
            Reservation(row.id, row.run_id, row.purpose, row.model, row.outcome, row.cost_usd, row.created_at)
            for row in rows
        ]


async def release(
    engine: AsyncEngine,
    tenant_id: TenantId,
    row_id: uuid.UUID,
    *,
    cost: decimal.Decimal,
    reason: str,
    by: str,
    ttl_s: int,
) -> Reservation:
    """Settle one reconcilable row at ``cost``, between nothing and its reservation."""
    if not reason.strip() or not by.strip():
        raise NotReconcilable("a release needs a reason and the name of whoever made it")
    async with tenant_transaction(engine, tenant_id) as session:
        # The same lock the gateway takes, so a release and a reservation cannot interleave.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"llm-spend:{tenant_id}"}
        )
        row = (
            await session.execute(
                select(LlmRequest).where(LlmRequest.id == row_id, _reconcilable(ttl_s)).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotReconcilable(f"{row_id} is not a reconcilable reservation of this tenant")
        if not decimal.Decimal(0) <= cost <= row.cost_usd:
            raise NotReconcilable(f"the cost must be between 0 and the reservation, {row.cost_usd}")
        before = Reservation(
            row.id, row.run_id, row.purpose, row.model, row.outcome, row.cost_usd, row.created_at
        )
        await session.execute(
            update(LlmRequest)
            .where(LlmRequest.id == row_id)
            .values(outcome="reconciled", cost_usd=cost, refusal_reason=f"reconciled_from:{before.outcome}")
        )
        await append_event(
            session,
            AuditEventDraft(
                action=AuditAction.LLM_REQUEST_RECONCILED,
                actor_kind=ActorKind.USER,
                actor_id=by.strip()[:80],
                subject_kind=SubjectKind.LLM_REQUEST,
                subject_id=str(row_id),
                outcome=AuditOutcome.OK,
                reason=reason.strip()[:MAX_REASON],
                payload={
                    "previous_outcome": before.outcome,
                    "reserved_usd": str(before.reserved_usd),
                    "settled_usd": str(cost),
                    "run_id": str(before.run_id),
                },
            ),
            tenant_id=tenant_id,
        )
    return before


def main(argv: list[str] | None = None) -> int:
    from aria_llm_gateway.settings import ProviderSettings

    parser = argparse.ArgumentParser(
        description="List or release spend reservations the gateway could not settle."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--tenant", required=True, type=uuid.UUID)
    releasing = commands.add_parser("release")
    releasing.add_argument("--tenant", required=True, type=uuid.UUID)
    releasing.add_argument("--id", required=True, type=uuid.UUID)
    releasing.add_argument("--cost", required=True, type=decimal.Decimal, help="USD, as the Console shows it")
    releasing.add_argument("--reason", required=True)
    releasing.add_argument("--by", required=True, help="who checked the Console")
    args = parser.parse_args(argv)

    ttl_s = ProviderSettings().llm_reservation_ttl_s
    tenant = TenantId(args.tenant)

    async def run() -> int:
        engine = create_engine(get_settings().database_dsn())
        try:
            if args.command == "list":
                found = await list_reservations(engine, tenant, ttl_s=ttl_s)
                for item in found:
                    print(
                        f"{item.id}  {item.outcome:11}  ${item.reserved_usd}  {item.purpose}  {item.model}  "
                        f"run {item.run_id}  since {item.created_at.isoformat(timespec='seconds')}"
                    )
                held = sum((item.reserved_usd for item in found), decimal.Decimal(0))
                print(f"{len(found)} reconcilable, ${held} reserved")
                return 0
            try:
                before = await release(
                    engine, tenant, args.id, cost=args.cost, reason=args.reason, by=args.by, ttl_s=ttl_s
                )
            except NotReconcilable as error:
                print(f"error: {error}", file=sys.stderr)
                return 1
            print(f"released {before.id}: ${before.reserved_usd} reserved, settled at ${args.cost}")
            return 0
        finally:
            await engine.dispose()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
