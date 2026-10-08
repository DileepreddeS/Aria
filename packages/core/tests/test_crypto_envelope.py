"""S2 encryption and its binding to one place (SECURITY.md §3, §10).

The interesting tests are not "does it round-trip" but "does a ciphertext refuse
to decrypt anywhere other than exactly where it was written".
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from aria_core.crypto.envelope import EnvelopeCipher, FieldRef, associated_data
from aria_core.crypto.kms import DATA_KEY_BYTES, LocalDevKms, generate_root_key_file

VISA = "F-1 OPT, EAD valid to 2027-05-31"


@pytest.fixture
def kms(tmp_path: Path) -> LocalDevKms:
    generate_root_key_file(tmp_path / "root.key")
    return LocalDevKms(tmp_path / "root.key")


@pytest.fixture
def cipher(kms: LocalDevKms) -> EnvelopeCipher:
    return EnvelopeCipher(kms)


@pytest.fixture
def ref() -> FieldRef:
    return FieldRef(
        tenant_id=uuid.uuid4(),
        table="sensitive_profile",
        column="work_authorization",
        row_id=uuid.uuid4(),
    )


class TestRootKeyHandling:
    def test_a_root_key_is_32_random_bytes(self, tmp_path: Path) -> None:
        first = generate_root_key_file(tmp_path / "a.key").read_bytes()
        second = generate_root_key_file(tmp_path / "b.key").read_bytes()
        assert len(first) == len(second) == DATA_KEY_BYTES
        assert first != second

    def test_an_existing_root_key_is_never_overwritten(self, tmp_path: Path) -> None:
        path = generate_root_key_file(tmp_path / "root.key")
        original = path.read_bytes()
        with pytest.raises(FileExistsError):
            generate_root_key_file(path)
        assert path.read_bytes() == original

    def test_a_key_may_not_be_written_inside_the_repository(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="inside the repository"):
            generate_root_key_file(tmp_path / "keys" / "root.key", repo_root=tmp_path)

    def test_a_missing_root_key_says_how_to_create_one(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="kms-init"):
            LocalDevKms(tmp_path / "absent.key")

    def test_a_truncated_root_key_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "short.key"
        path.write_bytes(b"too short")
        with pytest.raises(ValueError, match="expected 32"):
            LocalDevKms(path)

    def test_data_keys_are_stored_only_wrapped(self, cipher: EnvelopeCipher) -> None:
        material, wrapped = cipher.create_data_key()
        assert len(material) == DATA_KEY_BYTES
        assert material not in wrapped
        assert cipher.unwrap_data_key(wrapped) == material

    def test_another_root_key_cannot_unwrap(self, tmp_path: Path, cipher: EnvelopeCipher) -> None:
        _, wrapped = cipher.create_data_key()
        generate_root_key_file(tmp_path / "other.key")
        other = EnvelopeCipher(LocalDevKms(tmp_path / "other.key"))
        with pytest.raises(InvalidTag):
            other.unwrap_data_key(wrapped)


class TestFieldEncryption:
    def test_a_value_round_trips(self, cipher: EnvelopeCipher, ref: FieldRef) -> None:
        key, _ = cipher.create_data_key()
        assert cipher.decrypt(key, ref, cipher.encrypt(key, ref, VISA)) == VISA

    def test_the_plaintext_is_not_present_in_the_ciphertext(
        self, cipher: EnvelopeCipher, ref: FieldRef
    ) -> None:
        key, _ = cipher.create_data_key()
        stored = cipher.encrypt(key, ref, VISA)
        assert VISA.encode() not in stored
        assert b"F-1" not in stored

    def test_the_same_value_encrypts_differently_every_time(
        self, cipher: EnvelopeCipher, ref: FieldRef
    ) -> None:
        key, _ = cipher.create_data_key()
        # Otherwise a column of repeated answers would be readable by comparison.
        assert cipher.encrypt(key, ref, VISA) != cipher.encrypt(key, ref, VISA)

    def test_a_flipped_byte_is_detected(self, cipher: EnvelopeCipher, ref: FieldRef) -> None:
        key, _ = cipher.create_data_key()
        stored = bytearray(cipher.encrypt(key, ref, VISA))
        stored[-1] ^= 0x01
        with pytest.raises(InvalidTag):
            cipher.decrypt(key, ref, bytes(stored))

    def test_a_truncated_value_is_refused_before_decryption(
        self, cipher: EnvelopeCipher, ref: FieldRef
    ) -> None:
        key, _ = cipher.create_data_key()
        with pytest.raises(ValueError, match="too short"):
            cipher.decrypt(key, ref, b"\x00" * 8)

    def test_unicode_survives(self, cipher: EnvelopeCipher, ref: FieldRef) -> None:
        key, _ = cipher.create_data_key()
        value = "Hispanic/Latino — decline to self-identify ✓"
        assert cipher.decrypt(key, ref, cipher.encrypt(key, ref, value)) == value


class TestCiphertextIsBoundToItsPlace:
    """The associated data is tenant_id + table + column + row_id."""

    @pytest.fixture
    def key(self, cipher: EnvelopeCipher) -> bytes:
        return cipher.create_data_key()[0]

    def test_it_cannot_be_moved_to_another_row(
        self, cipher: EnvelopeCipher, ref: FieldRef, key: bytes
    ) -> None:
        stored = cipher.encrypt(key, ref, VISA)
        other_row = FieldRef(ref.tenant_id, ref.table, ref.column, uuid.uuid4())
        with pytest.raises(InvalidTag):
            cipher.decrypt(key, other_row, stored)

    def test_it_cannot_be_moved_to_another_column(
        self, cipher: EnvelopeCipher, ref: FieldRef, key: bytes
    ) -> None:
        stored = cipher.encrypt(key, ref, VISA)
        other_column = FieldRef(ref.tenant_id, ref.table, "eeo_answers", ref.row_id)
        with pytest.raises(InvalidTag):
            cipher.decrypt(key, other_column, stored)

    def test_it_cannot_be_moved_to_another_table(
        self, cipher: EnvelopeCipher, ref: FieldRef, key: bytes
    ) -> None:
        stored = cipher.encrypt(key, ref, VISA)
        other_table = FieldRef(ref.tenant_id, "candidate_facts", ref.column, ref.row_id)
        with pytest.raises(InvalidTag):
            cipher.decrypt(key, other_table, stored)

    def test_it_cannot_be_read_as_another_tenants_value(
        self, cipher: EnvelopeCipher, ref: FieldRef, key: bytes
    ) -> None:
        stored = cipher.encrypt(key, ref, VISA)
        other_tenant = FieldRef(uuid.uuid4(), ref.table, ref.column, ref.row_id)
        with pytest.raises(InvalidTag):
            cipher.decrypt(key, other_tenant, stored)

    def test_one_tenants_key_cannot_decrypt_anothers_value(
        self, cipher: EnvelopeCipher, ref: FieldRef, key: bytes
    ) -> None:
        stored = cipher.encrypt(key, ref, VISA)
        other_key, _ = cipher.create_data_key()
        with pytest.raises(InvalidTag):
            cipher.decrypt(other_key, ref, stored)


class TestAssociatedDataIsUnambiguous:
    def test_parts_cannot_run_together(self) -> None:
        tenant_id, row_id = uuid.uuid4(), uuid.uuid4()
        # Plain concatenation would make these two identical and let a value move
        # between columns undetected.
        first = associated_data(FieldRef(tenant_id, "ab", "c", row_id))
        second = associated_data(FieldRef(tenant_id, "a", "bc", row_id))
        assert first != second

    def test_the_scheme_is_named_in_the_binding(self) -> None:
        binding = associated_data(FieldRef(uuid.uuid4(), "sensitive_profile", "eeo_answers", uuid.uuid4()))
        assert b"aria-s2-aesgcm-v1" in binding

    def test_a_field_needs_a_table_and_a_column(self) -> None:
        with pytest.raises(ValueError, match="table and a column"):
            FieldRef(uuid.uuid4(), "", "eeo_answers", uuid.uuid4())
