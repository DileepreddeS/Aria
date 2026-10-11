"""File type detection from content (SECURITY.md §7).

"Never trust filename, extension or client-supplied MIME type." All three are
attacker-controlled: a browser sends whatever the page tells it to, and a filename is
just a string the client chose. The only thing that says what a file is, is the file.

What the product can read is PDF, DOCX and TXT. What it *accepts* is narrower:
``allow_docx`` defaults off, so a Word document is recognised and then declined with
an explanation. A .docx is a zip of XML — XXE, zip bombs, external relationships — and
every tool that writes one also writes a PDF, so the attack surface buys very little.
The reader stays built and tested; this is the switch.

PNG and JPEG are recognised so an image upload can be refused with a reason rather
than "invalid file": there is no OCR, by decision.

Everything else is refused by name, with what ARIA actually saw in the message, so a
user who uploads a Pages document is told that rather than "invalid file".

Hand-rolled rather than `python-magic`: that needs libmagic, which is a native
dependency with no good Windows story, and five signatures are twenty lines. The
trade-off is that this recognises exactly what it lists — which, for an allowlist, is
the behaviour wanted anyway.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO

__all__ = ["DetectedType", "UnsupportedFileType", "detect_type"]


class DetectedType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    PNG = "png"
    JPEG = "jpeg"


class UnsupportedFileType(Exception):
    """ARIA will not accept this file.

    ``advice`` is the sentence shown to the candidate. It names what ARIA saw and what
    to do instead, because "invalid file" tells nobody anything.
    """

    def __init__(self, detail: str, advice: str | None = None) -> None:
        self.detail = detail
        self.advice = advice or (
            "ARIA accepts PDF and plain text, and checks the file's content rather than its "
            "name or extension."
        )
        super().__init__(f"{detail}. {self.advice}")


@dataclass(frozen=True, slots=True)
class Signature:
    name: DetectedType
    magic: bytes


#: Checked in order. Order matters only for OOXML, which is a ZIP and so has to be
#: inspected further (see below).
_SIGNATURES = (
    Signature(DetectedType.PDF, b"%PDF-"),
    Signature(DetectedType.PNG, b"\x89PNG\r\n\x1a\n"),
    Signature(DetectedType.JPEG, b"\xff\xd8\xff"),
)

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

#: What makes a ZIP a Word document rather than any other OOXML file or a plain
#: archive. A .docm (macro-enabled) carries a different content type and is refused
#: here: SECURITY.md §7 says macros are never executed, and the simplest way to keep
#: that promise is not to accept the format.
_DOCX_CONTENT_TYPE = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _looks_like_docx(content: bytes) -> bool:
    """True for a Word .docx, by reading the archive's own content-type manifest."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            if "[Content_Types].xml" not in archive.namelist():
                return False
            # Read a bounded amount: the manifest of a real document is small, and an
            # enormous one is either corrupt or trying something.
            with archive.open("[Content_Types].xml") as handle:
                return _DOCX_CONTENT_TYPE in handle.read(64 * 1024)
    except (zipfile.BadZipFile, KeyError, OSError):
        return False


def _is_probably_text(content: bytes) -> bool:
    """True when the bytes decode as UTF-8 and hold no control characters.

    Last in the chain, so a binary format is never mistaken for a text file. A NUL
    byte is the clearest signal that something is not text, and rejecting other
    control characters keeps a terminal-escape payload out of a "plain text" resume.
    """
    if not content:
        return False
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    allowed_controls = {"\n", "\r", "\t", "\f"}
    return not any(character < " " and character not in allowed_controls for character in text)


def detect_type(content: bytes, *, allow_docx: bool = False) -> DetectedType:
    """What this file actually is, or raise :class:`UnsupportedFileType`.

    ``allow_docx`` comes from configuration and defaults off. A Word document is still
    *detected* when it is off — that is what lets the refusal say "this is a Word
    document, save it as a PDF" instead of guessing.
    """
    if not content:
        raise UnsupportedFileType("the file is empty")

    header = content[:16]
    for signature in _SIGNATURES:
        if header.startswith(signature.magic):
            return signature.name

    if any(header.startswith(magic) for magic in _ZIP_MAGIC):
        if _looks_like_docx(content):
            if not allow_docx:
                raise UnsupportedFileType(
                    "this is a Word document, and ARIA is not accepting .docx at the moment",
                    "Open it in Word or Google Docs and use Save As (or Download As) PDF — the "
                    "PDF keeps your LinkedIn and GitHub links clickable. ARIA accepts PDF and "
                    "plain text.",
                )
            return DetectedType.DOCX
        raise UnsupportedFileType(
            "the file is a ZIP archive but not a Word document (a .docm, .xlsx, .pptx or "
            "plain .zip would look like this)"
        )

    if _is_probably_text(content):
        return DetectedType.TXT

    raise UnsupportedFileType(f"the content does not match any accepted type (first bytes: {header[:8]!r})")
