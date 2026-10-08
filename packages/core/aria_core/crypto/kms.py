"""Key wrapping (SECURITY.md §10).

S2 fields are encrypted with a per-tenant data key, and that data key is stored
only in wrapped form. Unwrapping is the one operation that needs the root key, so
it is the one operation behind this interface: swapping the development
implementation for a cloud KMS means implementing :class:`KmsClient` and changing
configuration, not touching any code that handles candidate data.

Phase 0 ships :class:`LocalDevKms`, whose root key is a file in ``%APPDATA%\\aria``
— outside the repository, so no ``git add -A`` can pick it up, and outside the
database, so a database dump alone decrypts nothing. It is refused outside
dev/ci by :class:`aria_core.config.Settings`. Choosing the managed KMS is deferred
until the cloud is chosen, as agreed in the Phase 0 plan.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["DATA_KEY_BYTES", "KmsClient", "LocalDevKms", "generate_root_key_file"]

#: AES-256 everywhere (SECURITY.md §10).
DATA_KEY_BYTES = 32
_NONCE_BYTES = 12
#: Domain separator, so a wrapped data key can never be mistaken for a field
#: ciphertext even if both ended up in the same column by accident.
_WRAP_CONTEXT = b"aria.data-key.v1"


@runtime_checkable
class KmsClient(Protocol):
    """Wraps and unwraps data keys. Never sees field plaintext."""

    @property
    def key_id(self) -> str:
        """Identifies the root key, stored next to each wrapped data key.

        A rotated root key keeps its old versions available, so existing data keys
        stay unwrappable.
        """

    def wrap(self, data_key: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalDevKms:
    """A file-backed root key. Development and CI only."""

    def __init__(self, root_key_path: Path, *, key_id: str = "dev-kms-root-1") -> None:
        self._path = root_key_path
        self._key_id = key_id
        self._root_key = self._read_root_key(root_key_path)

    @staticmethod
    def _read_root_key(path: Path) -> bytes:
        if not path.exists():
            raise FileNotFoundError(
                f"dev KMS root key not found at {path}. Create it with `./tasks.ps1 kms-init`."
            )
        material = path.read_bytes()
        if len(material) != DATA_KEY_BYTES:
            raise ValueError(
                f"dev KMS root key at {path} is {len(material)} bytes, expected {DATA_KEY_BYTES}"
            )
        return material

    @property
    def key_id(self) -> str:
        return self._key_id

    def wrap(self, data_key: bytes) -> bytes:
        if len(data_key) != DATA_KEY_BYTES:
            raise ValueError(f"a data key must be {DATA_KEY_BYTES} bytes, got {len(data_key)}")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        wrapped = AESGCM(self._root_key).encrypt(nonce, data_key, _WRAP_CONTEXT + self._key_id.encode())
        return nonce + wrapped

    def unwrap(self, wrapped: bytes) -> bytes:
        nonce, ciphertext = wrapped[:_NONCE_BYTES], wrapped[_NONCE_BYTES:]
        return AESGCM(self._root_key).decrypt(nonce, ciphertext, _WRAP_CONTEXT + self._key_id.encode())


def generate_root_key_file(path: Path, *, repo_root: Path | None = None) -> Path:
    """Create a dev KMS root key, refusing to overwrite or to land in the repo.

    Returns the path written. Raises if the file exists: silently replacing a root
    key would make every existing ciphertext unreadable.
    """
    if repo_root is not None:
        resolved = path.resolve()
        if resolved.is_relative_to(repo_root.resolve()):
            raise ValueError(
                f"refusing to write a key inside the repository ({resolved}). Keys belong in %APPDATA%\\aria."
            )
    if path.exists():
        raise FileExistsError(
            f"{path} already exists. Delete it deliberately if you mean to lose access to "
            "everything encrypted with it."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions before any bytes are written. On Windows
    # the mode is largely advisory; the real protection is that %APPDATA% is a
    # per-user directory and the file never enters the repository or a backup of it.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(secrets.token_bytes(DATA_KEY_BYTES))
    return path
