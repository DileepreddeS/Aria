"""From characters to lines: baselines, runs, words, markers and weight (RESUME_ENGINE §5.2-§5.3).

Everything here works from geometry. The order a generator wrote characters in is
never consulted, and neither is any extractor's word grouping.

**Rows and runs.** Characters whose baselines agree within 0.3 font sizes share a
row (§5.2). A row is cut into runs wherever the gap between characters is wider than
1.5 font sizes: wider than any word space, even in justified text, narrower than the
tab that puts a date at the right margin or the gutter between two columns. Runs, not
rows, are what column detection sees; a line is then the runs of one row that fall in
one column, so a header and its right-aligned date stay one line in a single-column
page and a left and a right column never merge.

**Words are rebuilt from character gaps, always** (§5.3): a gap wider than a quarter
of the font size is a space. How an extractor groups characters into words changes
between versions; this does not.

**Markers.** A line opens a bullet when it starts with a marker glyph followed by a
gap, or when a small filled shape is drawn just left of it. The second is what CSS
`list-style` produces in a browser's PDF: no glyph in the text layer at all, the
bullet's first line and its wrapped lines starting at the same x, and a 3 pt circle
drawn beside the first line only. Without it, a wrapped tail like "mobile" is
indistinguishable from a new bullet — which is how the 2026-10-11 import invented
entries.

**Weight** comes from font names when any font in the document names a weight, and
otherwise from ink: a run noticeably darker than other runs of its size is bold
(§5.1). The source is recorded, so classification can trust a weight-only heading
less when it was measured rather than named.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from aria_core.documents import LayoutLine, LayoutSegment, WeightSource
from aria_document_parser.glyphs import Glyph

__all__ = [
    "BASELINE_TOLERANCE",
    "INK_BOLD_RATIO",
    "RUN_GAP",
    "WORD_GAP",
    "Box",
    "Row",
    "Run",
    "drawn_markers",
    "horizontal_rules",
    "make_line",
    "resolve_weight",
    "rows",
]

#: §5.2: characters within this many font sizes of a row's baseline share it.
BASELINE_TOLERANCE = 0.3
#: §5.3: a gap wider than this many font sizes between characters is a word boundary.
WORD_GAP = 0.25
#: A gap wider than this many font sizes ends a run; see the module docstring.
RUN_GAP = 1.5
#: §5.1: a run whose ink is this many times the median for its size is bold.
INK_BOLD_RATIO = 1.25
#: A drawn marker is no larger than this share of the text size, and sits within this
#: many font sizes of the text it marks.
_MARKER_MAX_SIZE = 0.7
_MARKER_MAX_DISTANCE = 2.5
#: A rule is a horizontal stroke at least this long, no thicker than this.
_RULE_MIN_LENGTH = 30.0
_RULE_MAX_THICKNESS = 2.0
#: A rule belongs to the line above it when it lies within this many font sizes below
#: the line and runs under most of it.
_RULE_MAX_DISTANCE = 1.2
_RULE_MIN_COVERAGE = 0.9
#: §5.3's marker glyphs, as escapes: bullet, white bullet, small square, triangular
#: bullet, middle dot, hyphen, en dash, asterisk, and the black circle and square
#: some generators use. A lone "o" is handled separately.
MARKER_GLYPHS = frozenset("\u2022\u25e6\u25aa\u2023\u00b7-\u2013*\u25cf\u25a0")
#: What a drawn marker is recorded as.
DRAWN_MARKER = "\u2022"


@dataclass(frozen=True, slots=True)
class Box:
    x0: float
    top: float
    x1: float
    bottom: float


@dataclass(frozen=True, slots=True)
class Run:
    """Characters on one baseline with no gap wider than RUN_GAP between them."""

    glyphs: tuple[Glyph, ...]
    page: int
    #: Resolved by resolve_weight; until then, from the font names alone.
    bold: bool = False

    @property
    def x0(self) -> float:
        return self.glyphs[0].x0

    @property
    def x1(self) -> float:
        return max(glyph.x1 for glyph in self.glyphs)

    @property
    def top(self) -> float:
        return min(glyph.top for glyph in self.glyphs)

    @property
    def bottom(self) -> float:
        return max(glyph.bottom for glyph in self.glyphs)

    @property
    def size(self) -> float:
        return _dominant([(glyph.size, 1) for glyph in self._ink_glyphs()]) or self.glyphs[0].size

    def _ink_glyphs(self) -> list[Glyph]:
        return [glyph for glyph in self.glyphs if not glyph.text.isspace()] or list(self.glyphs)

    @property
    def text(self) -> str:
        return _words(self.glyphs)

    @property
    def named_bold(self) -> bool:
        """The majority weight by name; a glyph whose name is silent counts as not bold."""
        glyphs = self._ink_glyphs()
        return sum(1 for glyph in glyphs if glyph.bold) * 2 > len(glyphs)


@dataclass(frozen=True, slots=True)
class Row:
    """One baseline across the page, as runs left to right."""

    runs: tuple[Run, ...]
    page: int


def _dominant(weighted: Sequence[tuple[float, int]]) -> float | None:
    totals: dict[float, int] = {}
    for value, weight in weighted:
        key = round(value, 1)
        totals[key] = totals.get(key, 0) + weight
    return max(totals, key=lambda key: (totals[key], key)) if totals else None


def _words(glyphs: Sequence[Glyph]) -> str:
    """The run's text with spaces wherever a character gap says there is one."""
    out: list[str] = []
    previous: Glyph | None = None
    for glyph in glyphs:
        if glyph.text.isspace():
            if out and out[-1] != " ":
                out.append(" ")
            continue
        if previous is not None and out and out[-1] != " ":
            gap = glyph.x0 - previous.x1
            if gap > WORD_GAP * max(glyph.size, previous.size):
                out.append(" ")
        out.append(glyph.text)
        previous = glyph
    return "".join(out).strip()


