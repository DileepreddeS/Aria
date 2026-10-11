"""Finding columns from the geometry of the page, not from the shape of the text.

A two-column resume is the one layout where both PDF readers are confidently wrong in
the same way. Each flattens a visual row into a line, so "Built 20+ React components"
and "Python, TypeScript, C++" arrive spliced together, the two extractors agree with
each other completely, and the cross-check in :mod:`aria_document_parser.pdf` sees a
consistent document. **Agreement means the readers are consistent, not that either is
right** (RESUME_ENGINE §8).

An earlier version caught this by looking for two known section headings on one line.
That works on "EXPERIENCE EDUCATION" and fails on the common case: a main column of
bullets beside a sidebar of skills, where every merged row reads like a plausible
sentence and nothing in the text looks wrong.

So the detection is geometric. pdfminer reports a bounding box for every text box on
the page, and a real two-column layout leaves a **vertical gutter** — a band of x
values that no text crosses, running the height of the page. That is a fact about the
page rather than a guess about the words, and it is the same fact a human eye uses.

Once the gutter is known the text can be put back in reading order: anything spanning
the gutter above the columns (the header), then the left column top to bottom, then the
right column, then anything spanning below (the footer). Where the geometry does not
settle the order — three columns, or a banner sitting *between* the two columns — this
says so and the caller refuses the file. Guessing an order is how a degree ends up
attached to a job.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from aria_document_parser.errors import ParseFailed
from aria_document_parser.limits import ParseLimits

__all__ = ["ColumnVerdict", "PageLayout", "TextBox", "analyse_page", "page_text_boxes"]


@dataclass(frozen=True, slots=True)
class TextBox:
    """One text box on a page, with the geometry pdfminer reported for it.

    PDF coordinates: x grows rightwards, y grows **upwards**, so the top of the page
    has the largest y. Reading order down a column is therefore descending y.
    """

    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def crosses(self, gutter: tuple[float, float]) -> bool:
        """True when this box spans the gutter, so it belongs to neither column."""
        return self.x0 < gutter[0] and self.x1 > gutter[1]


class ColumnVerdict:
    """What the geometry says about a page. Three outcomes, not two."""

    SINGLE = "single"
    """One column, or close enough that reading order is not in question."""
    RESOLVED = "resolved"
    """Columns found, and the reading order follows from the gutter."""
    AMBIGUOUS = "ambiguous"
    """Columns found and the order does not follow. The caller refuses the file."""


@dataclass(frozen=True, slots=True)
class PageLayout:
    """One page's geometry, and the text in whatever order it implies."""

    page_number: int
    verdict: str
    #: Text in reading order. For a single-column page this is simply the boxes top
    #: to bottom, which is what the extractors already produce.
    text: str
    #: The band of x values no text crosses, when there is one.
    gutter: tuple[float, float] | None = None
    column_count: int = 1
    #: Why the geometry did not settle the order, for the refusal message.
    reason: str = ""

    @property
    def has_columns(self) -> bool:
        return self.verdict in {ColumnVerdict.RESOLVED, ColumnVerdict.AMBIGUOUS}

    def describe(self) -> str:
        if self.verdict is ColumnVerdict.SINGLE:
            return f"page {self.page_number}: one column"
        gutter = f" (gutter {self.gutter[0]:.0f}-{self.gutter[1]:.0f}pt)" if self.gutter else ""
        if self.verdict == ColumnVerdict.RESOLVED:
            return f"page {self.page_number}: {self.column_count} columns{gutter}, read left to right"
        return f"page {self.page_number}: {self.column_count} columns{gutter}, {self.reason}"


