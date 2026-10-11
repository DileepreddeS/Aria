"""User questions: the table behind "ask, don't guess".

CLAUDE.md turns low confidence into a ``UserQuestion`` rather than a default answer,
and until now the importer had nowhere to put one — questions lived on the plan and
in the audit log, which means they vanished the moment the request ended. This table
is where they wait.

Three columns carry the weight:

``payload``
    What answering would create. A role the resume dated "Summer 2023" is not in the
    graph at all, so the question has to carry the whole proposal — employer, title,
    kind, bullets — or answering it could only record a date with nothing to attach
    it to. S1: it holds the candidate's own resume text.

``dedupe_key``
    Unique per candidate. Re-importing the same resume must not ask the same question
    again; a queue that grows by five every upload is a queue nobody reads, and that
    is the failure mode that ends with an unreviewed draft on a resume.

``status``
    ``open`` blocks (the policy engine already refuses to submit an application while
    any question is open), ``answered`` records what the candidate said, ``dismissed``
    is for a question they decided was not worth answering. Answered and dismissed
    rows are kept: the answer is the evidence behind whatever it created.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aria_app"

#: Mirrors aria_core.importer.plan.QuestionKind. A CHECK rather than a free string,
#: because the kind decides what answering does, and a typo would silently produce a
#: question nothing knows how to apply.
QUESTION_KINDS = (
    # From the importer.
    "missing_dates",
    "paid_or_unpaid",
    "employer_or_title",
    "columns_merged",
    "unknown_section",
    "unknown_skills",
    "possible_duplicate",
    # From the onboarding interviews.
    "missing_metric",
    "missing_context",
    "unevidenced_skill",
    "missing_profile_field",
    "motivation",
)

QUESTION_STATUSES = ("open", "answered", "dismissed")


def upgrade() -> None:
    op.create_table(
        "user_questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        # S1: the prompt quotes the candidate's own resume back to them.
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("source_upload_id", sa.Uuid(), nullable=True),
        sa.Column(
            "source_lines",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        # What answering would create, when the thing does not exist yet.
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        # Set once the question has produced a row, so the UI can link to it.
        sa.Column("subject_kind", sa.String(length=20), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by", sa.Uuid(), nullable=True),
        sa.Column(
            "dedupe_key",
            sa.String(length=200),
            nullable=False,
            comment="Identity of the question, so a re-import does not ask it twice.",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in QUESTION_KINDS) + ")",
            name=op.f("ck_user_questions_kind_is_known"),
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in QUESTION_STATUSES) + ")",
            name=op.f("ck_user_questions_status_is_known"),
        ),
        sa.CheckConstraint("length(btrim(prompt)) > 0", name=op.f("ck_user_questions_prompt_is_not_blank")),
        # An answered question must say who answered it and when. Without this, an
        # answer could appear with no name attached, and the row it created would
        # have no evidence behind it.
        sa.CheckConstraint(
            "(status <> 'answered') OR "
            "(answer IS NOT NULL AND answered_at IS NOT NULL AND answered_by IS NOT NULL)",
            name=op.f("ck_user_questions_answer_has_an_author"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            name=op.f("fk_user_questions_candidate_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["answered_by"], ["users.id"], name=op.f("fk_user_questions_answered_by"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_upload_id"],
            ["uploads.id"],
            name=op.f("fk_user_questions_source_upload_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_user_questions_tenant_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_questions")),
        sa.UniqueConstraint(
            "candidate_id", "dedupe_key", name=op.f("uq_user_questions_candidate_id_dedupe_key")
        ),
    )
    op.create_index(op.f("ix_user_questions_candidate_id"), "user_questions", ["candidate_id"], unique=False)
    # The open queue is read on every page load and before every application, so it
    # gets its own partial index rather than scanning answered history.
    op.create_index(
        "ix_user_questions_open",
        "user_questions",
        ["candidate_id"],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(op.f("ix_user_questions_tenant_id"), "user_questions", ["tenant_id"], unique=False)

    op.execute("ALTER TABLE user_questions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_questions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON user_questions FOR ALL TO {APP_ROLE} "
        "USING (tenant_id = aria_current_tenant()) "
        "WITH CHECK (tenant_id = aria_current_tenant())"
    )
    op.execute(
        "CREATE POLICY maintenance ON user_questions FOR ALL TO aria_migrate USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON user_questions TO {APP_ROLE}")
    op.execute(
        "COMMENT ON TABLE user_questions IS "
        "'Things ARIA will not guess. payload carries what answering would create.'"
    )


def downgrade() -> None:
    op.drop_index("ix_user_questions_open", table_name="user_questions")
    op.drop_index(op.f("ix_user_questions_tenant_id"), table_name="user_questions")
    op.drop_index(op.f("ix_user_questions_candidate_id"), table_name="user_questions")
    op.drop_table("user_questions")