def _deduplicated(glyphs: Sequence[Glyph]) -> list[Glyph]:
    """Drop a character drawn twice in the same place.

    Some generators fake bold or a shadow by drawing the text twice, a fraction of a
    point apart; read as two characters it becomes "NNoorrddvvaallee".
    """
    kept: list[Glyph] = []
    for glyph in sorted(glyphs, key=lambda g: (round(g.top, 0), g.x0)):
        if kept:
            last = kept[-1]
            if (
                glyph.text == last.text
                and abs(glyph.x0 - last.x0) < 0.2 * glyph.size
                and abs(glyph.top - last.top) < 0.2 * glyph.size
            ):
                continue
        kept.append(glyph)
    return kept


def rows(glyphs: Sequence[Glyph], page: int) -> tuple[list[Row], int]:
    """The page's rows top to bottom, and how many rotated characters were set aside."""
    upright = [glyph for glyph in glyphs if glyph.upright]
    rotated = len(glyphs) - len(upright)
    clusters: list[list[Glyph]] = []
    anchor = 0.0
    for glyph in sorted(_deduplicated(upright), key=lambda g: (g.bottom, g.x0)):
        if clusters and abs(glyph.bottom - anchor) <= BASELINE_TOLERANCE * max(
            glyph.size, clusters[-1][0].size
        ):
            clusters[-1].append(glyph)
        else:
            clusters.append([glyph])
            anchor = glyph.bottom
    result: list[Row] = []
    for cluster in clusters:
        ordered = sorted(cluster, key=lambda g: g.x0)
        runs: list[list[Glyph]] = [[ordered[0]]]
        last_ink = ordered[0] if not ordered[0].text.isspace() else None
        for glyph in ordered[1:]:
            if not glyph.text.isspace() and last_ink is not None:
                if glyph.x0 - last_ink.x1 > RUN_GAP * max(glyph.size, last_ink.size):
                    runs.append([])
            runs[-1].append(glyph)
            if not glyph.text.isspace():
                last_ink = glyph
        made = [Run(tuple(run), page) for run in runs if any(not glyph.text.isspace() for glyph in run)]
        made = [replace(run, bold=run.named_bold) for run in made]
        if made:
            result.append(Row(tuple(made), page))
    return result, rotated


def drawn_markers(page: Any) -> list[Box]:
    """Small filled shapes on a page: candidate bullets drawn rather than typed."""
    found: list[Box] = []
    for shape in list(page.curves) + list(page.rects):
        if not shape.get("fill"):
            continue
        width, height = float(shape["x1"]) - float(shape["x0"]), float(shape["bottom"]) - float(shape["top"])
        if 0.5 <= width <= 12 and 0.5 <= height <= 12:
            found.append(
                Box(float(shape["x0"]), float(shape["top"]), float(shape["x1"]), float(shape["bottom"]))
            )
    return found


def horizontal_rules(page: Any) -> list[Box]:
    """Horizontal strokes long enough to be a section rule."""
    found: list[Box] = []
    for shape in list(page.lines) + list(page.rects):
        x0, x1 = float(shape["x0"]), float(shape["x1"])
        top, bottom = float(shape["top"]), float(shape["bottom"])
        if x1 - x0 >= _RULE_MIN_LENGTH and bottom - top <= _RULE_MAX_THICKNESS:
            found.append(Box(x0, top, x1, bottom))
    return found