def page_text_boxes(content: bytes, *, page_count: int, limits: ParseLimits) -> dict[int, list[TextBox]]:
    """Text boxes per 1-based page number, from pdfminer's layout analysis.

    Imported here rather than at module scope so the cost lands in the worker process
    that is already parsing a hostile file, and so a pdfminer that cannot load does
    not stop the rest of the parser from working.
    """
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LAParams, LTTextContainer
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise ParseFailed(f"pdfminer is unavailable: {error}") from error

    pages: dict[int, list[TextBox]] = {}
    wanted = list(range(min(page_count, limits.max_pages)))
    try:
        rendered = extract_pages(io.BytesIO(content), page_numbers=wanted, laparams=LAParams())
        for index, page in zip(wanted, rendered, strict=False):
            boxes: list[TextBox] = []
            for element in page:
                if not isinstance(element, LTTextContainer):
                    continue
                text = element.get_text()
                if not text.strip():
                    continue
                x0, y0, x1, y1 = (float(value) for value in element.bbox)
                boxes.append(TextBox(x0=x0, y0=y0, x1=x1, y1=y1, text=text))
                if len(boxes) >= limits.max_text_boxes_per_page:
                    # A page with thousands of boxes is a generated document or an
                    # attempt to make layout analysis expensive. The cap is the same
                    # kind of guard as the page cap.
                    break
            pages[index + 1] = boxes
    except Exception as error:
        raise ParseFailed(f"pdfminer layout analysis failed: {type(error).__name__}") from error
    return pages


def _gaps(boxes: list[TextBox], *, minimum: float) -> list[tuple[float, float]]:
    """Bands of x that no box in ``boxes`` covers, wider than ``minimum``.

    Computed by merging the boxes' x-intervals and reporting the holes. Margins are
    not gaps: only the space *between* text counts, which is what a gutter is.
    """
    if not boxes:
        return []
    intervals = sorted((box.x0, box.x1) for box in boxes)
    merged: list[list[float]] = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [
        (merged[index][1], merged[index + 1][0])
        for index in range(len(merged) - 1)
        if merged[index + 1][0] - merged[index][1] >= minimum
    ]


def _full_width(boxes: list[TextBox], *, margin_ratio: float) -> list[TextBox]:
    """Boxes reaching both margins: a spanning title, a rule, a footer.

    The test is both edges, not width. A long bullet in the left column is wide and
    reaches only one margin; a spanning header reaches both. Getting this wrong is
    what moved the gutter into the middle of the text.
    """
    left = min(box.x0 for box in boxes)
    right = max(box.x1 for box in boxes)
    tolerance = (right - left) * margin_ratio
    return [box for box in boxes if box.x0 <= left + tolerance and box.x1 >= right - tolerance]


def _vertical_overlap(left: list[TextBox], right: list[TextBox]) -> float:
    """How much of the shorter column runs alongside the taller one, 0.0 to 1.0.

    Two blocks of text at different x positions but different heights are a heading
    and an indented paragraph, not columns. Columns run beside each other.
    """
    if not left or not right:
        return 0.0
    left_top, left_bottom = max(box.y1 for box in left), min(box.y0 for box in left)
    right_top, right_bottom = max(box.y1 for box in right), min(box.y0 for box in right)
    overlap = min(left_top, right_top) - max(left_bottom, right_bottom)
    shorter = min(left_top - left_bottom, right_top - right_bottom)
    return max(overlap, 0.0) / shorter if shorter > 0 else 0.0


def _in_reading_order(boxes: list[TextBox]) -> list[TextBox]:
    """Top to bottom, then left to right for boxes on the same line."""
    return sorted(boxes, key=lambda box: (-box.y1, box.x0))


def _joined(boxes: list[TextBox]) -> str:
    return "\n".join(box.text.strip("\n") for box in boxes)


