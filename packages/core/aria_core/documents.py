"""What the document parser hands to core: extracted text, and a page's layout.

Shared by the two sides that have to agree about it: ``services/document_parser``
produces it and ``aria_core.importer`` reads it. Defining it in either would leave the
other matching on a shape it does not own.

**The layout** (RESUME_ENGINE §5.1-§5.2, §5.6) is what Stage 0 reads a PDF into: every
line in reading order, rebuilt from character geometry rather than taken from an
extractor's text, with the position, size and weight that classification (§5.3) works
from, plus the triage verdict and the cross-extractor diagnostic. It crosses a process
boundary from a sandboxed worker, so it is a strict, immutable message and is validated
on arrival like any other untrusted input: a worker that has been subverted can at most
send a well-formed layout of a document that does not exist, never a malformed one.

Line text is the candidate's own resume and untrusted (S1). Geometry carries no
personal data (S0).

**The links block**: one constant and one function, shared by the two sides that have to agree about them:
``services/document_parser`` appends the links block, and ``aria_core.importer`` reads
it back. Defining it in either of those would leave the other matching on a string it
does not own, and a heading that drifts by one character silently turns a candidate's
LinkedIn URL into a resume bullet.

The links are kept as *text* rather than resolved into anything. They came out of an
uploaded file, so they are untrusted input: ARIA never fetches one (see
``docs/DECISIONS.md``), and the importer only decides which profile field a URL
belongs in.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from aria_core.schemas.base import AriaMessage
from aria_core.sensitivity import Sensitivity

__all__ = [
    "LINKS_HEADING",
    "CrossExtractor",
    "LayoutDocument",
    "LayoutLine",
    "LayoutPage",
    "LayoutSegment",
    "PageFont",
    "Triage",
    "WeightSource",
    "split_links_block",
]


class Triage(StrEnum):
    """Whether the PDF can be read at all (RESUME_ENGINE §5.1). Anything but READABLE
    ends the import with a reason; none of these is guessed through."""

    READABLE = "readable"
    NO_TEXT_LAYER = "no_text_layer"
    MOSTLY_IMAGE = "mostly_image"
    UNREADABLE_FONT = "unreadable_font"
    LOCKED = "locked"


class CrossExtractor(StrEnum):
    """§5.6: whether an independent extractor reads the same lines in the same order.

    Two values and nothing else. Agreement is not evidence of correctness — two
    extractors reading the same wrong content stream agree perfectly — so this is a
    diagnostic, never a score, and there is deliberately no field to hold a percentage.
    """

    AGREE = "agree"
    DISAGREE = "disagree"


class WeightSource(StrEnum):
    """How a line's boldness was decided (§5.1), so a heading that leaned on weight
    alone can be trusted less when the weight was inferred rather than named."""

    FONT_NAME = "font_name"
    GLYPH_DENSITY = "glyph_density"
    UNKNOWN = "unknown"


class PageFont(AriaMessage):
    """A font used on a page. §5.1 records whether it can be decoded at all."""

    #: The PostScript name with any six-letter subset prefix removed.
    name: Annotated[str, Sensitivity.S0] = Field(max_length=200)
    has_to_unicode: Annotated[bool, Sensitivity.S0]


class LayoutSegment(AriaMessage):
    """One run of text on a line, separated from the next by a gap wider than a word.

    A resume line is often two or three runs: "Nordvale Consulting — Software Engineer"
    at the left margin and "Feb 2024 - Jul 2025" at the right, sometimes in different
    weights. §5.3 classifies by these parts — a date counts only in the right-hand
    band, an entry header is bold where the employer is — so they travel separately.
    """

    text: Annotated[str, Sensitivity.S1] = Field(min_length=1, max_length=2000)
    x0: Annotated[float, Sensitivity.S0]
    x1: Annotated[float, Sensitivity.S0]
    font_size: Annotated[float, Sensitivity.S0] = Field(gt=0)
    bold: Annotated[bool, Sensitivity.S0]


class LayoutLine(AriaMessage):
    """One line in reading order. Coordinates are PDF points from the page's top-left."""

    #: Position in the reconstructed reading order; what ``source_line`` refers to.
    index: Annotated[int, Sensitivity.S0] = Field(ge=0)
    page: Annotated[int, Sensitivity.S0] = Field(ge=1)
    #: 0-based from the left; -1 for a line that spans every column (a header).
    column: Annotated[int, Sensitivity.S0] = Field(ge=-1)
    x0: Annotated[float, Sensitivity.S0]
    x1: Annotated[float, Sensitivity.S0]
    top: Annotated[float, Sensitivity.S0]
    bottom: Annotated[float, Sensitivity.S0]
    #: Where the text starts after any marker glyph. With x0 this is the hanging
    #: indent that tells a new bullet from a wrapped one (§5.3), glyph or no glyph.
    text_x0: Annotated[float, Sensitivity.S0]
    #: The line's words, rebuilt from character gaps (§5.3), marker excluded.
    text: Annotated[str, Sensitivity.S1] = Field(max_length=2000)
    #: The bullet glyph the line opened with, if any.
    marker: Annotated[str, Sensitivity.S0] = Field(default="", max_length=2)
    #: The dominant size and weight across the line's characters.
    font_size: Annotated[float, Sensitivity.S0] = Field(gt=0)
    bold: Annotated[bool, Sensitivity.S0]
    weight_source: Annotated[WeightSource, Sensitivity.S0]
    #: The line's runs, left to right; their text joined by single spaces is ``text``.
    segments: tuple[LayoutSegment, ...] = Field(min_length=1)
    #: A horizontal rule drawn just below the line: one of §5.3's heading signals.
    rule_below: Annotated[bool, Sensitivity.S0] = False

    @model_validator(mode="after")
    def _geometry_is_a_box(self) -> Self:
        if self.x1 < self.x0 or self.bottom < self.top:
            raise ValueError(f"line {self.index} has an inverted box")
        if not self.x0 <= self.text_x0 <= self.x1:
            raise ValueError(f"line {self.index} starts its text outside its own box")
        # A small tolerance: x positions are floats rounded independently.
        slack = 0.5
        previous_end = self.x0 - slack
        for segment in self.segments:
            if segment.x1 < segment.x0:
                raise ValueError(f"line {self.index} has an inverted segment")
            if segment.x0 < previous_end - slack or segment.x1 > self.x1 + slack:
                raise ValueError(f"line {self.index} has segments out of order or outside the line")
            previous_end = segment.x1
        if " ".join(segment.text for segment in self.segments) != self.text:
            raise ValueError(f"line {self.index}'s text is not its segments' text")
        return self


