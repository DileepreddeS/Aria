"""DOCX text extraction (SECURITY.md §7).

A .docx is a ZIP of XML. That gives it three ways to be hostile, and this module
answers each one rather than trusting a library to:

**XXE.** The XML can declare an external entity pointing at a local file or a URL.
``defusedxml`` is used instead of the stdlib parser, which resolves entities by
default. This is also why ``python-docx`` is not a dependency: it parses with ``lxml``,
and reading ``word/document.xml`` ourselves is about thirty lines and removes both the
dependency and the question of how it is configured.

**Zip bombs.** The archive's own directory declares uncompressed sizes before anything
is read, so the limits are checked against the declaration *and* against what actually
comes out — a lying header is itself a reason to stop.

**External relationships.** A document can reference remote images, templates or
sub-documents. Nothing here resolves a relationship target, and a relationship marked
``External`` is reported rather than followed: ARIA parses a file, it does not fetch
what the file points at.

Macros cannot arrive at all — a macro-enabled document is a different content type and
the upload detector refuses it before this module is reached.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from io import BytesIO

from defusedxml import ElementTree as DefusedElementTree

from aria_document_parser.errors import ParseFailed, ParseRefused
from aria_document_parser.limits import ParseLimits

__all__ = ["DocxExtraction", "extract_docx"]

_WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_BODY_PART = "word/document.xml"
#: Parts whose text is *not* body text. Contact details in a header are a real ATS
#: problem (RESUME_ENGINE §1.8), so their presence is reported rather than merged in.
_HEADER_FOOTER_PREFIXES = ("word/header", "word/footer")


@dataclass(frozen=True, slots=True)
class DocxExtraction:
    text: str
    detail: str
    #: True when a header or footer holds text. The importer warns about it: an ATS
    #: often cannot read a header, so contact details there may be invisible.
    has_header_or_footer_text: bool = False


def _check_archive_limits(archive: zipfile.ZipFile, content_size: int, limits: ParseLimits) -> None:
    entries = archive.infolist()
    if len(entries) > limits.max_archive_entries:
        raise ParseRefused(
            "too_many_parts",
            f"this document contains {len(entries)} internal parts, which is far more than "
            "a resume needs. Please re-save it from Word and upload again.",
        )

    declared = sum(entry.file_size for entry in entries)
    if declared > limits.max_uncompressed_bytes:
        raise ParseRefused(
            "archive_too_large",
            f"this document expands to {declared // (1024 * 1024)} MB, over the "
            f"{limits.max_uncompressed_bytes // (1024 * 1024)} MB limit.",
        )
    if content_size and declared // max(content_size, 1) > limits.max_compression_ratio:
        raise ParseRefused(
            "suspicious_compression",
            "this document expands to thousands of times its stored size, which is not "
            "how an ordinary Word file behaves. Please re-save it from Word.",
        )


def _read_part(archive: zipfile.ZipFile, name: str, limits: ParseLimits) -> bytes:
    """Read one part, refusing to trust the declared size.

    ``read()`` on a ZIP member will happily produce whatever the stream decompresses
    to, so the cap is applied to what actually arrives: a header that under-reports
    its size is exactly what a bomb does.
    """
    with archive.open(name) as handle:
        data = handle.read(limits.max_uncompressed_bytes + 1)
    if len(data) > limits.max_uncompressed_bytes:
        raise ParseRefused(
            "part_too_large",
            f"one part of this document ({name}) is larger than the limit allows.",
        )
    return data


def _paragraph_text(element: object) -> str:
    """Text of one paragraph, joined from its runs.

    ``w:t`` holds the characters; ``w:tab`` and ``w:br`` are structure that matters for
    reading a resume, because a tab is often what separates a job title from its dates.
    """
    pieces: list[str] = []
    for node in element.iter():  # type: ignore[attr-defined]
        tag = str(node.tag)
        if tag == f"{_WORD_NAMESPACE}t":
            pieces.append(node.text or "")
        elif tag == f"{_WORD_NAMESPACE}tab":
            pieces.append("\t")
        elif tag in (f"{_WORD_NAMESPACE}br", f"{_WORD_NAMESPACE}cr"):
            pieces.append("\n")
    return "".join(pieces)


def _external_relationship_count(archive: zipfile.ZipFile, limits: ParseLimits) -> int:
    """How many relationships point outside the document. None are followed."""
    count = 0
    for entry in archive.infolist():
        if not entry.filename.endswith(".rels"):
            continue
        try:
            root = DefusedElementTree.fromstring(_read_part(archive, entry.filename, limits))
        except Exception as error:
            # A malformed .rels is not a reason to lose the document, but it is worth
            # a line: it is also what a crafted archive looks like.
            logging.getLogger(__name__).warning(
                "docx_rels_unreadable: %s in %s", type(error).__name__, entry.filename
            )
            continue
        for relationship in root.iter():
            if relationship.get("TargetMode") == "External":
                count += 1
    return count


def extract_docx(content: bytes, limits: ParseLimits | None = None) -> DocxExtraction:
    """Body text from a .docx, in document order."""
    resolved = limits or ParseLimits()

    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as error:
        raise ParseRefused(
            "unreadable_docx",
            "this file could not be opened as a Word document. Please re-save it and try again.",
        ) from error

    with archive:
        _check_archive_limits(archive, len(content), resolved)

        if _BODY_PART not in archive.namelist():
            raise ParseRefused(
                "not_a_word_document",
                "this archive has no document body, so it is not a Word document.",
            )

        try:
            root = DefusedElementTree.fromstring(_read_part(archive, _BODY_PART, resolved))
        except ParseRefused:
            raise
        except Exception as error:
            # defusedxml raises its own types for entity declarations; whatever the
            # cause, a body that will not parse safely is a refusal, not a failure.
            raise ParseRefused(
                "unsafe_or_malformed_xml",
                "this document's XML could not be read safely. Please re-save it from Word "
                f"and upload again. ({type(error).__name__})",
            ) from error

        paragraphs: list[str] = []
        total = 0
        for element in root.iter(f"{_WORD_NAMESPACE}p"):
            text = _paragraph_text(element)
            total += len(text)
            if total > resolved.max_characters:
                raise ParseRefused(
                    "too_much_text",
                    f"this document holds more than {resolved.max_characters:,} characters, "
                    "which is far more than a resume.",
                )
            paragraphs.append(text)

        header_footer_text = any(
            entry.filename.startswith(_HEADER_FOOTER_PREFIXES) and entry.file_size > 200
            for entry in archive.infolist()
        )
        external = _external_relationship_count(archive, resolved)

    text = "\n".join(paragraphs)
    if not text.strip():
        raise ParseFailed("the document body parsed but contained no text")

    notes = [f"{len(paragraphs)} paragraph(s), {len(text)} characters"]
    if header_footer_text:
        notes.append(
            "text found in a header or footer and not imported \u2014 an ATS often cannot read "
            "those, so check nothing important lives only there"
        )
    if external:
        notes.append(f"{external} external relationship(s) present and not followed")

    return DocxExtraction(text=text, detail="; ".join(notes), has_header_or_footer_text=header_footer_text)
