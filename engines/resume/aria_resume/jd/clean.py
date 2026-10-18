"""The posting as a person would read it: what cannot be seen is removed and counted.

Zero-width characters, bidirectional overrides and other format characters let a page
show one text and hand a parser another: "Python" can hide a word between its letters,
and an override can reverse an instruction a reviewer never sees. They are removed
before anything reads the text, and their count is reported as a suspicious signal,
because a hiring page has no reason to contain them.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

__all__ = ["MAX_CHARACTERS", "CleanText", "clean"]

#: Longer than any posting worth reading; a page this long is cut, and the cut is reported.
MAX_CHARACTERS = 40_000


@dataclass(frozen=True, slots=True)
class CleanText:
    text: str
    invisible: int
    truncated: bool


def _invisible(character: str) -> bool:
    # Cf: format characters (zero-width joiners, bidi controls, soft hyphens). Control
    # characters other than line and tab breaks go too.
    category = unicodedata.category(character)
    return category == "Cf" or (category == "Cc" and character not in "\n\t")


def clean(raw: str) -> CleanText:
    text = unicodedata.normalize("NFKC", raw.replace("\r\n", "\n").replace("\r", "\n"))
    kept = [character for character in text if not _invisible(character)]
    invisible = len(text) - len(kept)
    text = "".join(kept).replace("\t", " ")
    truncated = len(text) > MAX_CHARACTERS
    return CleanText(text[:MAX_CHARACTERS], invisible, truncated)