class LayoutPage(AriaMessage):
    number: Annotated[int, Sensitivity.S0] = Field(ge=1)
    width: Annotated[float, Sensitivity.S0] = Field(gt=0)
    height: Annotated[float, Sensitivity.S0] = Field(gt=0)
    character_count: Annotated[int, Sensitivity.S0] = Field(ge=0)
    #: Characters drawn rotated — a vertical label down a sidebar — which are not part
    #: of reading order and so are not in any line. Counted so the candidate is told
    #: what was set aside: text that disappears without a word is the worst failure a
    #: parser can have.
    rotated_characters: Annotated[int, Sensitivity.S0] = Field(default=0, ge=0)
    #: Share of characters that are ``(cid:N)`` or U+FFFD: a font with no usable map.
    unreadable_share: Annotated[float, Sensitivity.S0] = Field(ge=0.0, le=1.0)
    #: Modal line font size; what "bigger than body text" is measured against.
    body_size: Annotated[float, Sensitivity.S0] = Field(ge=0.0)
    fonts: tuple[PageFont, ...] = ()
    columns: Annotated[int, Sensitivity.S0] = Field(default=1, ge=1)
    #: Each gutter as (x0, x1).
    gutters: Annotated[tuple[tuple[float, float], ...], Sensitivity.S0] = ()
    #: A line crossed a gutter below the header band, so the order does not follow
    #: from the geometry (§5.2) and the import must end rather than guess.
    ambiguous_columns: Annotated[bool, Sensitivity.S0] = False
    #: Text repeated in the top or bottom margin of two or more pages — page numbers,
    #: a name banner — kept out of the body stream (§5.2). It can be a name: S1.
    running_heads: Annotated[tuple[str, ...], Sensitivity.S1] = ()

    @model_validator(mode="after")
    def _gutters_match_columns(self) -> Self:
        if len(self.gutters) != self.columns - 1:
            raise ValueError(f"page {self.number} has {self.columns} columns but {len(self.gutters)} gutters")
        return self


class LayoutDocument(AriaMessage):
    """A PDF read into lines, or the reason it could not be."""

    triage: Annotated[Triage, Sensitivity.S0]
    #: Why triage stopped the import, in words for the candidate. Never document text.
    triage_detail: Annotated[str, Sensitivity.S0] = Field(default="", max_length=500)
    pages: tuple[LayoutPage, ...] = ()
    #: Reading order across the whole document.
    lines: tuple[LayoutLine, ...] = ()
    #: URIs from link annotations: data only, never fetched.
    links: Annotated[tuple[str, ...], Sensitivity.S1] = ()
    #: None when triage stopped the import before there was anything to compare.
    cross_extractor: Annotated[CrossExtractor | None, Sensitivity.S0] = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.triage is not Triage.READABLE:
            if self.lines:
                raise ValueError("a document triage refused carries no lines")
            if not self.triage_detail:
                raise ValueError("a refusal must say why")
            return self
        if [line.index for line in self.lines] != list(range(len(self.lines))):
            raise ValueError("line indexes must run 0..n-1 in reading order")
        numbers = {page.number for page in self.pages}
        if [page.number for page in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError("pages must be numbered 1..n")
        stray = sorted({line.page for line in self.lines} - numbers)
        if stray:
            raise ValueError(f"lines refer to pages that are not there: {stray}")
        if self.cross_extractor is None:
            raise ValueError("a readable document records the cross-extractor diagnostic")
        return self


#: Appended to extracted text, once, before the URLs found in PDF link annotations.
#: Says where they came from and that nothing followed them, because the text is shown
#: to the candidate as the source of their imported facts.
LINKS_HEADING = "--- LINKS (from PDF annotations; not followed) ---"


def split_links_block(text: str) -> tuple[str, tuple[str, ...]]:
    """Split extracted text into ``(body, links)``.

    The block is always last and appears at most once, so this is a right-hand split
    rather than a scan. Text with no block comes back unchanged with no links, which
    is the ordinary case for a .txt upload and for a PDF with no annotations.
    """
    head, separator, tail = text.rpartition(f"\n{LINKS_HEADING}\n")
    if not separator:
        return text, ()
    links = tuple(line.strip() for line in tail.splitlines() if line.strip())
    return head.rstrip(), links
