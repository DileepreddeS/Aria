"""The quarantine store (SECURITY.md §7).

Where an uploaded file sits between arriving and being parsed. Three properties, each
closing a specific hole:

**The name is generated, never derived from the client's.** A filename is a string the
client chose. ``../../.ssh/authorized_keys``, ``C:\\Windows\\System32\\x``, a name with
a NUL byte, a 4 KB name, a name that differs from an existing one only by case — all of
those are a path-traversal attempt or an overwrite, and none of them can happen if the
stored name is a UUID the server picked.

**Writes are exclusive.** ``O_EXCL`` means a collision fails rather than overwrites.

**The size limit is enforced while streaming.** Checking afterwards means a 2 GB upload
has already been in memory, which is the denial of service the limit exists to stop.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import structlog

__all__ = ["QuarantineStore", "StoredFile", "UploadTooLarge"]

logger = structlog.get_logger(__name__)


class UploadTooLarge(Exception):
    """The upload exceeded the configured limit."""

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(
            f"the file is larger than the {limit_bytes // (1024 * 1024)} MB limit (SECURITY.md \u00a77)"
        )
        self.limit_bytes = limit_bytes


@dataclass(frozen=True, slots=True)
class StoredFile:
    """A file in quarantine.

    ``name`` is the generated name, which is the only name anything should use.
    ``content`` is held in memory because every accepted type is at most 10 MB and the
    detector, the scanner and the parser all need the bytes; reading the file three
    times would be three chances to read something different from what was scanned.
    """

    name: str
    path: Path
    size_bytes: int
    sha256: str
    content: bytes


class QuarantineStore:
    """Files on disk, outside the repository and outside any web root."""

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        self._root = root
        self._max_bytes = max_bytes

    @property
    def root(self) -> Path:
        return self._root

    def _generated_name(self) -> str:
        """A server-chosen name. Nothing from the client reaches the filesystem."""
        return f"{uuid.uuid4().hex}.bin"

    async def store(self, chunks: AsyncIterator[bytes]) -> StoredFile:
        """Write a stream to quarantine, enforcing the limit as it goes.

        Raises :class:`UploadTooLarge` as soon as the limit is passed, and removes the
        partial file — an over-limit upload leaves nothing behind.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        name = self._generated_name()
        path = self._root / name

        digest = hashlib.sha256()
        buffer = bytearray()
        total = 0

        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                async for chunk in chunks:
                    total += len(chunk)
                    if total > self._max_bytes:
                        raise UploadTooLarge(self._max_bytes)
                    digest.update(chunk)
                    buffer.extend(chunk)
                    handle.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

        logger.info("upload_quarantined", name=name, size_bytes=total)
        return StoredFile(
            name=name,
            path=path,
            size_bytes=total,
            sha256=digest.hexdigest(),
            content=bytes(buffer),
        )

    async def store_bytes(self, content: bytes) -> StoredFile:
        """Convenience for callers that already hold the whole file."""

        async def one_chunk() -> AsyncIterator[bytes]:
            yield content

        return await self.store(one_chunk())

    def read(self, name: str) -> bytes:
        """Read a quarantined file by its generated name.

        The name is validated rather than trusted, even though it came from us: this
        is the one place a path is built from a string, so it is the one place worth
        being certain.
        """
        return self._resolve(name).read_bytes()

    def delete(self, name: str) -> None:
        """Remove the original. Called once a derivative exists (SECURITY.md §7)."""
        self._resolve(name).unlink(missing_ok=True)
        logger.info("upload_original_deleted", name=name)

    def exists(self, name: str) -> bool:
        try:
            return self._resolve(name).exists()
        except ValueError:
            return False

    def _resolve(self, name: str) -> Path:
        """Turn a stored name into a path inside the root, or refuse.

        Two checks rather than one: the name must look like a name we generated, and
        the resolved path must still be inside the root. The second catches anything
        the first did not think of, including symlinks and platform-specific path
        quirks.
        """
        if "/" in name or "\\" in name or name in {".", ".."} or "\x00" in name:
            raise ValueError(f"not a quarantine name: {name!r}")
        candidate = (self._root / name).resolve()
        if not candidate.is_relative_to(self._root.resolve()):
            raise ValueError(f"{name!r} resolves outside the quarantine store")
        return candidate
