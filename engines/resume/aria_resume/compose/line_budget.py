"""How much of the page a plan takes, estimated before anything is printed (RESUME_ENGINE §8 5a).

Heights follow resume.css: US Letter with 0.6in margins (a 525.6 by 705.6pt content box),
10.5pt body text at 1.2 line height, the name at 14pt, 6pt above each section, 3pt above
each entry, a heading with its rule, bullets with an 11pt padding and an 8pt hanging indent.
Text is wrapped word by word with Carlito's own glyph widths (:mod:`.metrics`).

It is an estimate: Chromium kerns, and sets text a little narrower than the font's advance
widths, by an amount that differs by platform. Measured against the print
(evals/line_budget/measure.py, DECISIONS 2026-10-29), a full line prints at 0.981 of its
advance width on Linux, where production runs, and at 0.995 on Windows. The width margin is
calibrated on Linux, a line of slack is kept at the foot of the page, and the printed page
is what decides in the end: 6d's one-page check, after rendering.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aria_resume.compose.metrics import carlito
from aria_resume.document import CONTACT_SEPARATOR

__all__ = [
    "PAGE",
    "Page",
    "bullet_lines",
    "contact_lines",
    "contact_rows",
    "fits_a_line",
    "plain_lines",
    "wrapped_lines",
]


@dataclass(frozen=True, slots=True)
class Page:
    width: float = 525.6
    height: float = 705.6
    size: float = 10.5
    line: float = 10.5 * 1.2
    name: float = 14 * 1.2
    contact_gap: float = 4.0
    section_gap: float = 6.0
    heading: float = 10.5 * 1.2 + 0.75 + 2.0
    entry_gap: float = 3.0
    bullet_padding: float = 11.0
    bullet_hang: float = 8.0
    #: How much of a line, in advance widths, a line may fill. Above 1 because Chromium sets
    #: text narrower than its advance widths. Calibrated on Linux over 1,200 bullets: no
    #: bullet estimated short up to 1.010 (the first at 1.015), so 1.005 keeps half a percent
    #: in hand; also none short on Windows. Was 0.97, a guess that left a line unused on one
    #: bullet in ten on Linux.
    width_margin: float = 1.005
    #: Lines held back at the foot of the page.
    slack_lines: int = 1

    @property
    def usable_height(self) -> float:
        return self.height - self.slack_lines * self.line


PAGE = Page()


def wrapped_lines(
    text: str, width: float, *, size: float = PAGE.size, bold: bool = False, first_line_extra: float = 0.0
) -> int:
    """Lines ``text`` wraps to in ``width`` points; the first line may be ``first_line_extra`` wider."""
    metrics = carlito("Bold" if bold else "Regular")
    space = metrics.width(" ", size)
    lines, used, available = 1, 0.0, width + first_line_extra
    for word in text.split():
        span = metrics.width(word, size)
        needed = span if used == 0 else used + space + span
        if needed <= available or used == 0:
            used = needed
            continue
        lines, used, available = lines + 1, span, width
    return lines


def bullet_lines(text: str, page: Page = PAGE) -> int:
    width = (page.width - page.bullet_padding) * page.width_margin
    # The bullet character and a no-break space sit in the hanging indent's room.
    return wrapped_lines("\u2022\u00a0" + text, width, first_line_extra=page.bullet_hang)


def plain_lines(text: str, page: Page = PAGE) -> int:
    return wrapped_lines(text, page.width * page.width_margin)


def fits_a_line(text: str, page: Page = PAGE) -> bool:
    """Whether ``text`` prints on one line on every platform: its full advance width fits.

    No width margin: Chromium sets a line at 0.981 (Linux) to 0.995 (Windows) of its advance
    width, so a line that fits at full width fits on both. A contact line that does not fit is
    not a line of room lost, as a bullet's is, but a link broken in two.
    """
    return carlito("Regular").width(text, page.size) <= page.width


def contact_rows(items: Sequence[str], page: Page = PAGE) -> tuple[tuple[str, ...], ...]:
    """The contact items in printed lines, broken only between items (DECISIONS 2026-10-29).

    Filled from the left, so the profile links, which come last, end the last line. An item
    wider than a line on its own takes a line of its own, whole: it is never broken, and the
    layout check (6d) reports it, so the page goes to a person.
    """
    rows: list[list[str]] = []
    for item in items:
        if rows and fits_a_line(CONTACT_SEPARATOR.join([*rows[-1], item]), page):
            rows[-1].append(item)
        else:
            rows.append([item])
    return tuple(tuple(row) for row in rows)


def contact_lines(items: Sequence[str], page: Page = PAGE) -> tuple[str, ...]:
    return tuple(CONTACT_SEPARATOR.join(row) for row in contact_rows(items, page))
