"""Reading and writing S2 profile fields (SECURITY.md §3).

The only way to put an S2 value in the database or get one out. Plaintext never
reaches a column, a log line or a model; callers hand over a value and a purpose,
and get back a value only when they say which row and column they mean.

This is a small repository rather than a SQLAlchemy type decorator, because the
associated data includes the row id, and a type decorator binds values without
knowing which row they are destined for. Making the call site name the row is
also the honest interface: encrypting an S2 field is not a free conversion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.crypto.data_keys import ensure_data_key, load_data_key
from aria_core.crypto.envelope import EnvelopeCipher, FieldRef
from aria_core.db.models import SensitiveProfile
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId, UserId, new_id

__all__ = ["S2_COLUMNS", "SensitiveProfileValues", "read_sensitive_profile", "write_sensitive_profile"]

TABLE = SensitiveProfile.__tablename__

#: The S2 columns of this table, as the associated data names them.
S2_COLUMNS = ("work_authorization", "eeo_answers")


@dataclass(frozen=True, slots=True)
class SensitiveProfileValues:
    """Decrypted S2 values. Never logged, never serialized to the frontend without
    an explicit purpose and re-authentication (SECURITY.md §11)."""

    work_authorization: str | None = None
    eeo_answers: str | None = None

    def __repr__(self) -> str:
        present = [field.name for field in fields(self) if getattr(self, field.name) is not None]
        return f"<SensitiveProfileValues present={present}>"

    __str__ = __repr__


def _ref(tenant_id: TenantId, row_id: uuid.UUID, column: str) -> FieldRef:
    return FieldRef(tenant_id=tenant_id, table=TABLE, column=column, row_id=row_id)


async def write_sensitive_profile(
    session: AsyncSession,
    cipher: EnvelopeCipher,
    *,
    tenant_id: TenantId,
    user_id: UserId,
    values: SensitiveProfileValues,
) -> uuid.UUID:
    """Create or replace a user's S2 profile. Returns the row id.

    The row id is generated here, before encryption, because it is part of the
    binding that stops a ciphertext being moved to another row.
    """
    data_key = await ensure_data_key(session, tenant_id, cipher)

    existing = await session.scalar(select(SensitiveProfile.id).where(SensitiveProfile.user_id == user_id))
    row_id = existing or new_id()

    encrypted = {
        f"{column}_ciphertext": (
            cipher.encrypt(data_key.material, _ref(tenant_id, row_id, column), plaintext)
            if (plaintext := getattr(values, column)) is not None
            else None
        )
        for column in S2_COLUMNS
    }

    if existing is None:
        await session.execute(
            insert(SensitiveProfile).values(
                id=row_id,
                tenant_id=tenant_id,
                user_id=user_id,
                key_version=data_key.version,
                **encrypted,
            )
        )
    else:
        await session.execute(
            update(SensitiveProfile)
            .where(SensitiveProfile.id == row_id)
            .values(key_version=data_key.version, updated_at=utc_now(), **encrypted)
        )
    return row_id


async def read_sensitive_profile(
    session: AsyncSession,
    cipher: EnvelopeCipher,
    *,
    tenant_id: TenantId,
    user_id: UserId,
) -> SensitiveProfileValues | None:
    """Decrypt a user's S2 profile, or ``None`` when there is nothing stored.

    Decryption uses the key version the row was written with, so a rotated key
    does not orphan existing rows.
    """
    row = (
        await session.execute(
            select(
                SensitiveProfile.id,
                SensitiveProfile.key_version,
                SensitiveProfile.work_authorization_ciphertext,
                SensitiveProfile.eeo_answers_ciphertext,
            ).where(SensitiveProfile.user_id == user_id)
        )
    ).first()
    if row is None:
        return None

    row_id, key_version, *ciphertexts = row
    data_key = await load_data_key(session, tenant_id, key_version, cipher)
    decrypted = {
        column: (
            cipher.decrypt(data_key.material, _ref(tenant_id, row_id, column), stored)
            if stored is not None
            else None
        )
        for column, stored in zip(S2_COLUMNS, ciphertexts, strict=True)
    }
    return SensitiveProfileValues(**decrypted)
