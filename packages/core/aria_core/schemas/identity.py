"""Identifier types, so a tenant id cannot be passed where a user id is expected."""

from __future__ import annotations

import uuid
from typing import NewType

__all__ = ["PLATFORM_CHAIN_ID", "ApplicationId", "AuditChainId", "TenantId", "UserId", "new_id"]

TenantId = NewType("TenantId", uuid.UUID)
UserId = NewType("UserId", uuid.UUID)
ApplicationId = NewType("ApplicationId", uuid.UUID)

#: Audit chains are per tenant. Events that belong to no tenant (service start-up,
#: cross-tenant denials, admin actions) go on the platform chain, which is never
#: visible to a tenant-scoped session. See aria_core.audit.
AuditChainId = NewType("AuditChainId", uuid.UUID)
PLATFORM_CHAIN_ID = AuditChainId(uuid.UUID("00000000-0000-0000-0000-000000000000"))


def new_id() -> uuid.UUID:
    """Identifiers are generated in the application, not by the database.

    Row ids are part of the associated data that binds an encrypted S2 value to its
    row, so the id has to exist before the value is encrypted (SECURITY.md §10).
    """
    return uuid.uuid4()