def analyse_page(boxes: list[TextBox], *, page_number: int, limits: ParseLimits) -> PageLayout:
    """Decide whether a page has columns, and in what order to read it."""
    if not boxes:
        return PageLayout(page_number=page_number, verdict=ColumnVerdict.SINGLE, text="")

    single = PageLayout(
        page_number=page_number,
        verdict=ColumnVerdict.SINGLE,
        text=_joined(_in_reading_order(boxes)),
    )

    # The gap is looked for among *all* the boxes first. Only if there is none do
    # full-width boxes get set aside and the search repeated: a spanning title covers
    # the gutter and hides it, but setting aside merely wide boxes moves the gutter
    # into the middle of the left column's longest line.
    gaps = _gaps(boxes, minimum=limits.min_gutter_points)
    if not gaps:
        full_width = _full_width(boxes, margin_ratio=limits.spanning_margin_ratio)
        if not full_width:
            return single
        body = [box for box in boxes if box not in full_width]
        gaps = _gaps(body, minimum=limits.min_gutter_points)
    if not gaps:
        return single

    if len(gaps) > 1:
        return PageLayout(
            page_number=page_number,
            verdict=ColumnVerdict.AMBIGUOUS,
            text=single.text,
            gutter=gaps[0],
            column_count=len(gaps) + 1,
            reason=(
                f"{len(gaps) + 1} columns, and nothing in the page says which order "
                "they are meant to be read in"
            ),
        )

    gutter = gaps[0]
    left = [box for box in boxes if box.x1 <= gutter[0]]
    right = [box for box in boxes if box.x0 >= gutter[1]]
    spanning = [box for box in boxes if box.crosses(gutter)]

    # A gutter with text on only one side is a margin, not a gutter.
    if not left or not right:
        return single

    characters = sum(len(box.text.strip()) for box in boxes)
    smaller = min(sum(len(box.text.strip()) for box in left), sum(len(box.text.strip()) for box in right))
    if characters == 0 or smaller / characters < limits.min_column_share:
        # One side holds almost nothing: a page number, a rule, a logo caption. Not a
        # column, and treating it as one would reorder the whole page around it.
        return single

    if _vertical_overlap(left, right) < limits.min_column_overlap:
        return single

    # A spanning box inside the column block means the page is sectioned: header,
    # columns, banner, more columns. The order across that banner does not follow
    # from the gutter, so this is where the parser stops rather than guesses.
    column_top = max(box.y1 for box in left + right)
    column_bottom = min(box.y0 for box in left + right)
    header = [box for box in spanning if box.y0 >= column_top]
    footer = [box for box in spanning if box.y1 <= column_bottom]
    if len(header) + len(footer) != len(spanning):
        return PageLayout(
            page_number=page_number,
            verdict=ColumnVerdict.AMBIGUOUS,
            text=single.text,
            gutter=gutter,
            column_count=2,
            reason=(
                "something spans both columns part-way down the page, so the order "
                "across it does not follow from the layout"
            ),
        )

    if len(left) + len(right) + len(spanning) != len(boxes):
        # A box that is neither in a column nor spanning the gutter means the gutter
        # is in the wrong place. Dropping it would make the candidate's own text
        # disappear with nothing reported, which is this parser's worst failure mode,
        # so the page is reported as ambiguous instead.
        return PageLayout(
            page_number=page_number,
            verdict=ColumnVerdict.AMBIGUOUS,
            text=single.text,
            gutter=gutter,
            column_count=2,
            reason=(
                "some text crosses into the gap between the columns, so the column "
                "boundaries are not where they appear to be"
            ),
        )

    ordered = (
        _in_reading_order(header)
        + _in_reading_order(left)
        + _in_reading_order(right)
        + _in_reading_order(footer)
    )
    return PageLayout(
        page_number=page_number,
        verdict=ColumnVerdict.RESOLVED,
        text=_joined(ordered),
        gutter=gutter,
        column_count=2,
    )


def analyse(content: bytes, *, page_count: int, limits: ParseLimits) -> dict[int, PageLayout]:
    """Layout for every page, keyed by 1-based page number."""
    boxes = page_text_boxes(content, page_count=page_count, limits=limits)
    return {
        number: analyse_page(page_boxes, page_number=number, limits=limits)
        for number, page_boxes in boxes.items()
    }
