"""How much of the page a plan takes, estimated before anything is printed (RESUME_ENGINE §8 5a).

Heights follow resume.css: US Letter with 0.6in margins (a 525.6 by 705.6pt content box),
10.5pt body text at 1.2 line height, the name at 14pt, 6pt above each section, 3pt above
each entry, a heading with its rule, bullets with an 11pt padding and an 8pt hanging indent.
Text is wrapped word by word with Carlito's own glyph widths (:mod:`.metrics`).

It is an estimate: Chromium kerns, and rounds line boxes its own way. So the width is taken
at 97% and a line of slack is kept at the foot of the page, and the printed page is what
decides in the end: 6d's one-page check, after rendering.
"""

from __future__ import annotations

from dataclasses import dataclass

from aria_resume.compose.metrics import carlito

__all__ = ["PAGE", "Page", "bullet_lines", "plain_lines", "wrapped_lines"]


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
    #: Share of the measured width a line may fill, for kerning and rounding.
    width_margin: float = 0.97
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