def resolve_weight(
    all_rows: Sequence[Row], ink: Callable[[int, float, float, float, float], float] | None
) -> tuple[list[Row], WeightSource]:
    """Settle every run's weight for the whole document, and say how it was settled."""
    named = any(glyph.bold is not None for row in all_rows for run in row.runs for glyph in run.glyphs)
    if named:
        return list(all_rows), WeightSource.FONT_NAME
    if ink is None:
        cleared = [Row(tuple(replace(run, bold=False) for run in row.runs), row.page) for row in all_rows]
        return cleared, WeightSource.UNKNOWN
    shares = {
        (row.page, index_row, index_run): ink(run.page, run.x0, run.top, run.x1, run.bottom)
        for index_row, row in enumerate(all_rows)
        for index_run, run in enumerate(row.runs)
    }
    by_size: dict[float, list[float]] = {}
    for index_row, row in enumerate(all_rows):
        for index_run, run in enumerate(row.runs):
            by_size.setdefault(round(run.size), []).append(shares[(row.page, index_row, index_run)])
    medians = {size: statistics.median(values) for size, values in by_size.items()}
    resolved: list[Row] = []
    for index_row, row in enumerate(all_rows):
        runs = []
        for index_run, run in enumerate(row.runs):
            median = medians[round(run.size)]
            share = shares[(row.page, index_row, index_run)]
            runs.append(replace(run, bold=median > 0 and share >= INK_BOLD_RATIO * median))
        resolved.append(Row(tuple(runs), row.page))
    return resolved, WeightSource.GLYPH_DENSITY


def _glyph_marker(run: Run) -> tuple[str, float, tuple[Glyph, ...]]:
    """A marker glyph opening the run: (marker, where the text starts, the text's glyphs)."""
    ink = [glyph for glyph in run.glyphs if not glyph.text.isspace()]
    if len(ink) < 2:
        return "", run.x0, run.glyphs
    first, second = ink[0], ink[1]
    is_marker = first.text in MARKER_GLYPHS or first.text == "o"
    # Separated from the text by a gap a word space wide, or by an explicit space
    # character however narrow: "\u2022 Built" with a tight space is still a bullet.
    spaced = second.x0 - first.x1 > WORD_GAP * first.size or any(
        glyph.text.isspace() and first.x1 - 0.01 <= glyph.x0 <= second.x0 for glyph in run.glyphs
    )
    if is_marker and spaced:
        rest = tuple(glyph for glyph in run.glyphs if glyph is not first and glyph.x0 >= second.x0 - 0.01)
        return first.text, second.x0, rest
    return "", run.x0, run.glyphs


def make_line(
    runs: Sequence[Run],
    *,
    index: int,
    page: int,
    column: int,
    weight_source: WeightSource,
    markers: Sequence[Box] = (),
    rules: Sequence[Box] = (),
) -> LayoutLine:
    """One line from the runs of one row that lie in one column, left to right."""
    ordered = sorted(runs, key=lambda run: run.x0)
    first = ordered[0]
    marker, text_x0, first_glyphs = _glyph_marker(first)
    x0 = first.x0
    top, bottom = min(run.top for run in ordered), max(run.bottom for run in ordered)
    size = _dominant([(run.size, len(run.glyphs)) for run in ordered]) or first.size

    if not marker:
        for shape in markers:
            centre = (shape.top + shape.bottom) / 2
            small = max(shape.x1 - shape.x0, shape.bottom - shape.top) <= _MARKER_MAX_SIZE * size
            beside = 0 <= first.x0 - shape.x1 <= _MARKER_MAX_DISTANCE * size
            if small and beside and top <= centre <= bottom:
                marker, x0 = DRAWN_MARKER, shape.x0
                break

    segments: list[LayoutSegment] = []
    for position, run in enumerate(ordered):
        glyphs = first_glyphs if position == 0 else run.glyphs
        text = _words(glyphs)
        if not text:
            continue
        segments.append(
            LayoutSegment(
                text=text,
                x0=glyphs[0].x0 if glyphs else run.x0,
                x1=run.x1,
                font_size=run.size,
                bold=run.bold,
            )
        )
    x1 = max(run.x1 for run in ordered)
    bold = sum(len(run.glyphs) for run in ordered if run.bold) * 2 > sum(len(run.glyphs) for run in ordered)
    width = x1 - x0
    rule_below = any(
        bottom - 1.0 <= rule.top <= bottom + _RULE_MAX_DISTANCE * size
        and min(rule.x1, x1) - max(rule.x0, x0) >= _RULE_MIN_COVERAGE * width
        for rule in rules
    )
    return LayoutLine(
        index=index,
        page=page,
        column=column,
        x0=x0,
        x1=x1,
        top=top,
        bottom=bottom,
        text_x0=segments[0].x0 if segments else text_x0,
        text=" ".join(segment.text for segment in segments),
        marker=marker,
        font_size=size,
        bold=bold,
        weight_source=weight_source,
        segments=tuple(segments),
        rule_below=rule_below,
    )
