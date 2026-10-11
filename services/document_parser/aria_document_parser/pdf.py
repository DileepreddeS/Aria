"""PDF text extraction (SECURITY.md §7, RESUME_ENGINE §8).

PDF is now the main path, so three things that used to be approximations are decided
here explicitly.

**Which extractor's text is used.** ``pypdf`` is the source of truth, always. It
preserves reading order better in practice, and "always pypdf" is a rule that can be
stated, tested and recorded. ``pdfminer.six`` runs alongside as a *check*, not a
candidate: its output is compared, never blended. The one exception is stated in code
and recorded on the upload — if pypdf produced nothing usable and pdfminer did, the
latter is used and the upload says so. Picking whichever happened to be longer, as an
earlier version did, meant the choice could flip between two runs of the same file.

**Low-agreement pages are flagged, not guessed at.** A two-column layout can come out
interleaved, and no amount of cleverness tells us which order was intended. Each page
is compared separately; pages where the two extractors disagree are listed, the upload
is marked as needing review, and the candidate is asked to check those facts against
the original. Guessing silently is how a resume ends up saying something the candidate
never wrote.

**Link annotations are extracted as data.** A resume's LinkedIn and GitHub URLs live in
link annotations, not in the text layer, so extracting the text alone loses them.
Those URIs are read, filtered to http/https/mailto, capped, and appended as a labelled
block. **Nothing is ever fetched**: a URI from an uploaded file is untrusted input, and
following one would make the parser a request-forgery tool on behalf of whoever sent
the file.

Nothing in a PDF is executed — JavaScript, embedded files, launch actions and
annotation actions other than plain URIs are never evaluated. A PDF with no text layer
is refused rather than OCR'd, because a scan is not ATS-safe either, so accepting one
would produce a resume the systems it is sent to cannot read.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from aria_core.documents import LINKS_HEADING
from aria_document_parser.errors import ParseFailed, ParseRefused
from aria_document_parser.layout import ColumnVerdict, PageLayout, analyse
from aria_document_parser.limits import ParseLimits

# LINKS_HEADING is re-exported: this module writes the block, aria_core.importer
# reads it, and aria_core.documents owns the one definition both sides match on.
__all__ = ["LINKS_HEADING", "PdfExtraction", "extract_pdf"]

#: pdfminer is loud about recoverable oddities in real-world PDFs. Its warnings are
#: not ours to act on, and they would otherwise dominate the worker's stderr.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

#: Schemes worth keeping. ``mailto`` carries the candidate's email, which is a profile
#: field; everything else — javascript, file, data — is either an attack or useless.
_ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "mailto"})


@dataclass(frozen=True, slots=True)
class PdfExtraction:
    text: str
    page_count: int
    #: Notes for the upload record: which extractor was used, agreement per page.
    detail: str
    #: URIs from link annotations, filtered and de-duplicated. Never fetched.
    links: tuple[str, ...] = ()
    #: 1-based page numbers where the extractors disagreed sharply.
    low_agreement_pages: tuple[int, ...] = ()
    #: Which extractor's text is in ``text``. Recorded so a surprising import can be
    #: explained later.
    extractor: str = "pypdf"
    #: True when a person should check the imported facts against the original.
    needs_review: bool = False
    #: 1-based pages where the geometry showed columns and the text was rebuilt in
    #: reading order. Flagged for review: the order follows from the gutter, which is
    #: a good reason to believe it and not a reason to skip checking it.
    column_pages: tuple[int, ...] = ()


def _alphanumeric(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _similarity(left: str, right: str) -> float:
    """Rough agreement between two extractions, on alphanumeric content only.

    Whitespace and punctuation differ constantly between extractors and say nothing
    about whether the *words* came out the same, so they are ignored.
    """
    first, second = _alphanumeric(left), _alphanumeric(right)
    if not first and not second:
        return 1.0
    if not first or not second:
        return 0.0
    shorter, longer = sorted((len(first), len(second)))
    return shorter / longer


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


def _extract_with_pypdf(content: bytes, limits: ParseLimits) -> tuple[list[str], list[str], int]:
    """``(page_texts, links, page_count)`` from pypdf."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(content))
    except PdfReadError as error:
        raise ParseRefused(
            "unreadable_pdf",
            "This PDF could not be opened — it may be corrupt or password-protected. Try "
            "re-exporting it from Word or Google Docs, or upload a plain-text version.",
        ) from error

    if reader.is_encrypted:
        # An empty-password PDF can sometimes be opened, but a resume should not be
        # encrypted at all, and guessing at passwords is not something to automate.
        raise ParseRefused(
            "encrypted_pdf",
            "This PDF is password-protected. Please upload an unprotected copy.",
        )

    pages = reader.pages
    if len(pages) > limits.max_pages:
        raise ParseRefused(
            "too_many_pages",
            f"This PDF has {len(pages)} pages and the limit is {limits.max_pages}. A resume is "
            "one or two pages; please upload the resume rather than a portfolio.",
        )

    page_texts: list[str] = []
    links: list[str] = []
    total = 0
    for page in pages:
        try:
            text = page.extract_text() or ""
        except Exception as error:  # a single bad page should not lose the document
            logging.getLogger(__name__).warning("pdf_page_failed: %s", type(error).__name__)
            page_texts.append("")
            continue
        total += len(text)
        if total > limits.max_characters:
            raise ParseRefused(
                "too_much_text",
                f"This PDF holds more than {limits.max_characters:,} characters of text, which "
                "is far more than a resume. Please upload the resume itself.",
            )
        page_texts.append(text)
        links.extend(_links_from_page(page, limits))

    return page_texts, links, len(pages)


