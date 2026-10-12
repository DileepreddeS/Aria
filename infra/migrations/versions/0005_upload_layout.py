"""Keep Stage 0's reading of a PDF, and its cross-extractor diagnostic.

RESUME_ENGINE §5 replaces flat-text extraction with geometry: a PDF is read into lines
in reading order, each with its page, column and position. ``uploads.layout`` keeps
that reading (aria_core.documents.LayoutDocument as JSON) for two reasons. A refused
import returns "the reason and the reconstructed line stream" (§5.5), and the original
file is deleted after processing by default, so the stream has to be kept to be shown.
And a fact's ``source_line`` indexes into it, so the approval view can quote the line
a draft came from.

``uploads.cross_extractor`` is §5.6's diagnostic: whether an independent extractor read
the same lines in the same order. Two values, enforced by a CHECK. There is no column
for a percentage on purpose — the old parser wrote "agreement 100%" over nine
fabricated roles, and agreement is not evidence of correctness.

Both are nullable: uploads parsed before this migration have neither, and a .txt or a
refused PDF has nothing to record. The layout is the candidate's resume text (S1),
under the same row-level security as the rest of the row.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("layout", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("uploads", sa.Column("cross_extractor", sa.String(length=10), nullable=True))
    op.create_check_constraint(
        op.f("ck_uploads_cross_extractor_is_known"),
        "uploads",
        "cross_extractor IN ('agree', 'disagree')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_uploads_cross_extractor_is_known"), "uploads", type_="check")
    op.drop_column("uploads", "cross_extractor")
    op.drop_column("uploads", "layout")
