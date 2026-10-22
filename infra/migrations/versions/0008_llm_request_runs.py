"""Attribute each model request to the run that made it, so a run's spend can be capped.

The gateway caps spend per tenant per month and per run (DECISIONS 2026-10-20). Both caps
are counted from ``llm_requests``, never from a counter in memory: a crash loop restarts
the process and would reset the counter, then walk through the limit. The monthly cap
needs only ``tenant_id`` and ``created_at``; the run cap needs to know which rows belong to
one run, which nothing recorded until now.

``run_id`` is nullable because rows written before this revision have none. The gateway
refuses a request without one, so every row from here on carries it.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_requests", sa.Column("run_id", sa.UUID(), nullable=True))
    op.create_index("ix_llm_requests_tenant_id_run_id", "llm_requests", ["tenant_id", "run_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_requests_tenant_id_run_id", table_name="llm_requests")
    op.drop_column("llm_requests", "run_id")
