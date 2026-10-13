"""Malware scanning (SECURITY.md §7).

Behind a protocol, because the implementation differs per environment and the rule
must not. Two implementations and one hard constraint:

:class:`ClamAvScanner` talks to a clamd daemon, which runs behind a docker-compose
profile. :class:`DevScanner` scans nothing and says so.

**The dev scanner reports ``NOT_SCANNED``, never ``CLEAN``.** A scanner that returns
"clean" without looking is worse than no scanner: it puts a reassuring word in the
audit trail and in the upload record, and the next person reads it as a fact. Every
upload it handles is permanently marked as unscanned, which is the truth.

**A process configured with the dev scanner refuses to start outside dev/ci.** That is
the part a convention cannot provide. Without it, the staging deployment that forgot
to set the scanner would accept real files from real people and record them as
unscanned, and nobody would notice until someone read the column.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

from aria_core.config import Settings

__all__ = ["ClamAvScanner", "DevScanner", "MalwareScanner", "ScanResult", "ScanStatus", "build_scanner"]

logger = structlog.get_logger(__name__)


class ScanStatus(StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"
    NOT_SCANNED = "not_scanned"
    """No scanner looked at this file. Recorded as such, forever."""
    ERROR = "error"
    """A scanner was configured but could not be reached or failed."""

    @property
    def may_be_parsed(self) -> bool:
        """Whether the pipeline may hand this file to the parser.

        ``NOT_SCANNED`` is allowed through in development — the alternative is being
        unable to build the feature at all — which is exactly why the start-up check
        exists to keep that combination out of production.
        """
        return self in {ScanStatus.CLEAN, ScanStatus.NOT_SCANNED}


@dataclass(frozen=True, slots=True)
class ScanResult:
    status: ScanStatus
    #: Which scanner, and what it said. Goes into uploads.scan_detail verbatim.
    detail: str

    @property
    def is_clean(self) -> bool:
        return self.status is ScanStatus.CLEAN


class MalwareScanner(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def actually_scans(self) -> bool:
        """False for stand-ins. Read by the start-up check, not by the pipeline."""
        ...

    async def scan(self, content: bytes) -> ScanResult: ...


class DevScanner:
    """Scans nothing. Development and CI only.

    Deliberately has no code path that returns ``CLEAN``: there is nothing to write
    that would make that true.
    """

    name = "dev-scanner"
    actually_scans = False

    async def scan(self, content: bytes) -> ScanResult:
        logger.warning(
            "upload_not_scanned",
            scanner=self.name,
            size_bytes=len(content),
            detail="no malware scanner is configured; the file is recorded as not scanned",
        )
        return ScanResult(
            status=ScanStatus.NOT_SCANNED,
            detail=f"{self.name}: not scanned (no scanner configured)",
        )


class ClamAvScanner:
    """A clamd daemon over its INSTREAM protocol.

    Spoken directly rather than through a client library: INSTREAM is a length-prefixed
    stream and a one-line reply, and a dependency that shells out to `clamscan` would
    be a second way for a file to reach a subprocess.
    """

    name = "clamav"
    actually_scans = True

    def __init__(self, host: str, port: int, *, timeout_seconds: float = 30.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout_seconds

    async def scan(self, content: bytes) -> ScanResult:
        try:
            return await asyncio.wait_for(self._scan(content), timeout=self._timeout)
        except TimeoutError:
            return ScanResult(ScanStatus.ERROR, f"{self.name}: timed out after {self._timeout}s")
        except OSError as error:
            return ScanResult(ScanStatus.ERROR, f"{self.name}: unreachable ({error.__class__.__name__})")

    async def _scan(self, content: bytes) -> ScanResult:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(b"zINSTREAM\x00")
            # Chunked, each chunk prefixed with its length; a zero-length chunk ends
            # the stream. 64 KiB keeps memory flat for a 10 MB upload.
            for start in range(0, len(content), 65536):
                chunk = content[start : start + 65536]
                writer.write(len(chunk).to_bytes(4, "big") + chunk)
            writer.write((0).to_bytes(4, "big"))
            await writer.drain()
            reply = (await reader.read(4096)).decode("utf-8", "replace").strip()
        finally:
            writer.close()
            await writer.wait_closed()

        if reply.endswith("OK") and "FOUND" not in reply:
            return ScanResult(ScanStatus.CLEAN, f"{self.name}: {reply}")
        if "FOUND" in reply:
            return ScanResult(ScanStatus.INFECTED, f"{self.name}: {reply}")
        return ScanResult(ScanStatus.ERROR, f"{self.name}: unexpected reply {reply!r}")


class ScannerMisconfigured(RuntimeError):
    """A stand-in scanner is configured somewhere it must not be."""


def assert_scanner_is_allowed(scanner: MalwareScanner, settings: Settings) -> None:
    """Refuse to serve with a scanner that does not scan, outside dev/ci.

    Called from the API's preflight checks. The failure this prevents is a staging or
    production deployment that forgot to configure clamd, accepting real files and
    recording them as unscanned — true, but unnoticed until someone reads the column.
    """
    if scanner.actually_scans or settings.is_development:
        return
    raise ScannerMisconfigured(
        f"{scanner.name} does not scan anything and env={settings.env}. Uploads from real users "
        "must be scanned (SECURITY.md \u00a77). Configure ARIA_CLAMAV_HOST, or run with ARIA_ENV=dev."
    )


def build_scanner(settings: Settings) -> MalwareScanner:
    """The scanner this environment is configured for.

    No silent fallback: if a host is set it is used, and if it is not, the dev scanner
    is returned and the start-up check decides whether that is acceptable here.
    """
    if settings.clamav_host:
        return ClamAvScanner(settings.clamav_host, settings.clamav_port)
    return DevScanner()
