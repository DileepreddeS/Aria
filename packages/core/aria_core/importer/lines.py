"""Extracted text as numbered lines.

Every proposal the importer makes carries the line it came from, because the approval
view shows the candidate the line of their own resume behind each draft fact
(``docs/DECISIONS.md``: "Approving a fact is a truth claim, so the candidate sees what
it came from"). That only works if line numbers refer to the text stored on the upload
row, so **nothing here renumbers or reorders**: blank lines keep their place and a
dropped line still advances the count.

Bullet glyphs are recognised and stripped, and the fact that a line *was* a bullet is
kept. It matters: in a resume, a bullet is a claim about an achievement and a
non-bullet line is usually a heading, an employer or a date. Guessing that from
indentation alone does not survive PDF extraction, which loses leading whitespace
about half the time.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from aria_core.documents import split_links_block

__all__ = ["ResumeText", "SourceLine", "read_lines"]

#: Glyphs a resume uses to start a bullet. The ASCII hyphen is here and the result is
#: a little lossy — a line genuinely starting with a hyphen is rare, and treating a
#: real bullet as a heading is the more damaging mistake, because a heading's text is
#: not imported as a fact.
_BULLET_GLYPHS = "\u2022\u00b7\u25aa\u25ab\u2023\u2043\u25cf\u25e6\u2219*\u2013\u2014-\u00ad\u2212"

#: Unicode categories to drop outright: format characters (soft hyphens, bidi marks,
#: zero-width joiners) that PDF extraction leaves behind and that make an otherwise
#: identical line compare unequal.
_DROPPED_CATEGORIES = frozenset({"Cf"})


def _clean(line: str) -> str:
    """Normalise one line without changing what it says.

    NFKC folds the ligatures and full-width forms that PDF fonts produce, so "ﬁnance"
    and "finance" are one word. Non-breaking spaces become ordinary ones. Tabs become
    single spaces rather than being expanded, because the original column positions
    did not survive extraction anyway.
    """
    folded = unicodedata.normalize("NFKC", line)
    kept = "".join(
        " " if character in "\t\u00a0\u2007\u202f" else character
        for character in folded
        if unicodedata.category(character) not in _DROPPED_CATEGORIES
    )
    return " ".join(kept.split())


@dataclass(frozen=True, slots=True)
class SourceLine:
    """One line of an upload's extracted text.

    ``number`` is 1-based and indexes the stored text, so it can be shown to the
    candidate and still mean something a year later.
    """

    number: int
    #: Cleaned, with any bullet glyph removed. What gets imported.
    text: str
    #: Exactly as extracted. Kept so the approval view can quote the original.
    raw: str
    #: The line began with a bullet glyph.
    bullet: bool

    @property
    def blank(self) -> bool:
        return not self.text

    @property
    def words(self) -> list[str]:
        return self.text.split()


def _split_bullet(text: str) -> tuple[str, bool]:
    """Strip a leading bullet glyph. Returns ``(text, was_a_bullet)``."""
    stripped = text.lstrip()
    if not stripped or stripped[0] not in _BULLET_GLYPHS:
        return text, False

    remainder = stripped[1:].lstrip()
    # A glyph with nothing after it is a rule or a separator, not a bullet, and a
    # bullet followed immediately by another glyph ("--") is almost certainly a rule.
    if not remainder or remainder[0] in _BULLET_GLYPHS:
        return text, False
    return remainder, True


@dataclass(frozen=True, slots=True)
class ResumeText:
    """An upload's extracted text, ready to parse.

    ``links`` comes from the block the document parser appends; the URLs are data and
    are never fetched. They are separated here so a URL cannot be read as a bullet,
    which is the whole reason the parser labels the block.
    """

    lines: tuple[SourceLine, ...]
    links: tuple[str, ...]

    @property
    def content(self) -> tuple[SourceLine, ...]:
        """The non-blank lines, in order."""
        return tuple(line for line in self.lines if not line.blank)

    def __len__(self) -> int:
        return len(self.lines)


def read_lines(text: str) -> ResumeText:
    """Split extracted text into numbered lines and the link block."""
    body, links = split_links_block(text)
    lines: list[SourceLine] = []
    for number, raw in enumerate(body.splitlines(), start=1):
        cleaned = _clean(raw)
        content, bullet = _split_bullet(cleaned)
        lines.append(SourceLine(number=number, text=content.strip(), raw=raw, bullet=bullet))
    return ResumeText(lines=tuple(lines), links=links)