def _pdfminer_page_texts(content: bytes, page_count: int, limits: ParseLimits) -> list[str]:
    """pdfminer's text, page by page, so agreement can be judged per page."""
    from pdfminer.high_level import extract_text

    texts: list[str] = []
    for index in range(min(page_count, limits.max_pages)):
        try:
            texts.append(extract_text(io.BytesIO(content), page_numbers=[index]))
        except Exception as error:
            raise ParseFailed(f"pdfminer failed on page {index + 1}: {type(error).__name__}") from error
    return texts


def extract_pdf(content: bytes, limits: ParseLimits | None = None) -> PdfExtraction:
    """Text, links and a review verdict from a PDF."""
    resolved = limits or ParseLimits()
    pypdf_pages, raw_links, page_count = _extract_with_pypdf(content, resolved)
    pypdf_text = "\n".join(pypdf_pages)

    notes: list[str] = []
    low_agreement: list[int] = []
    extractor = "pypdf"
    text = pypdf_text

    # ---- geometry first, because it can settle what the cross-check cannot see.
    layouts: dict[int, PageLayout] = {}
    try:
        layouts = analyse(content, page_count=page_count, limits=resolved)
    except ParseFailed as error:
        notes.append(f"layout analysis unavailable ({error.detail})")

    ambiguous = [layout for layout in layouts.values() if layout.verdict == ColumnVerdict.AMBIGUOUS]
    if ambiguous:
        first = ambiguous[0]
        raise ParseRefused(
            "ambiguous_columns",
            f"Page {first.page_number} of this PDF has {first.column_count} columns and "
            f"{first.reason}. ARIA will not guess the order, because reading the columns "
            "the wrong way round attaches the wrong dates and the wrong employer to the "
            "wrong work. Please re-export the resume in a single column.",
        )

    column_pages = sorted(
        number for number, layout in layouts.items() if layout.verdict == ColumnVerdict.RESOLVED
    )
    if column_pages:
        # The page was laid out in columns, so pypdf's reading order for it is known
        # to be wrong: it follows the content stream, which runs across the gutter a
        # visual row at a time. The geometric order replaces it for those pages only.
        rebuilt = list(pypdf_pages)
        for number in column_pages:
            if number - 1 < len(rebuilt):
                rebuilt[number - 1] = layouts[number].text
        pypdf_pages = rebuilt
        text = "\n".join(rebuilt)
        extractor = "pypdf+layout"
        notes.append(
            "columns found by geometry on "
            + ", ".join(f"page {number}" for number in column_pages)
            + "; text rebuilt in reading order ("
            + "; ".join(layouts[number].describe() for number in column_pages)
            + ")"
        )

    try:
        pdfminer_pages = _pdfminer_page_texts(content, page_count, resolved)
    except ParseFailed as error:
        # The cross-check is a check, not a dependency: without it the text still
        # stands, and the upload records that nobody verified the reading order.
        notes.append(f"cross-check unavailable ({error.detail})")
        pdfminer_pages = []
    else:
        for number, (left, right) in enumerate(zip(pypdf_pages, pdfminer_pages, strict=False), start=1):
            if number in column_pages:
                # Already explained, and already fixed. Reporting it again would send
                # the candidate to check a page whose order the geometry settled.
                continue
            agreement = _similarity(left, right)
            if agreement < resolved.extractor_disagreement_ratio:
                low_agreement.append(number)
        overall = _similarity(pypdf_text, "\n".join(pdfminer_pages))
        notes.append(f"agreement {overall:.0%}")

        # The one documented exception to "always pypdf": it found nothing usable and
        # pdfminer did. Stated here, recorded on the upload, and testable.
        pdfminer_text = "\n".join(pdfminer_pages)
        if not column_pages and len(pypdf_text.strip()) < resolved.min_pdf_characters <= len(
            pdfminer_text.strip()
        ):
            text = pdfminer_text
            extractor = "pdfminer"
            notes.append("pypdf found no usable text layer, so pdfminer's text was used")

    if len(text.strip()) < resolved.min_pdf_characters:
        raise ParseRefused(
            "no_text_layer",
            "This PDF has no text layer — it is an image or a scan. ARIA does not read scans, "
            "because a scanned resume cannot be parsed by the systems it would be sent to "
            "either. Please upload the file you exported from Word or Google Docs.",
        )

    links = _dedupe(raw_links)[: resolved.max_links_total]
    if links:
        text = f"{text}\n\n{LINKS_HEADING}\n" + "\n".join(links)

    detail = "; ".join(
        [
            f"extractor={extractor}",
            f"{page_count} page(s)",
            f"{len(text)} characters",
            f"{len(links)} link(s)",
            *notes,
            *(
                [
                    "pages where the extractors disagreed and the reading order is "
                    f"uncertain: {', '.join(str(number) for number in low_agreement)} — "
                    "check the imported facts from those pages against the original"
                ]
                if low_agreement
                else []
            ),
        ]
    )

    return PdfExtraction(
        text=text,
        page_count=page_count,
        detail=detail,
        links=tuple(links),
        low_agreement_pages=tuple(low_agreement),
        extractor=extractor,
        # A rebuilt page is worth a look even though the order follows from the
        # gutter: the gutter says which column comes first, not that a sidebar of
        # skills belongs where it landed.
        needs_review=bool(low_agreement) or bool(column_pages),
        column_pages=tuple(column_pages),
    )


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication: a header link repeats on every page."""
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique
