"""Foundations: tenants, audit chain, S2 storage, gateway accounting.

The tables are the easy half. The important half is below them: the scope
functions, the row-level security policies and the grants that make a cross-tenant
read impossible rather than merely unlikely (SECURITY.md §2.4, §3, §12, §16).

Three things are worth reading carefully before changing this file.

**A forgotten scope is an error, not an empty result.** The policies compare
against ``aria_current_tenant()``, which raises when the transaction has no scope
set. A query that forgets to say whose data it is about fails loudly instead of
quietly returning nothing and looking like "no results".

**The platform chain is invisible to tenants.** ``audit_events`` carries both
tenant events and events that belong to no tenant (service start-up, cross-tenant
denials, admin actions). Platform rows have ``tenant_id IS NULL`` and live on the
nil-UUID chain; a tenant-scoped session cannot see or write them, and a
platform-scoped session cannot see or write tenant rows.

**The audit table is append-only for the application.** ``aria_app`` is granted
SELECT and INSERT and nothing else, so no amount of ORM misuse can rewrite
history. Together with the hash chain, an alteration has to be both authorized and
consistent to go unnoticed, and the chain verifier checks the second half.

Revision ID: 0001
Revises:
Created: 2026-10-08
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aria_app"

#: Tables whose every row belongs to exactly one tenant, identified by a
#: ``tenant_id`` column. Mirrors ``aria_core.db.models.TENANT_SCOPED_TABLES``;
#: a test compares both against the policies the database actually has.
TENANT_ID_TABLES = (
    "users",
    "tenant_data_keys",
    "sensitive_profile",
    "applications",
    "llm_requests",
    "consumed_apply_task_jtis",
)


def _execute_each(*statements: str) -> None:
    """Run one statement per call.

    asyncpg prepares every statement, and a prepared statement may hold only one
    command, so DDL blocks cannot be sent as a single string.
    """
    for statement in statements:
        op.execute(statement.strip())


def _create_tables() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_users_tenant_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("tenant_id", "email", name=op.f("uq_users_tenant_id_email")),
    )
    op.create_table(
        "tenant_data_keys",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("kms_key_id", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_tenant_data_keys_tenant_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "key_version", name=op.f("pk_tenant_data_keys")),
    )
    op.create_table(
        "sensitive_profile",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("work_authorization_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("eeo_answers_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_sensitive_profile_tenant_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sensitive_profile_user_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sensitive_profile")),
        sa.UniqueConstraint("user_id", name=op.f("uq_sensitive_profile_user_id")),
    )
    op.create_table(
        "applications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("job_ref", sa.String(length=500), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "state IN ('discovered', 'screening', 'ask_user', 'tailoring', 'validating', "
            "'repairing', 'ready', 'awaiting_approval', 'dispatched_to_runner', 'applying', "
            "'verifying', 'submitted', 'follow_up', 'outcome_rejected', 'outcome_assessment', "
            "'outcome_interview', 'outcome_offer', 'outcome_ghosted', 'skipped', 'blocked_captcha', "
            "'blocked_login', 'blocked_closed', 'blocked_policy', 'failed', 'waiting_for_user', "
            "'cancelled')",
            name=op.f("ck_applications_state_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_applications_tenant_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applications")),
    )
    op.create_index("ix_applications_tenant_id_state", "applications", ["tenant_id", "state"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("chain_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_kind", sa.String(length=40), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("subject_kind", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "(chain_id = '00000000-0000-0000-0000-000000000000'::uuid) = (tenant_id IS NULL)",
            name=op.f("ck_audit_events_platform_chain_has_no_tenant"),
        ),
        sa.CheckConstraint(
            "outcome IN ('allowed', 'denied', 'ok', 'failed')",
            name=op.f("ck_audit_events_outcome_is_known"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
        sa.UniqueConstraint("chain_id", "seq", name=op.f("uq_audit_events_chain_id_seq")),
        sa.UniqueConstraint("hash", name=op.f("uq_audit_events_hash")),
    )
    op.create_index("ix_audit_events_chain_id_seq", "audit_events", ["chain_id", "seq"])
    op.create_table(
        "llm_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("refusal_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_llm_requests_tenant_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_requests")),
    )
    op.create_index("ix_llm_requests_tenant_id_created_at", "llm_requests", ["tenant_id", "created_at"])
    op.create_table(
        "consumed_apply_task_jtis",
        sa.Column("jti", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("application_id", sa.UUID(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_consumed_apply_task_jtis_tenant_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("jti", name=op.f("pk_consumed_apply_task_jtis")),
    )
    op.create_index("ix_consumed_apply_task_jtis_expires_at", "consumed_apply_task_jtis", ["expires_at"])


def _create_scope_functions() -> None:
    """The session variables the policies read, with a loud failure when unset."""
    op.execute(
        textwrap.dedent(
            """
            CREATE FUNCTION aria_current_scope() RETURNS text
            LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
            DECLARE raw text := current_setting('aria.scope', true);
            BEGIN
                IF raw IS NULL OR raw = '' THEN
                    RAISE EXCEPTION
                        'aria.scope is not set: open the transaction through aria_core.db.session'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
                IF raw NOT IN ('tenant', 'platform') THEN
                    RAISE EXCEPTION 'aria.scope is %, expected tenant or platform', raw
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
                RETURN raw;
            END
            $$;
            """
        )
    )
    op.execute(
        textwrap.dedent(
            """
            CREATE FUNCTION aria_current_tenant() RETURNS uuid
            LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
            DECLARE raw text;
            BEGIN
                IF aria_current_scope() = 'platform' THEN
                    -- NULL compares false against every tenant_id, so a platform
                    -- session sees no tenant rows instead of all of them.
                    RETURN NULL;
                END IF;
                raw := current_setting('aria.tenant_id', true);
                IF raw IS NULL OR raw = '' THEN
                    RAISE EXCEPTION
                        'aria.tenant_id is not set for a tenant-scoped transaction'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
                RETURN raw::uuid;
            END
            $$;
            """
        )
    )
    _execute_each(
        "COMMENT ON FUNCTION aria_current_scope() IS "
        "'Transaction scope set by aria_core.db.session. Raises when unset.'",
        "COMMENT ON FUNCTION aria_current_tenant() IS "
        "'Tenant of the current transaction, NULL under platform scope. Raises when unset.'",
        "REVOKE ALL ON FUNCTION aria_current_scope() FROM PUBLIC",
        "REVOKE ALL ON FUNCTION aria_current_tenant() FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION aria_current_scope() TO {APP_ROLE}",
        f"GRANT EXECUTE ON FUNCTION aria_current_tenant() TO {APP_ROLE}",
    )


def _enable_row_security() -> None:
    """Row-level security on every table that holds tenant data.

    ``FORCE`` matters even though ``aria_app`` does not own these tables: it keeps
    the policies in effect if ownership ever changes, instead of silently granting
    the owner a view of everything.
    """
    for table in ("tenants", "audit_events", *TENANT_ID_TABLES):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # A tenant sees its own row; platform scope provisions new tenants.
    _execute_each(
        textwrap.dedent(
            f"""
            CREATE POLICY tenant_isolation ON tenants FOR ALL TO {APP_ROLE}
                USING (id = aria_current_tenant())
                WITH CHECK (id = aria_current_tenant())
            """
        ),
        textwrap.dedent(
            f"""
            CREATE POLICY platform_provisioning ON tenants FOR ALL TO {APP_ROLE}
                USING (aria_current_scope() = 'platform')
                WITH CHECK (aria_current_scope() = 'platform')
            """
        ),
    )

    # Rows carrying a tenant_id: visible and writable only under that tenant's
    # scope. Platform scope deliberately gets no policy here, so a platform
    # transaction cannot read user data at all.
    for table in TENANT_ID_TABLES:
        op.execute(
            textwrap.dedent(
                f"""
                CREATE POLICY tenant_isolation ON {table} FOR ALL TO {APP_ROLE}
                    USING (tenant_id = aria_current_tenant())
                    WITH CHECK (tenant_id = aria_current_tenant())
                """
            )
        )

    # The audit table holds both chains. A tenant may read and append only its own
    # chain, and the chain id must equal the tenant id so events cannot be filed
    # under someone else's history.
    _execute_each(
        textwrap.dedent(
            f"""
            CREATE POLICY tenant_chain ON audit_events FOR ALL TO {APP_ROLE}
                USING (tenant_id = aria_current_tenant())
                WITH CHECK (
                    tenant_id = aria_current_tenant()
                    AND chain_id = aria_current_tenant()
                )
            """
        ),
        textwrap.dedent(
            f"""
            CREATE POLICY platform_chain ON audit_events FOR ALL TO {APP_ROLE}
                USING (tenant_id IS NULL AND aria_current_scope() = 'platform')
                WITH CHECK (
                    tenant_id IS NULL
                    AND chain_id = '00000000-0000-0000-0000-000000000000'::uuid
                    AND aria_current_scope() = 'platform'
                )
            """
        ),
    )


def _grant_least_privilege() -> None:
    """What the application role may do, stated explicitly rather than inherited."""
    tables = ("tenants", *TENANT_ID_TABLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(tables)} TO {APP_ROLE}")

    # Append-only history: no UPDATE, no DELETE, for any reason (SECURITY.md §16).
    op.execute(f"REVOKE ALL ON audit_events FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON audit_events TO {APP_ROLE}")

    # Alembic's bookkeeping is the migration role's business, not the application's.
    # ALTER DEFAULT PRIVILEGES granted it along with everything else; take it back.
    op.execute(f"REVOKE ALL ON alembic_version FROM {APP_ROLE}")
    op.execute(
        "COMMENT ON TABLE audit_events IS "
        "'Append-only, hash-chained audit log. aria_app holds SELECT and INSERT only.'"
    )
    op.execute(
        "COMMENT ON TABLE sensitive_profile IS "
        "'S2 data. Columns hold AES-256-GCM ciphertext bound to tenant, table, column and row id.'"
    )
    op.execute(
        "COMMENT ON TABLE llm_requests IS "
        "'Per-request metadata and cost. Never prompts, completions or untrusted content.'"
    )


def upgrade() -> None:
    _create_tables()
    _create_scope_functions()
    _enable_row_security()
    _grant_least_privilege()


def downgrade() -> None:
    # Tables first: their policies depend on the scope functions, so the functions
    # cannot be dropped while a policy still references them.
    op.drop_index("ix_consumed_apply_task_jtis_expires_at", table_name="consumed_apply_task_jtis")
    op.drop_table("consumed_apply_task_jtis")
    op.drop_index("ix_llm_requests_tenant_id_created_at", table_name="llm_requests")
    op.drop_table("llm_requests")
    op.drop_index("ix_audit_events_chain_id_seq", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_applications_tenant_id_state", table_name="applications")
    op.drop_table("applications")
    op.drop_table("sensitive_profile")
    op.drop_table("tenant_data_keys")
    op.drop_table("users")
    op.drop_table("tenants")
    op.execute("DROP FUNCTION IF EXISTS aria_current_tenant()")
    op.execute("DROP FUNCTION IF EXISTS aria_current_scope()")
