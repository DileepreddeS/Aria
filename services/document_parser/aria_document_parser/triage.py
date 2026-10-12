"""Whether a PDF can be read at all, decided before anything tries to (RESUME_ENGINE §5.1).

Four ways a resume PDF cannot be read, each ending the import with a reason for the
candidate rather than a guess:

======================================  =====================  ==========================
Signal                                  Meaning                Result
======================================  =====================  ==========================
encrypted, or extraction not permitted  a protected PDF        LOCKED
no characters at all                    a scan, or a picture   NO_TEXT_LAYER
fewer than 200 characters a page        mostly image           MOSTLY_IMAGE
over 25% ``(cid:N)`` or U+FFFD          a font with no usable  UNREADABLE_FONT
                                        Unicode map
======================================  =====================  ==========================

The decision is a pure function of a per-page census, so each row is testable without
building a PDF that triggers it. The census reads the file with pypdf (encryption,
permissions, font dictionaries) and pdfplumber (characters and images).

A permission bit that forbids extraction is respected even though it could be ignored.
Whoever made the PDF said its text is not to be copied out; ARIA asks the candidate
for another route instead of overriding that.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pypdf
from pypdf.constants import UserAccessPermissions

from aria_core.documents import PageFont, Triage
from aria_document_parser.glyphs import Glyph, base_font_name

__all__ = [
    "MAX_UNREADABLE_SHARE",
    "MIN_CHARACTERS_PER_PAGE",
    "PageCensus",
    "census",
    "decide",
    "lock_reason",
]

#: §5.1. A one-page resume is several thousand characters; under 200 a page, the
#: text layer is a caption on a picture of the resume rather than the resume.
MIN_CHARACTERS_PER_PAGE = 200
#: §5.1. Past a quarter of the characters unmappable, the text that does decode is
#: fragments, and parsing them would produce entries made of gaps.
MAX_UNREADABLE_SHARE = 0.25
#: An image covering this much of the page is the page: what a scanner produces.
FULL_PAGE_IMAGE_COVERAGE = 0.8

#: Other ways in (§5.8), offered with every refusal so the candidate is never stuck.
_OTHER_DOORS = "You can paste the text instead, or build your profile in the interview."


@dataclass(frozen=True, slots=True)
class PageCensus:
    """What triage needs to know about one page."""

    number: int
    width: float
    height: float
    characters: int
    unreadable: int
    full_page_image: bool
    fonts: tuple[PageFont, ...] = ()

    @property
    def unreadable_share(self) -> float:
        return self.unreadable / self.characters if self.characters else 0.0


def lock_reason(reader: pypdf.PdfReader) -> str | None:
    """Why the PDF is closed to extraction, or None when it is open."""
    if not reader.is_encrypted:
        return None
    if not reader.decrypt(""):
        return "it is protected by a password"
    permissions = reader.user_access_permissions
    # Unknown permissions on an encrypted file are treated as a refusal: the safe
    # reading of "cannot tell whether copying is allowed" is that it is not.
    if permissions is None or not permissions & UserAccessPermissions.EXTRACT:
        return "its permissions forbid copying text out of it"
    return None


def _fonts(page: pypdf.PageObject) -> tuple[PageFont, ...]:
    resources = page.get("/Resources")
    if resources is None:
        return ()
    fonts = resources.get_object().get("/Font")
    if fonts is None:
        return ()
    found: dict[str, bool] = {}
    for reference in fonts.get_object().values():
        font = reference.get_object()
        name = base_font_name(str(font.get("/BaseFont", "")).lstrip("/"))
        found[name] = found.get(name, False) or "/ToUnicode" in font
    return tuple(PageFont(name=name, has_to_unicode=mapped) for name, mapped in sorted(found.items()))


def _covers_the_page(image: dict[str, Any], width: float, height: float) -> bool:
    area = max(0.0, float(image["x1"]) - float(image["x0"])) * max(
        0.0, float(image["bottom"]) - float(image["top"])
    )
    return area >= FULL_PAGE_IMAGE_COVERAGE * width * height


def census(
    content: bytes, plumber_pages: Sequence[Any], glyphs: Sequence[Sequence[Glyph]]
) -> list[PageCensus]:
    """Count each page. ``plumber_pages`` and ``glyphs`` are pdfplumber's, page by page."""
    reader = pypdf.PdfReader(io.BytesIO(content))
    pages: list[PageCensus] = []
    for number, (page, page_glyphs) in enumerate(zip(plumber_pages, glyphs, strict=True), start=1):
        width, height = float(page.width), float(page.height)
        pages.append(
            PageCensus(
                number=number,
                width=width,
                height=height,
                characters=sum(1 for glyph in page_glyphs if not glyph.text.isspace()),
                unreadable=sum(1 for glyph in page_glyphs if glyph.unreadable),
                full_page_image=any(_covers_the_page(image, width, height) for image in page.images),
                fonts=_fonts(reader.pages[number - 1]),
            )
        )
    return pages


def decide(pages: Sequence[PageCensus], locked: str | None = None) -> tuple[Triage, str]:
    """The §5.1 verdict and, for a refusal, the reason in the candidate's terms."""
    if locked is not None:
        return Triage.LOCKED, f"ARIA cannot read this PDF because {locked}. {_OTHER_DOORS}"
    characters = sum(page.characters for page in pages)
    if characters == 0:
        scanned = any(page.full_page_image for page in pages)
        what = "a scanned image" if scanned else "a picture of text, or blank"
        return (
            Triage.NO_TEXT_LAYER,
            f"this PDF has no text layer — it is {what}, so neither ARIA nor an applicant tracking "
            f"system can read it. Export a PDF from the document you edit. {_OTHER_DOORS}",
        )
    if not pages or characters / len(pages) < MIN_CHARACTERS_PER_PAGE:
        return (
            Triage.MOSTLY_IMAGE,
            "this PDF holds very little text for its pages; most of it is probably an image. "
            f"Export a PDF from the document you edit. {_OTHER_DOORS}",
        )
    unreadable = sum(page.unreadable for page in pages)
    if unreadable / characters > MAX_UNREADABLE_SHARE:
        return (
            Triage.UNREADABLE_FONT,
            "this PDF uses a font whose letters cannot be decoded into text, so what ARIA would "
            f"read is fragments. Re-export it, or choose a different font. {_OTHER_DOORS}",
        )
    return Triage.READABLE, ""
