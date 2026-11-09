"""Resumes: each built page, its PDF and the artifacts that explain and reproduce it.

Step 11b (DECISIONS 2026-11-08). A resume holds the candidate's name, contact details and
facts, so it is S1: it lives in the main database under row-level security per tenant, the
PDF included, rather than in a file whose path could be guessed or traversed. Its generated
name is the row's id; the name it is downloaded as, First_Last_Resume.pdf, comes from the
approved name, never from the posting or the company (RESUME_ENGINE section 10).

The posting's text is kept so the input hash can be checked against what was built from. It
is untrusted content: stored as data, never placed in instruction text (SECURITY section 5).

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aria_app"

#: aria_resume.api.ResumeResult.status
STATUSES = ("ok", "needs_review", "no_fit")
#: aria_resume.schemas.FitDecision
FITS = ("strong", "ok", "weak", "no_fit")


def upgrade() -> None:
    op.create_table(
        "resumes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fit", sa.String(length=20), nullable=False),
        # Untrusted: the posting as given. Data only, never instruction text.
        sa.Column("jd_text", sa.Text(), nullable=False),
        sa.Column("jd_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("used_model", sa.Boolean(), nullable=False),
        sa.Column("versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # S1: the plan, the bullets with their fact ids, the evidence map, the validation report.
        sa.Column("artifacts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # S1: the printed page.
        sa.Column("pdf", sa.LargeBinary(), nullable=False),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in STATUSES) + ")",
            name=op.f("ck_resumes_status_is_known"),
        ),
        sa.CheckConstraint(
            "fit IN (" + ", ".join(f"'{fit}'" for fit in FITS) + ")", name=op.f("ck_resumes_fit_is_known")
        ),
        sa.CheckConstraint("length(btrim(jd_text)) > 0", name=op.f("ck_resumes_jd_text_is_not_blank")),
        sa.CheckConstraint("octet_length(pdf) > 0", name=op.f("ck_resumes_pdf_is_not_empty")),
        sa.CheckConstraint(
            "file_name ~ '^[A-Za-z0-9-]+(_[A-Za-z0-9-]+)*\\.pdf$'", name=op.f("ck_resumes_file_name_is_plain")
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], name=op.f("fk_resumes_candidate_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_resumes_created_by"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_resumes_tenant_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resumes")),
    )
    op.create_index(op.f("ix_resumes_tenant_id"), "resumes", ["tenant_id"], unique=False)
    op.create_index("ix_resumes_candidate_newest", "resumes", ["candidate_id", "created_at"], unique=False)

    op.execute("ALTER TABLE resumes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE resumes FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON resumes FOR ALL TO {APP_ROLE} "
        "USING (tenant_id = aria_current_tenant()) "
        "WITH CHECK (tenant_id = aria_current_tenant())"
    )
    op.execute("CREATE POLICY maintenance ON resumes FOR ALL TO aria_migrate USING (true) WITH CHECK (true)")
    # No UPDATE: a built resume is a record of what was built; a new build is a new row.
    op.execute(f"GRANT SELECT, INSERT, DELETE ON resumes TO {APP_ROLE}")
    op.execute("COMMENT ON TABLE resumes IS 'Built resume pages: S1. jd_text is untrusted data.'")


def downgrade() -> None:
    op.drop_index("ix_resumes_candidate_newest", table_name="resumes")
    op.drop_index(op.f("ix_resumes_tenant_id"), table_name="resumes")
    op.drop_table("resumes")
