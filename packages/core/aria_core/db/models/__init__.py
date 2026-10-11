"""Every table ARIA has, in one namespace.

Split by area for readability — :mod:`.platform` for tenancy, audit, keys and
gateway accounting, :mod:`.graph` for the candidate graph — and re-exported here so
callers import from ``aria_core.db.models`` without tracking which file a table is
in. Both modules share the ``Base`` in :mod:`.base`, so Alembic sees one metadata.
"""

from __future__ import annotations

from aria_core.db.models.base import Base, created_at_column, month_column, uuid_primary_key
from aria_core.db.models.graph import (
    GRAPH_TENANT_TABLES,
    Achievement,
    Candidate,
    DeclaredSkill,
    Education,
    Experience,
    Fact,
    FactMetric,
    FactRevision,
    FactSkill,
    FactTheme,
    Motivation,
    ProfileField,
    Project,
    Upload,
    UserQuestion,
)
from aria_core.db.models.platform import (
    PLATFORM_TENANT_TABLES,
    Application,
    AuditEvent,
    ConsumedApplyTaskJti,
    LlmRequest,
    SensitiveProfile,
    Tenant,
    TenantDataKey,
    User,
)

#: Every table whose rows belong to exactly one tenant and must therefore be covered
#: by a row-level security policy. ``test_rls_cross_tenant.py`` compares this against
#: what the database actually enforces, so a new table without a policy fails there
#: rather than leaking.
TENANT_SCOPED_TABLES: frozenset[str] = PLATFORM_TENANT_TABLES | GRAPH_TENANT_TABLES

__all__ = [
    "GRAPH_TENANT_TABLES",
    "PLATFORM_TENANT_TABLES",
    "TENANT_SCOPED_TABLES",
    "Achievement",
    "Application",
    "AuditEvent",
    "Base",
    "Candidate",
    "ConsumedApplyTaskJti",
    "DeclaredSkill",
    "Education",
    "Experience",
    "Fact",
    "FactMetric",
    "FactRevision",
    "FactSkill",
    "FactTheme",
    "LlmRequest",
    "Motivation",
    "ProfileField",
    "Project",
    "SensitiveProfile",
    "Tenant",
    "TenantDataKey",
    "Upload",
    "User",
    "UserQuestion",
    "created_at_column",
    "month_column",
    "uuid_primary_key",
]
