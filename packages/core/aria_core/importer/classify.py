"""What each line of a resume is, from how it looks on the page (RESUME_ENGINE §5.3).

Classification uses geometry first — size, weight, position, markers, rules — and
word lists only ever *raise* confidence, never decide alone, so "WORK HISTORY",
"Professional Experience" and a heading in another language all resolve.

- **SECTION_HEADER** — larger than body text or bold, short, and at least one of:
  all capitals, a rule drawn under it, a known heading.
- **ENTRY_HEADER** — bold where it starts (the employer, school or project name),
  and either holding a separator (an em or en dash, a bar, a comma) or carrying a date
  in the right-hand band.
- **BULLET** — opens with a marker, typed or drawn (line building found both).
- **CONTINUATION** — the wrapped tail of the bullet above: no marker, starting where
  that bullet's text starts, close beneath it. This is the hanging-indent rule, and
  it is what keeps "mobile" from becoming an entry.
- **NAME** and **CONTACT** — the opening lines: the name set larger, then lines with
  an email, a phone number or a profile link.
- **TEXT** — everything else: a location under a header, a "Client:" line, coursework,
  a skills line, a summary.

**Dates come only from the right-hand band** of an entry header (x0 past 55% of the
content width) or from its trailing segment — never pattern-matched out of the middle
of a line. A heading whose only claim to being one is its weight is marked less
certain when that weight was measured from ink rather than named by the font.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from aria_core.documents import LayoutDocument, LayoutLine, WeightSource
from aria_core.importer.dates import parse_date_range
from aria_core.importer.sections import SectionKind, section_for_heading

__all__ = [
    "DATE_BAND",
    "HEADER_SIZE_RATIO",
    "ClassifiedLine",
    "LineClass",
    "classify",
]

#: §5.3: a section heading is set at least this much larger than body text, or bold.
HEADER_SIZE_RATIO = 1.08
#: §5.3: a heading is short.
_MAX_HEADING_CHARACTERS = 40
#: §5.3: the right-hand band where an entry's dates sit, as a share of content width.
DATE_BAND = 0.55
#: The name is set noticeably larger than body text.
_NAME_SIZE_RATIO = 1.3
#: How many opening lines may hold the name and contact details.
_OPENING_LINES = 5
#: A continuation starts within this many font sizes of its bullet's text, and sits
#: within this many font sizes below the line above it.
_CONTINUATION_ALIGN = 0.6
_CONTINUATION_GAP = 1.0
#: A hanging indent: wrapped lines start this many font sizes right of the first.
_HANGING_MIN = 0.8
_HANGING_MAX = 3.0
#: Two line starts are the same x position within this many font sizes. Tighter than
#: _CONTINUATION_ALIGN: a bullet column 4pt right of the entry's edge must not merge
#: with the edge itself.
_SAME_X = 0.2
#: A date segment is short: "Feb 2024 - Jul 2025", "Expected Dec 2026".
_MAX_DATE_WORDS = 6

_SEPARATORS = ("\u2014", "\u2013", "|", ",", " - ")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\(?\+?\d[\d\s().\-]{6,18}\d")
_LINK = re.compile(r"\b(linkedin\.com|github\.com|gitlab\.com|https?://)", re.IGNORECASE)


class LineClass(StrEnum):
    NAME = "name"
    CONTACT = "contact"
    SECTION_HEADER = "section_header"
    ENTRY_HEADER = "entry_header"
    BULLET = "bullet"
    CONTINUATION = "continuation"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class ClassifiedLine:
    line: LayoutLine
    kind: LineClass
    #: For a section header: the section its words name, or UNKNOWN.
    section: SectionKind | None = None
    #: For an entry header: the date text found in the right-hand band, if any.
    date_text: str = ""
    #: False when the decision leaned on a weight measured from ink, not named.
    certain: bool = True


def _body_size(lines: Sequence[LayoutLine]) -> float:
    sizes = Counter(round(line.font_size, 1) for line in lines)
    return max(sizes, key=lambda size: (sizes[size], -size)) if sizes else 0.0


def _content_band(lines: Sequence[LayoutLine]) -> dict[tuple[int, int], float]:
    """Where the right-hand date band starts, per page and column."""
    edges: dict[tuple[int, int], tuple[float, float]] = {}
    for line in lines:
        key = (line.page, line.column)
        low, high = edges.get(key, (line.x0, line.x1))
        edges[key] = (min(low, line.x0), max(high, line.x1))
    return {key: low + DATE_BAND * (high - low) for key, (low, high) in edges.items()}


def _is_date(text: str) -> bool:
    return len(text.split()) <= _MAX_DATE_WORDS and parse_date_range(text) is not None


def _all_capitals(text: str) -> bool:
    letters = [character for character in text if character.isalpha()]
    return len(letters) > 2 and all(character.isupper() for character in letters)


def _section_header(line: LayoutLine, body: float) -> tuple[bool, SectionKind | None, bool]:
    if line.marker or len(line.text) > _MAX_HEADING_CHARACTERS or any(c.isdigit() for c in line.text):
        return False, None, True
    larger = line.font_size > body * HEADER_SIZE_RATIO
    if not (larger or line.bold):
        return False, None, True
    known = section_for_heading(line.text)
    if not (_all_capitals(line.text) or line.rule_below or known is not None):
        return False, None, True
    # Weight was the only thing setting it apart, and that weight was measured.
    certain = larger or line.weight_source is WeightSource.FONT_NAME
    return True, known or SectionKind.UNKNOWN, certain


def _entry_header(line: LayoutLine, band: float) -> tuple[bool, str, bool]:
    if line.marker or not line.segments:
        return False, "", True
    first = line.segments[0]
    if not (first.bold or line.bold):
        return False, "", True
    date_text = ""
    trailing = line.segments[-1]
    if len(line.segments) > 1 and trailing.x0 >= band and _is_date(trailing.text):
        date_text = trailing.text
    separated = any(separator in first.text for separator in _SEPARATORS)
    if not (separated or date_text):
        return False, "", True
    return True, date_text, line.weight_source is not WeightSource.GLYPH_DENSITY


def classify(document: LayoutDocument) -> list[ClassifiedLine]:
    """Every line of the document, in reading order, with what it is."""
    lines = list(document.lines)
    body = _body_size(lines)
    bands = _content_band(lines)
    result: list[ClassifiedLine] = []
    named = False
    in_opening = True
    for position, line in enumerate(lines):
        previous = result[-1] if result else None
        is_opening = in_opening and position < _OPENING_LINES

        if is_opening and not named and position == 0 and line.font_size >= body * _NAME_SIZE_RATIO:
            result.append(ClassifiedLine(line, LineClass.NAME))
            named = True
            continue
        if is_opening and (_EMAIL.search(line.text) or _PHONE.search(line.text) or _LINK.search(line.text)):
            result.append(ClassifiedLine(line, LineClass.CONTACT))
            continue

        heading, section, certain = _section_header(line, body)
        if heading:
            in_opening = False
            result.append(ClassifiedLine(line, LineClass.SECTION_HEADER, section=section, certain=certain))
            continue
        in_opening = in_opening and not line.marker

        if line.marker:
            result.append(ClassifiedLine(line, LineClass.BULLET))
            continue

        if previous is not None and previous.kind in (LineClass.BULLET, LineClass.CONTINUATION):
            anchor = previous.line.text_x0
            same_place = previous.line.page == line.page and previous.line.column == line.column
            aligned = abs(line.x0 - anchor) <= _CONTINUATION_ALIGN * line.font_size
            close = 0 <= line.top - previous.line.bottom <= _CONTINUATION_GAP * line.font_size
            if same_place and aligned and close:
                result.append(ClassifiedLine(line, LineClass.CONTINUATION))
                continue

        header, date_text, certain = _entry_header(line, bands[(line.page, line.column)])
        if header:
            result.append(ClassifiedLine(line, LineClass.ENTRY_HEADER, date_text=date_text, certain=certain))
            continue
        result.append(ClassifiedLine(line, LineClass.TEXT))
    return _hanging_indents(result)


def _hanging_indents(lines: list[ClassifiedLine]) -> list[ClassifiedLine]:
    """Bullets with no marker at all, told apart by their indent (§5.3).

    Inside an entry, a plain line followed closely by lines indented a little to its
    right is a bullet whose text wraps: x_marker for the first line, x_text for the
    rest. Restricted to lines under an entry header and not bold, so a location or a
    header's second line is never read as a bullet.

    A wrap proves where the column's x_marker is, and §5.3 makes every line starting
    there a new bullet, wrapped or not: a one-line bullet at x_marker is a bullet, in
    an entry where nothing wraps as much as in one where something does. That second
    pass needs x_marker clearly right of the entry header's own edge. Where bullets sit
    flush with the header, geometry cannot tell them from a location line, and they
    stay TEXT for assembly to report (RESUME_ENGINE §5.9 measures that case).
    """
    result = list(lines)
    markers: dict[tuple[int, int], list[float]] = {}
    for position, following in _wrapped_runs(result):
        current = result[position]
        result[position] = ClassifiedLine(current.line, LineClass.BULLET)
        for index in following:
            result[index] = ClassifiedLine(result[index].line, LineClass.CONTINUATION)
        markers.setdefault((current.line.page, current.line.column), []).append(current.line.x0)

    header_x: float | None = None
    for position, current in enumerate(result):
        if current.kind is LineClass.SECTION_HEADER:
            header_x = None
        elif current.kind is LineClass.ENTRY_HEADER:
            header_x = current.line.x0
        if header_x is None or current.kind is not LineClass.TEXT or current.line.bold:
            continue
        line = current.line
        tolerance = _SAME_X * line.font_size
        at_marker = any(abs(line.x0 - x) <= tolerance for x in markers.get((line.page, line.column), ()))
        if at_marker and line.x0 - header_x > tolerance:
            result[position] = ClassifiedLine(line, LineClass.BULLET)
    return result


def _wrapped_runs(lines: list[ClassifiedLine]) -> list[tuple[int, list[int]]]:
    """Each plain line inside an entry that wraps onto indented lines, with those lines."""
    runs: list[tuple[int, list[int]]] = []
    in_entry = False
    position = 0
    while position < len(lines):
        current = lines[position]
        if current.kind is LineClass.SECTION_HEADER:
            in_entry = False
        elif current.kind is LineClass.ENTRY_HEADER:
            in_entry = True
        if not (in_entry and current.kind is LineClass.TEXT and not current.line.bold):
            position += 1
            continue
        following = position + 1
        wrapped: list[int] = []
        indent: float | None = None
        while following < len(lines) and lines[following].kind is LineClass.TEXT:
            previous_line, line = lines[following - 1].line, lines[following].line
            step = line.x0 - current.line.x0
            size = current.line.font_size
            close = 0 <= line.top - previous_line.bottom <= _CONTINUATION_GAP * size
            indented = _HANGING_MIN * size <= step <= _HANGING_MAX * size
            if not (
                close and indented and (indent is None or abs(line.x0 - indent) <= _CONTINUATION_ALIGN * size)
            ):
                break
            indent = line.x0 if indent is None else indent
            wrapped.append(following)
            following += 1
        if wrapped:
            runs.append((position, wrapped))
        position = following if wrapped else position + 1
    return runs
