"""Record who approved each graph row, not only each fact.

``facts`` has carried ``approved_by`` since the start, because approving a bullet is
the candidate asserting a claim and that needs a name against it. The other seven
tables carried only ``approved_at`` — so "I worked at Accenture from June 2024" could
become approved with nobody attached, which is the same claim in a different shape.

The audit log already names the actor for every transition, and that stays the
authoritative record. This puts the name on the row as well, for the same reason
``facts`` has it: the approval view reads rows, not chains, and a row that cannot say
who approved it cannot show it.

``ON DELETE SET NULL`` rather than CASCADE: removing a user must not remove the
history of what they approved. The audit chain keeps the name regardless.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every graph table with an approval status but no approver. Listed rather than
#: discovered, so adding a table means deciding about this column deliberately.
TABLES = (
    "experiences",
    "projects",
    "education",
    "profile_fields",
    "declared_skills",
    "achievements",
    "motivations",
)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("approved_by", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table}_approved_by"),
            table,
            "users",
            ["approved_by"],
            ["id"],
            ondelete="SET NULL",
        )
        # An approved row names who approved it. Enforced in the database because the
        # alternative is every future writer remembering, and the first one to forget
        # produces a claim nobody is attached to.
        op.create_check_constraint(
            op.f(f"ck_{table}_approval_has_an_author"),
            table,
            "(status <> 'approved') OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)",
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_constraint(op.f(f"ck_{table}_approval_has_an_author"), table, type_="check")
        op.drop_constraint(op.f(f"fk_{table}_approved_by"), table, type_="foreignkey")
        op.drop_column(table, "approved_by")
