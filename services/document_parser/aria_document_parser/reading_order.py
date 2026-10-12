"""Columns, reading order and running heads, from where the text is (RESUME_ENGINE §5.2).

**Columns.** Project every run onto the x-axis. A gutter is a band of x that no run
crosses, at least 4% of the page wide, with text on both sides running alongside for
at least 60% of the body's height, and with each side holding a real share of the
characters. Runs in the header band — the top fifth of the page's text — are left out
of the projection, so a name and contact line spanning the page above two columns
does not hide the gutter beneath it. Zero gutters is one column, one is two, more is
more; columns are read left to right whatever their number.

The share rule is what keeps right-aligned dates from being a "column": on the gate
fixtures they are 3-5% of a page's characters. A page number or a caption beside the
text fails the height rule for the same reason.

**Order.** Header lines first, then each column top to bottom. A line is the runs of
one row that fall in one column, so a date stays with its header on a single-column
page and two columns never fuse. A run that crosses a gutter below the header band
means the page's order does not follow from its geometry: the page is marked
ambiguous, and the import ends rather than guessing which side the run belongs to.
No run is ever dropped — every one lands in a column, in the header, or on an
ambiguous page that refuses the import with the reason.

**Running heads.** A line in the top or bottom 6% of a page whose text, with digits
ignored, repeats on another page — "Page 1 of 2", a name banner — is taken out of the
body and kept separately. Read as body text it would open a phantom section on every
page.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from aria_core.documents import LayoutLine, WeightSource
from aria_document_parser.lines import Box, Row, Run, make_line

__all__ = [
    "HEADER_BAND",
    "MIN_COLUMN_HEIGHT",
    "MIN_COLUMN_SHARE",
    "MIN_GUTTER_WIDTH",
    "RUNNING_HEAD_BAND",
    "PageReading",
    "find_gutters",
    "read_page",
    "set_aside_running_heads",
]

#: §5.2: a gutter is at least this share of the page width.
MIN_GUTTER_WIDTH = 0.04
#: §5.2: the text either side of a gutter runs alongside for this share of the body.
MIN_COLUMN_HEIGHT = 0.6
#: Each side of a gutter holds at least this share of the body's characters; below
#: it, the "column" is dates, a page number or a caption.
MIN_COLUMN_SHARE = 0.15
#: §5.2: a gap only in the top fifth of the text is a header layout, not a column.
HEADER_BAND = 0.2
#: §5.2: running heads live in the top or bottom 6% of the page.
RUNNING_HEAD_BAND = 0.06
#: A band of x counts as empty when the text over it is at most this share of what a
#: typical column carries: a banner or two across a gutter, not a column.
_EMPTY_SHARE_OF_TYPICAL = 0.25
#: The gutter proper is the part of an empty band within this share of a typical
#: column's coverage of its emptiest point.
_CORE_SLACK = 0.1


@dataclass(slots=True)
class PageReading:
    """One page in reading order. Line indexes are page-local until the document is assembled."""

    page: int
    lines: list[LayoutLine]
    columns: int = 1
    gutters: tuple[tuple[float, float], ...] = ()
    ambiguous: bool = False
    running_heads: list[str] = field(default_factory=list)


def _header_bottom(all_runs: Sequence[Run]) -> float:
    top = min(run.top for run in all_runs)
    bottom = max(run.bottom for run in all_runs)
    return top + HEADER_BAND * (bottom - top)


def find_gutters(runs: Sequence[Run], width: float) -> list[tuple[float, float]]:
    """The gutters among ``runs`` (header runs already removed), left to right.

    A gutter is a band of x that is nearly empty — at most a line or two across it,
    against the many a column holds — with text either side running alongside for at
    least MIN_COLUMN_HEIGHT of the body ("spanning 60% of the page" in §5.2's words).
    Nearly empty rather than empty: one banner across two columns must not erase the
    gutter, or the columns are fused row by row and nobody is told. A run that crosses
    a gutter found this way is visible, and read_page refuses the page for it.
    """
    if not runs:
        return []
    left_edge = int(min(run.x0 for run in runs))
    right_edge = int(max(run.x1 for run in runs)) + 1
    body_top = min(run.top for run in runs)
    body_bottom = max(run.bottom for run in runs)
    body_height = max(body_bottom - body_top, 1e-6)
    # How much of the body's height is covered by text at each point of x.
    covered = [0.0] * (right_edge - left_edge + 1)
    for run in runs:
        for x in range(int(run.x0) - left_edge, int(run.x1) - left_edge + 1):
            covered[x] += run.bottom - run.top
    # "Empty" is judged against how much text a column holds, not against the body's
    # height: two short blocks far apart make the body tall without making the text
    # in them any thinner. A banner across a gutter adds one line; a column has many.
    occupied = sorted(height for height in covered if height > 0)
    typical = occupied[len(occupied) // 2] if occupied else 0.0
    empty_enough = _EMPTY_SHARE_OF_TYPICAL * typical
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for x, height in enumerate(covered):
        if height <= empty_enough:
            start = x if start is None else start
        elif start is not None:
            bands.append((start, x))
            start = None
    characters = sum(len(run.glyphs) for run in runs)
    gutters: list[tuple[float, float]] = []
    for band_start, band_end in bands:
        # The gutter proper is the emptiest part of the band: a column's ragged right
        # edge thins the coverage beside it without being the gap between columns.
        lowest = min(covered[band_start:band_end])
        core = [x for x in range(band_start, band_end) if covered[x] <= lowest + _CORE_SLACK * typical]
        start_x, end_x = float(core[0] + left_edge), float(core[-1] + left_edge + 1)
        if end_x - start_x < MIN_GUTTER_WIDTH * width or band_start == 0 or band_end >= len(covered) - 1:
            continue
        middle = (start_x + end_x) / 2
        left = [run for run in runs if (run.x0 + run.x1) / 2 < middle and not _crosses(run, start_x, end_x)]
        right = [run for run in runs if (run.x0 + run.x1) / 2 >= middle and not _crosses(run, start_x, end_x)]
        if not left or not right:
            continue
        alongside = min(max(r.bottom for r in left), max(r.bottom for r in right)) - max(
            min(r.top for r in left), min(r.top for r in right)
        )
        left_share = sum(len(run.glyphs) for run in left) / characters
        right_share = sum(len(run.glyphs) for run in right) / characters
        if alongside >= MIN_COLUMN_HEIGHT * body_height and min(left_share, right_share) >= MIN_COLUMN_SHARE:
            gutters.append((start_x, end_x))
    return _keep_columns_substantial(gutters, runs, characters)


def _crosses(run: Run, start: float, end: float) -> bool:
    """Whether a run reaches across a gutter from one side to the other."""
    return run.x0 < start - 0.5 and run.x1 > end + 0.5


def _keep_columns_substantial(
    gutters: list[tuple[float, float]], runs: Sequence[Run], characters: int
) -> list[tuple[float, float]]:
    """Drop gutters until no column between them is too thin to be one.

    Each gutter was checked against everything on either side of it, so a thin column
    can only be one squeezed between two gutters — a strip of dates down the middle.
    The narrower of the gutters bordering the thinnest column goes, merging the strip
    into its nearer neighbour, and the check repeats.
    """
    kept = list(gutters)
    while kept:
        lows = [float("-inf")] + [end for _, end in kept]
        highs = [start for start, _ in kept] + [float("inf")]
        shares = [
            sum(len(run.glyphs) for run in runs if run.x0 >= low and run.x1 <= high) / characters
            for low, high in zip(lows, highs, strict=True)
        ]
        thinnest = min(range(len(shares)), key=shares.__getitem__)
        if shares[thinnest] >= MIN_COLUMN_SHARE:
            break
        bordering = [index for index in (thinnest - 1, thinnest) if 0 <= index < len(kept)]
        narrowest = min(bordering, key=lambda index: kept[index][1] - kept[index][0])
        del kept[narrowest]
    return kept


def _column_of(run: Run, gutters: Sequence[tuple[float, float]]) -> int | None:
    """The column a run lies in, or None when it crosses a gutter."""
    for start, end in gutters:
        if _crosses(run, start, end):
            return None
    middle = (run.x0 + run.x1) / 2
    return sum(1 for start, end in gutters if middle >= (start + end) / 2)


def read_page(
    rows: Sequence[Row],
    *,
    page: int,
    width: float,
    weight_source: WeightSource,
    markers: Sequence[Box] = (),
    rules: Sequence[Box] = (),
) -> PageReading:
    """The page's lines in reading order, its columns, and whether the order is in doubt."""
    all_runs = [run for row in rows for run in row.runs]
    if not all_runs:
        return PageReading(page=page, lines=[])
    header_bottom = _header_bottom(all_runs)
    body_runs = [run for run in all_runs if run.bottom > header_bottom]
    gutters = find_gutters(body_runs, width)

    header: list[list[Run]] = []
    columns: list[list[list[Run]]] = [[] for _ in range(len(gutters) + 1)]
    ambiguous = False
    for row in rows:
        by_column: dict[int, list[Run]] = {}
        spanning: list[Run] = []
        for run in row.runs:
            column = _column_of(run, gutters) if gutters else 0
            if column is None:
                # Across a gutter: a header in the top band, otherwise a page whose
                # order the geometry does not settle.
                if run.bottom > header_bottom:
                    ambiguous = True
                spanning.append(run)
            else:
                by_column.setdefault(column, []).append(run)
        if spanning:
            # A header row is read as one line across the page, so a name centred over
            # two columns stays whole; on an ambiguous page the import is refused anyway.
            header.append(spanning + [run for runs in by_column.values() for run in runs])
            continue
        for column, runs in by_column.items():
            columns[column].append(runs)

    def build(groups: list[list[Run]], column: int) -> list[LayoutLine]:
        return [
            make_line(
                runs,
                index=0,
                page=page,
                column=column,
                weight_source=weight_source,
                markers=markers,
                rules=rules,
            )
            for runs in groups
        ]

    ordered = build(header, -1 if gutters else 0)
    for column, groups in enumerate(columns):
        ordered.extend(build(groups, column if gutters else 0))
    lines = [line.model_copy(update={"index": index}) for index, line in enumerate(ordered)]
    return PageReading(
        page=page, lines=lines, columns=len(gutters) + 1, gutters=tuple(gutters), ambiguous=ambiguous
    )


def _normalised(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def set_aside_running_heads(pages: Sequence[PageReading], heights: Sequence[float]) -> list[PageReading]:
    """Take repeated top and bottom margin lines out of every page's body."""
    if len(pages) < 2:
        return list(pages)

    def margin_lines(reading: PageReading, height: float) -> list[LayoutLine]:
        band = RUNNING_HEAD_BAND * height
        return [line for line in reading.lines if line.bottom <= band or line.top >= height - band]

    seen: dict[str, set[int]] = {}
    for reading, height in zip(pages, heights, strict=True):
        for line in margin_lines(reading, height):
            seen.setdefault(_normalised(line.text), set()).add(reading.page)
    repeated = {text for text, on in seen.items() if len(on) >= 2}

    result: list[PageReading] = []
    for reading, height in zip(pages, heights, strict=True):
        margins = {id(line) for line in margin_lines(reading, height) if _normalised(line.text) in repeated}
        heads = [line.text for line in reading.lines if id(line) in margins]
        body = [line for line in reading.lines if id(line) not in margins]
        body = [line.model_copy(update={"index": index}) for index, line in enumerate(body)]
        result.append(
            PageReading(
                page=reading.page,
                lines=body,
                columns=reading.columns,
                gutters=reading.gutters,
                ambiguous=reading.ambiguous,
                running_heads=heads,
            )
        )
    return result
