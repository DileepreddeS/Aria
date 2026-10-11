"""Plain-text extraction.

The simplest case, and the one the founder's v1 skills notes arrive as. Still not
"return the bytes": the encoding has to be settled, line endings normalised, and the
length capped like everything else.

The upload detector has already confirmed the file decodes as UTF-8 with no control
characters, so this does not have to guess at an encoding — which is the part of
plain-text handling that usually goes wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from aria_document_parser.errors import ParseRefused
from aria_document_parser.limits import ParseLimits

__all__ = ["TextExtraction", "extract_text_file"]


@dataclass(frozen=True, slots=True)
class TextExtraction:
    text: str
    detail: str


def extract_text_file(content: bytes, limits: ParseLimits | None = None) -> TextExtraction:
    resolved = limits or ParseLimits()

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ParseRefused(
            "not_utf8",
            "this text file is not UTF-8. Please re-save it as UTF-8 and upload again.",
        ) from error

    if len(text) > resolved.max_characters:
        raise ParseRefused(
            "too_much_text",
            f"this file holds more than {resolved.max_characters:,} characters, which is far "
            "more than a resume or a notes file.",
        )

    # Normalise line endings so downstream line numbers mean one thing. Line offsets
    # are recorded on every draft fact, and a CRLF file would otherwise count
    # differently from an LF one.
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.count("\n") + 1
    return TextExtraction(text=normalised, detail=f"{lines} line(s), {len(normalised)} characters")
