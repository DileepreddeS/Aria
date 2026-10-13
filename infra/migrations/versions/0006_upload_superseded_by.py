"""Let a file be read again: an upload can be superseded by a later reading of it.

Uploads are deduplicated per candidate by the file's SHA-256, so uploading the same
PDF twice returns the first record. That was right while the parser stood still, and
wrong once it changed: every PDF uploaded before Stage 0 (migration 0005) has no
layout, and a re-upload handed back the old flat-text reading, and its "agreement
100%", without a word.

The old row cannot be rewritten in place. Every graph row an import created carries
``source_upload_id`` and a ``source_line`` that indexes that upload's stored text, so
replacing the text would make each of them quote the wrong line. A new reading is a
new row; the old row stays as it was and points at its replacement through
``superseded_by``.

The unique constraint on (candidate_id, sha256) becomes a unique index over the rows
not superseded: one current reading per file. The foreign key is deferred because the
two rows are written in one transaction, the old one pointing at the new one's id
before the new row is inserted.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("superseded_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_uploads_superseded_by"),
        "uploads",
        "uploads",
        ["superseded_by"],
        ["id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.drop_constraint(op.f("uq_uploads_candidate_id_sha256"), "uploads", type_="unique")
    op.create_index(
        "uq_uploads_candidate_id_sha256_current",
        "uploads",
        ["candidate_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("superseded_by IS NULL"),
    )


def downgrade() -> None:
    # Fails if a file has been read twice: the old constraint allows one row per file.
    op.drop_index("uq_uploads_candidate_id_sha256_current", table_name="uploads")
    op.create_unique_constraint(op.f("uq_uploads_candidate_id_sha256"), "uploads", ["candidate_id", "sha256"])
    op.drop_constraint(op.f("fk_uploads_superseded_by"), "uploads", type_="foreignkey")
    op.drop_column("uploads", "superseded_by")
