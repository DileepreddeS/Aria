"""Record who reviewed a flagged upload, and when.

An upload is flagged needs_review when Stage 0's independent reading disagreed about
its line order, or its columns were rebuilt from geometry (RESUME_ENGINE §5.6). The
flag is a diagnostic, not a verdict, and the completeness report holds Phase 1 until
someone has looked. Until now nothing could record that they had: the flag stayed
set after the candidate read the lines and found them right.

``reviewed_at`` and ``reviewed_by`` record that look. Both or neither, by CHECK. The
flag itself is not cleared, so the upload still says why it needed a look.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("uploads", sa.Column("reviewed_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_uploads_reviewed_by"), "uploads", "users", ["reviewed_by"], ["id"], ondelete="SET NULL"
    )
    op.create_check_constraint(
        op.f("ck_uploads_review_has_a_reviewer"),
        "uploads",
        "(reviewed_at IS NULL) = (reviewed_by IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_uploads_review_has_a_reviewer"), "uploads", type_="check")
    op.drop_constraint(op.f("fk_uploads_reviewed_by"), "uploads", type_="foreignkey")
    op.drop_column("uploads", "reviewed_by")
    op.drop_column("uploads", "reviewed_at")
