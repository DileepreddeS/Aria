"""Record, per tenant, whether its candidate data may be sent to a model.

Step 9's writer is the first time a candidate's own facts leave the machine (DECISIONS
2026-10-20). Dileep accepted up to 30-day retention for his own data, with this switch as
the mechanism: off for every tenant until someone turns it on, so a new tenant's facts never
leave by default. The gateway refuses every purpose that carries candidate data while it is
off; turning it on is audited.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("model_writing_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tenants", "model_writing_opt_in")
