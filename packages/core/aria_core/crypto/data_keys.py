"""Per-tenant data keys, stored wrapped (SECURITY.md §10).

One current key per tenant, plus every retired version, so rotation never makes
existing ciphertext unreadable. A row records which version wrote it.

Rotation is yearly and on incident. It issues a new version and retires the old
one; re-encrypting existing rows is a separate background job, which is why the
reader looks up the version the row was written with rather than assuming the
latest.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.crypto.envelope import EnvelopeCipher
from aria_core.db.models import TenantDataKey
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId

__all__ = ["DataKey", "ensure_data_key", "load_data_key", "rotate_data_key"]


@dataclass(frozen=True, slots=True)
class DataKey:
    """An unwrapped data key and the version that identifies it.

    Held for the length of one operation. Never logged, never serialized: the
    class has no ``__str__`` that could render the material, and ``material`` is
    bytes, so an accidental f-string shows a byte repr rather than a usable key —
    still worth not doing.
    """

    version: int
    material: bytes


async def ensure_data_key(session: AsyncSession, tenant_id: TenantId, cipher: EnvelopeCipher) -> DataKey:
    """The tenant's current data key, creating one on first use."""
    current = await _current_version(session, tenant_id, cipher=cipher)
    if current is not None:
        return current

    material, wrapped = cipher.create_data_key()
    await session.execute(
        insert(TenantDataKey).values(
            tenant_id=tenant_id,
            key_version=1,
            wrapped_dek=wrapped,
            kms_key_id=cipher.kms_key_id,
        )
    )
    return DataKey(version=1, material=material)


async def load_data_key(
    session: AsyncSession, tenant_id: TenantId, version: int, cipher: EnvelopeCipher
) -> DataKey:
    """The specific version a stored row was written with."""
    wrapped = await session.scalar(
        select(TenantDataKey.wrapped_dek).where(
            TenantDataKey.tenant_id == tenant_id, TenantDataKey.key_version == version
        )
    )
    if wrapped is None:
        raise LookupError(f"no data key version {version} for this tenant")
    return DataKey(version=version, material=cipher.unwrap_data_key(wrapped))


async def rotate_data_key(session: AsyncSession, tenant_id: TenantId, cipher: EnvelopeCipher) -> DataKey:
    """Issue the next version and retire the current one.

    Existing rows stay readable: their version is still present, only marked
    retired, and re-encryption happens separately.
    """
    current = await _current_version(session, tenant_id)
    next_version = 1 if current is None else current.version + 1

    material, wrapped = cipher.create_data_key()
    if current is not None:
        await session.execute(
            update(TenantDataKey)
            .where(TenantDataKey.tenant_id == tenant_id, TenantDataKey.key_version == current.version)
            .values(retired_at=utc_now())
        )
    await session.execute(
        insert(TenantDataKey).values(
            tenant_id=tenant_id,
            key_version=next_version,
            wrapped_dek=wrapped,
            kms_key_id=cipher.kms_key_id,
        )
    )
    return DataKey(version=next_version, material=material)


async def _current_version(
    session: AsyncSession, tenant_id: TenantId, *, cipher: EnvelopeCipher | None = None
) -> DataKey | None:
    row = (
        await session.execute(
            select(TenantDataKey.key_version, TenantDataKey.wrapped_dek)
            .where(TenantDataKey.tenant_id == tenant_id, TenantDataKey.retired_at.is_(None))
            .order_by(TenantDataKey.key_version.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    version, wrapped = row
    if cipher is None:
        # Caller only needs the version number; unwrapping would touch the KMS for
        # nothing. ensure_data_key/rotate_data_key pass their own cipher when they
        # need the material.
        return DataKey(version=version, material=b"")
    return DataKey(version=version, material=cipher.unwrap_data_key(wrapped))
