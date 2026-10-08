"""S2 data in the database: ciphertext only, per tenant, survives rotation."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.crypto.data_keys import ensure_data_key, load_data_key, rotate_data_key
from aria_core.crypto.envelope import EnvelopeCipher, FieldRef
from aria_core.crypto.kms import LocalDevKms, generate_root_key_file
from aria_core.db.models import SensitiveProfile
from aria_core.db.sensitive_profile import (
    SensitiveProfileValues,
    read_sensitive_profile,
    write_sensitive_profile,
)
from aria_core.db.session import tenant_transaction
from aria_core.schemas.identity import TenantId, UserId

pytestmark = pytest.mark.db

VISA = "F-1 OPT, EAD valid to 2027-05-31"
EEO = "decline to self-identify"


@pytest.fixture
def cipher(tmp_path: Path) -> EnvelopeCipher:
    generate_root_key_file(tmp_path / "root.key")
    return EnvelopeCipher(LocalDevKms(tmp_path / "root.key"))


class TestStoredValues:
    async def test_values_round_trip_through_the_database(
        self, engine: AsyncEngine, cipher: EnvelopeCipher, tenant_a: TenantId, user_a: UserId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await write_sensitive_profile(
                session,
                cipher,
                tenant_id=tenant_a,
                user_id=user_a,
                values=SensitiveProfileValues(work_authorization=VISA, eeo_answers=EEO),
            )

        async with tenant_transaction(engine, tenant_a) as session:
            stored = await read_sensitive_profile(session, cipher, tenant_id=tenant_a, user_id=user_a)

        assert stored is not None
        assert stored.work_authorization == VISA
        assert stored.eeo_answers == EEO

    async def test_the_column_holds_no_plaintext(
        self, engine: AsyncEngine, cipher: EnvelopeCipher, tenant_a: TenantId, user_a: UserId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await write_sensitive_profile(
                session,
                cipher,
                tenant_id=tenant_a,
                user_id=user_a,
                values=SensitiveProfileValues(work_authorization=VISA),
            )

        async with tenant_transaction(engine, tenant_a) as session:
            raw = await session.scalar(
                select(SensitiveProfile.work_authorization_ciphertext).where(
                    SensitiveProfile.user_id == user_a
                )
            )
            # What a database dump or a read-only replica would show.
            as_text = await session.scalar(
                text(
                    "SELECT work_authorization_ciphertext::text FROM sensitive_profile "
                    "WHERE user_id = :user_id"
                ),
                {"user_id": user_a},
            )

        assert raw is not None
        assert VISA.encode() not in raw
        assert "F-1" not in str(as_text)

    async def test_a_missing_profile_reads_as_none(
        self, engine: AsyncEngine, cipher: EnvelopeCipher, tenant_a: TenantId, user_a: UserId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            assert await read_sensitive_profile(session, cipher, tenant_id=tenant_a, user_id=user_a) is None

    async def test_writing_twice_replaces_rather_than_duplicates(
        self, engine: AsyncEngine, cipher: EnvelopeCipher, tenant_a: TenantId, user_a: UserId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            first = await write_sensitive_profile(
                session,
                cipher,
                tenant_id=tenant_a,
                user_id=user_a,
                values=SensitiveProfileValues(work_authorization=VISA),
            )
        async with tenant_transaction(engine, tenant_a) as session:
            second = await write_sensitive_profile(
                session,
                cipher,
                tenant_id=tenant_a,
                user_id=user_a,
                values=SensitiveProfileValues(work_authorization="H-1B, approved"),
            )
            assert second == first
            rows = await session.scalar(
                text("SELECT count(*) FROM sensitive_profile WHERE user_id = :user_id"),
                {"user_id": user_a},
            )
            assert rows == 1

        async with tenant_transaction(engine, tenant_a) as session:
            stored = await read_sensitive_profile(session, cipher, tenant_id=tenant_a, user_id=user_a)
        assert stored is not None
        assert stored.work_authorization == "H-1B, approved"

    async def test_decrypted_values_do_not_print_themselves(self) -> None:
        values = SensitiveProfileValues(work_authorization=VISA)
        assert VISA not in repr(values)
        assert VISA not in str(values)
        assert VISA not in f"{values}"
        assert "work_authorization" in repr(values)


class TestDataKeys:
    async def test_each_tenant_gets_its_own_key(
        self, engine: AsyncEngine, cipher: EnvelopeCipher, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            key_a = await ensure_data_key(session, tenant_a, cipher)
        async with tenant_transaction(engine, tenant_b) as session:
            key_b = await ensure_data_key(session, tenant_b, cipher)

        assert key_a.material != key_b.material

    async def test_one_tenants_key_cannot_read_anothers_row(
        self,
        engine: AsyncEngine,
        cipher: EnvelopeCipher,
        tenant_a: TenantId,
        tenant_b: TenantId,
        user_a: UserId,
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            row_id = await write_sensitive_profile(
                session,
                cipher,
                tenant_id=tenant_a,
                user_id=user_a,
                values=SensitiveProfileValues(work_authorization=VISA),
            )
            stored = await session.scalar(
                select(SensitiveProfile.work_authorization_ciphertext).where(SensitiveProfile.id == row_id)
            )
        assert stored is not None

        async with tenant_transaction(engine, tenant_b) as session:
            key_b = await ensure_data_key(session, tenant_b, cipher)

        # Even holding the ciphertext and tenant B's key, and naming the row
        # correctly, the value does not come back.
        with pytest.raises(InvalidTag):
            cipher.decrypt(
                key_b.material,
                FieldRef(tenant_a, "sensitive_profile", "work_authorization", row_id),
                stored,
            )

    async def test_the_key_is_created_once_and_reused(
        self, engine: AsyncEngine, cipher: EnvelopeCipher, tenant_a: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            first = await ensure_data_key(session, tenant_a, cipher)
        async with tenant_transaction(engine, tenant_a) as session:
            again = await ensure_data_key(session, tenant_a, cipher)
        assert (first.version, first.material) == (again.version, again.material)

    async def test_rotation_keeps_existing_rows_readable(
        self, engine: AsyncEngine, cipher: EnvelopeCipher, tenant_a: TenantId, user_a: UserId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await write_sensitive_profile(
                session,
                cipher,
                tenant_id=tenant_a,
                user_id=user_a,
                values=SensitiveProfileValues(work_authorization=VISA),
            )

        async with tenant_transaction(engine, tenant_a) as session:
            rotated = await rotate_data_key(session, tenant_a, cipher)
            assert rotated.version == 2

        async with tenant_transaction(engine, tenant_a) as session:
            # The row was written under version 1 and is still readable, because the
            # reader uses the version the row records rather than the newest.
            stored = await read_sensitive_profile(session, cipher, tenant_id=tenant_a, user_id=user_a)
            assert stored is not None
            assert stored.work_authorization == VISA

            # New writes use the rotated key.
            await write_sensitive_profile(
                session,
                cipher,
                tenant_id=tenant_a,
                user_id=user_a,
                values=SensitiveProfileValues(work_authorization="H-1B, approved"),
            )
            version = await session.scalar(
                select(SensitiveProfile.key_version).where(SensitiveProfile.user_id == user_a)
            )
            assert version == 2

    async def test_an_unknown_key_version_is_an_error_not_a_guess(
        self, engine: AsyncEngine, cipher: EnvelopeCipher, tenant_a: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await ensure_data_key(session, tenant_a, cipher)
            with pytest.raises(LookupError, match="version 99"):
                await load_data_key(session, tenant_a, 99, cipher)
