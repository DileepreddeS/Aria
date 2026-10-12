"""PDF reading for Stage 0: geometry in, a LayoutDocument out (RESUME_ENGINE §5.1-§5.6).

The order, each step in its own module:

1. **Triage** (triage.py): a locked file, no text layer, mostly image, or a font with
   no usable Unicode map ends the read with a reason. Nothing is guessed through.
2. **Glyphs** (glyphs.py): every character with its box, font and named weight, from
   pdfplumber.
3. **Lines** (lines.py): rows, runs, words rebuilt from character gaps, markers drawn
   or typed, rules, and weight by name or by ink.
4. **Reading order** (reading_order.py): columns from where the text is, header lines
   first, each column top to bottom, running heads set aside.
5. **Cross-check** (crosscheck.py): whether pypdf reads the same lines in the same
   order. Agree or disagree, a diagnostic, never a number.

The reading-order text is what ``text`` carries, markers in front, so the importer
that still reads text gets lines in the order a person reads them rather than the order
a generator wrote them; the layout itself travels alongside for the importer that
replaces it.

**Link annotations are extracted as data**, filtered to http/https/mailto, capped, and
appended as a labelled block. **Nothing is ever fetched**: a URI from an uploaded file
is untrusted input. Nothing in a PDF is executed. A page whose column order the
geometry cannot settle is refused with the reason, rather than read in an order nobody
chose.
"""

from __future__ import annotations

import io
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import pdfplumber
import pypdf

from aria_core.documents import (
    LINKS_HEADING,
    CrossExtractor,
    LayoutDocument,
    LayoutLine,
    LayoutPage,
    Triage,
)
from aria_document_parser import crosscheck, triage
from aria_document_parser.errors import ParseRefused
from aria_document_parser.glyphs import Glyph, page_glyphs
from aria_document_parser.ink import PageInk
from aria_document_parser.limits import ParseLimits
from aria_document_parser.lines import Row, drawn_markers, horizontal_rules, resolve_weight, rows
from aria_document_parser.reading_order import PageReading, read_page, set_aside_running_heads

# LINKS_HEADING is re-exported: this module writes the block, aria_core.importer
# reads it, and aria_core.documents owns the one definition both sides match on.
__all__ = ["LINKS_HEADING", "PdfExtraction", "extract_pdf"]

#: pdfminer (under pdfplumber) is loud about recoverable oddities in real-world PDFs.
#: Its warnings are not ours to act on, and they would dominate the worker's stderr.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

#: Schemes worth keeping. ``mailto`` carries the candidate's email, which is a profile
#: field; everything else — javascript, file, data — is either an attack or useless.
_ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "mailto"})


@dataclass(frozen=True, slots=True)
class PdfExtraction:
    #: The lines in reading order, then the links block.
    text: str
    page_count: int
    #: Notes for the upload record. Never a percentage.
    detail: str
    layout: LayoutDocument
    #: URIs from link annotations, filtered and de-duplicated. Never fetched.
    links: tuple[str, ...] = ()
    #: True when a person should check the imported facts against the original: the
    #: extractors disagreed about the order, or the page was laid out in columns.
    needs_review: bool = False

    @property
    def extractor(self) -> str:
        return "pdfplumber"

    @property
    def column_pages(self) -> tuple[int, ...]:
        return tuple(page.number for page in self.layout.pages if page.columns > 1)


def _links_from_page(page: Any, limits: ParseLimits) -> list[str]:
    """URIs from one page's link annotations.

    Only ``/A /URI`` actions are read. A ``/Launch``, ``/GoToR`` or JavaScript action
    is ignored entirely rather than recorded, because there is nothing a resume needs
    from one and an unused value in a database is still a value someone may later use.
    """
    found: list[str] = []
    annotations = page.get("/Annots")
    if not annotations:
        return found
    try:
        annotations = list(annotations)
    except TypeError:
        return found
    for annotation in annotations[: limits.max_links_per_page]:
        try:
            resolved = annotation.get_object()
            action = resolved.get("/A")
            if action is None:
                continue
            uri = action.get_object().get("/URI")
        except Exception as error:
            # A malformed annotation is not a reason to lose the document's text, but
            # it is worth a line: it is also what a crafted file looks like.
            logging.getLogger(__name__).warning("pdf_annotation_unreadable: %s", type(error).__name__)
            continue
        if not uri:
            continue
        candidate = str(uri).strip()[: limits.max_link_length]
        if _is_safe_link(candidate):
            found.append(candidate)
    return found


