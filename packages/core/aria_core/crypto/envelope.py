"""Field-level encryption for S2 data (SECURITY.md §3, §10).

AES-256-GCM with a per-tenant data key, and — the part that matters — associated
data that binds every ciphertext to the exact place it belongs::

    tenant_id | table | column | row_id

Associated data is authenticated but not encrypted, so a ciphertext only decrypts
when all four match. A row's work-authorization value cannot be pasted into
another row, another column, another table or another tenant and still read back:
it fails with ``InvalidTag`` rather than returning a plausible answer. Without
that binding, an attacker who can write to one row could move a value they are
allowed to see into a row they are allowed to read.

The row id is part of the binding, which is why ARIA generates ids in the
application rather than letting the database assign them: the id has to exist
before the value is encrypted.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aria_core.crypto.kms import DATA_KEY_BYTES, KmsClient

__all__ = ["EnvelopeCipher", "FieldRef", "associated_data"]

_NONCE_BYTES: Final = 12
#: Versioned so a future change of scheme is distinguishable rather than ambiguous.
_SCHEME: Final = b"aria-s2-aesgcm-v1"


@dataclass(frozen=True, slots=True)
class FieldRef:
    """Exactly where one encrypted value lives."""

    tenant_id: uuid.UUID
    table: str
    column: str
    row_id: uuid.UUID

    def __post_init__(self) -> None:
        if not self.table or not self.column:
            raise ValueError("an encrypted field needs both a table and a column")


def associated_data(ref: FieldRef) -> bytes:
    """The authenticated binding for one field.

    Each part is length-prefixed, so no two different references can produce the
    same byte string. Plain concatenation would let ``table="ab", column="c"`` and
    ``table="a", column="bc"`` collide, which would quietly re-open the hole this
    binding exists to close.
    """
    parts = (
        _SCHEME,
        str(ref.tenant_id).encode(),
        ref.table.encode(),
        ref.column.encode(),
        str(ref.row_id).encode(),
    )
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


class EnvelopeCipher:
    """Encrypts and decrypts S2 fields with a tenant's data key.

    The data key is passed in per call rather than held: it is unwrapped for one
    operation and dropped, so it lives in memory for as short a time as the code
    allows (SECURITY.md §10).
    """

    def __init__(self, kms: KmsClient) -> None:
        self._kms = kms

    # ---------------------------------------------------------------- data keys
    def create_data_key(self) -> tuple[bytes, bytes]:
        """A fresh data key, returned as ``(data_key, wrapped_data_key)``.

        Only the wrapped half is ever stored.
        """
        data_key = secrets.token_bytes(DATA_KEY_BYTES)
        return data_key, self._kms.wrap(data_key)

    def unwrap_data_key(self, wrapped: bytes) -> bytes:
        return self._kms.unwrap(wrapped)

    @property
    def kms_key_id(self) -> str:
        return self._kms.key_id

    # ------------------------------------------------------------------- fields
    def encrypt(self, data_key: bytes, ref: FieldRef, plaintext: str) -> bytes:
        """Encrypt one field value. Output is ``nonce || ciphertext_and_tag``.

        A fresh random nonce per call, so encrypting the same answer twice gives
        different ciphertext and the column leaks nothing by comparison.
        """
        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = AESGCM(data_key).encrypt(nonce, plaintext.encode("utf-8"), associated_data(ref))
        return nonce + sealed

    def decrypt(self, data_key: bytes, ref: FieldRef, stored: bytes) -> str:
        """Decrypt one field value.

        Raises ``cryptography.exceptions.InvalidTag`` when the ciphertext was
        tampered with, or when ``ref`` does not name the place it was written.
        """
        if len(stored) <= _NONCE_BYTES:
            raise ValueError("stored value is too short to be an S2 ciphertext")
        nonce, sealed = stored[:_NONCE_BYTES], stored[_NONCE_BYTES:]
        plaintext = AESGCM(data_key).decrypt(nonce, sealed, associated_data(ref))
        return plaintext.decode("utf-8")