def _is_safe_link(uri: str) -> bool:
    """Whether a URI is worth keeping as data. Nothing here fetches anything."""
    try:
        scheme = urlsplit(uri).scheme.lower()
    except ValueError:
        return False
    return scheme in _ALLOWED_LINK_SCHEMES


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication: a header link repeats on every page."""
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _open(content: bytes, limits: ParseLimits) -> pypdf.PdfReader:
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        # The lock first: an encrypted file's pages cannot be counted until it is
        # decrypted, and a password-protected resume is "locked", not "corrupt".
        locked = triage.lock_reason(reader)
        if locked is not None:
            verdict, why = triage.decide([], locked)
            raise ParseRefused(verdict.value, why)
        count = len(reader.pages)
    except ParseRefused:
        raise
    except Exception as error:
        raise ParseRefused(
            "unreadable_pdf",
            "This PDF could not be opened \u2014 it may be corrupt. Try re-exporting it from the "
            "document you edit, or paste the text instead.",
        ) from error
    if count > limits.max_pages:
        raise ParseRefused(
            "too_many_pages",
            f"This PDF has {count} pages and the limit is {limits.max_pages}. A resume is one or "
            "two pages; please upload the resume rather than a portfolio.",
        )
    return reader


def _flat(line: LayoutLine) -> str:
    """A line as the text importer reads it: the marker kept in front of the words.

    The layout records the marker separately; the flat text is for the importer that
    still reads text, which recognises a bullet by its leading glyph. A drawn marker is
    written as a bullet glyph here, which gives that importer the bullets of a
    list-style resume for the first time.
    """
    return f"{line.marker} {line.text}" if line.marker else line.text


def _body_size(lines: list[LayoutLine]) -> float:
    sizes = Counter(round(line.font_size, 1) for line in lines)
    return max(sizes, key=lambda size: (sizes[size], size)) if sizes else 0.0


def extract_pdf(content: bytes, limits: ParseLimits | None = None) -> PdfExtraction:
    """Read a PDF into lines in reading order, or refuse it with the reason."""
    resolved = limits or ParseLimits()
    reader = _open(content, resolved)

    links = _dedupe([link for page in reader.pages for link in _links_from_page(page, resolved)])
    links = links[: resolved.max_links_total]

    with pdfplumber.open(io.BytesIO(content)) as pdf, PageInk(content) as ink:
        glyphs: list[list[Glyph]] = [page_glyphs(page) for page in pdf.pages]
        total = sum(len(page) for page in glyphs)
        if total > resolved.max_characters:
            raise ParseRefused(
                "too_much_text",
                f"This PDF holds more than {resolved.max_characters:,} characters of text, which is "
                "far more than a resume. Please upload the resume itself.",
            )
        census = triage.census(content, pdf.pages, glyphs)
        verdict, why = triage.decide(census)
        if verdict is not Triage.READABLE:
            raise ParseRefused(verdict.value, why)

        page_rows: list[list[Row]] = []
        rotated: list[int] = []
        for number, page_glyph_list in enumerate(glyphs, start=1):
            found, turned = rows(page_glyph_list, number)
            page_rows.append(found)
            rotated.append(turned)
        flat, weight_source = resolve_weight([row for found in page_rows for row in found], ink.share)
        by_page: dict[int, list[Row]] = {}
        for row in flat:
            by_page.setdefault(row.page, []).append(row)

        readings: list[PageReading] = [
            read_page(
                by_page.get(number, []),
                page=number,
                width=float(page.width),
                weight_source=weight_source,
                markers=drawn_markers(page),
                rules=horizontal_rules(page),
            )
            for number, page in enumerate(pdf.pages, start=1)
        ]
        readings = set_aside_running_heads(readings, [float(page.height) for page in pdf.pages])
        sizes = [(float(page.width), float(page.height)) for page in pdf.pages]

    ambiguous = [reading for reading in readings if reading.ambiguous]
    if ambiguous:
        first = ambiguous[0]
        raise ParseRefused(
            "ambiguous_columns",
            f"Page {first.page} of this PDF is laid out in {first.columns} columns with text running "
            "across the gap between them, so the order it is meant to be read in does not follow "
            "from the page. ARIA will not guess, because reading columns the wrong way round "
            "attaches the wrong dates and the wrong employer to the wrong work. Please re-export "
            "the resume in a single column, or paste the text instead.",
        )

    lines: list[LayoutLine] = []
    pages: list[LayoutPage] = []
    for reading, page_census, (width, height), turned in zip(readings, census, sizes, rotated, strict=True):
        for line in reading.lines:
            lines.append(line.model_copy(update={"index": len(lines)}))
        pages.append(
            LayoutPage(
                number=reading.page,
                width=width,
                height=height,
                character_count=page_census.characters,
                rotated_characters=turned,
                unreadable_share=page_census.unreadable_share,
                body_size=_body_size(reading.lines),
                fonts=page_census.fonts,
                columns=reading.columns,
                gutters=reading.gutters,
                ambiguous_columns=reading.ambiguous,
                running_heads=tuple(reading.running_heads),
            )
        )

    pypdf_reading = "\n".join(page.extract_text() or "" for page in reader.pages)
    verdict_order = crosscheck.compare([line.text for line in lines], pypdf_reading)
    layout = LayoutDocument(
        triage=Triage.READABLE,
        pages=tuple(pages),
        lines=tuple(lines),
        links=tuple(links),
        cross_extractor=verdict_order,
    )

    text = "\n".join(_flat(line) for line in lines)
    if links:
        text = f"{text}\n\n{LINKS_HEADING}\n" + "\n".join(links)

    column_pages = [page.number for page in pages if page.columns > 1]
    notes = [
        "extractor=pdfplumber",
        f"{len(pages)} page(s)",
        f"{len(lines)} line(s)",
        f"{len(links)} link(s)",
        f"weight from {weight_source.value.replace('_', ' ')}",
        f"cross-extractor: {verdict_order.value}",
    ]
    if column_pages:
        notes.append(
            "columns found by geometry on "
            + ", ".join(f"page {number}" for number in column_pages)
            + "; read left to right"
        )
    if sum(rotated):
        notes.append(f"{sum(rotated)} rotated character(s) set aside, not read")
    if verdict_order is CrossExtractor.DISAGREE:
        notes.append(
            "an independent reading put the lines in a different order or found different text; "
            "check the imported entries against the original"
        )
    return PdfExtraction(
        text=text,
        page_count=len(pages),
        detail="; ".join(notes),
        layout=layout,
        links=tuple(links),
        needs_review=verdict_order is CrossExtractor.DISAGREE or bool(column_pages),
    )
