"""Does an independent extractor read the same lines in the same order? (RESUME_ENGINE §5.6)

pypdf reads the content stream in the order the generator wrote it. Each line of the
geometric reconstruction is looked for in pypdf's text; the readings disagree when too
few lines keep their relative order, or when text is present in one reading and
missing from the other.

Order, not a token multiset, because a multiset cannot see order: measured on the
fixtures, float_scrambled.pdf — whose entry headers are written ahead of their bodies —
differs from a correct reading by three tokens, exactly as much as markerless_bullets
.pdf, whose order is fine.

**The result is agree or disagree, never a number.** Agreement is not evidence of
correctness: two extractors reading the same wrong content stream agree perfectly,
which is precisely what reported "agreement 100%" over nine fabricated roles. The
measurements behind the verdict stay inside this module.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence

from aria_core.documents import CrossExtractor

__all__ = ["MAX_MISSING_SHARE", "MIN_ORDERED_SHARE", "compare"]

#: Q1 (DECISIONS 2026-10-11): at least this share of the lines found in both readings
#: must keep their relative order.
MIN_ORDERED_SHARE = 0.95
#: Text in one reading and not the other, as a share of its characters, beyond which
#: the readings disagree about what the page says rather than about its order.
MAX_MISSING_SHARE = 0.05
#: Lines shorter than this, once reduced to letters and digits, are not looked for:
#: "OR" or "2024" occurs in too many places to say where a line is.
_MIN_LINE_LENGTH = 6


def _normal(text: str) -> str:
    return "".join(character for character in text.lower() if character.isalnum())


def _longest_ordered(positions: Sequence[int]) -> int:
    """Length of the longest strictly increasing subsequence."""
    tails: list[int] = []
    for position in positions:
        at = bisect.bisect_left(tails, position)
        if at == len(tails):
            tails.append(position)
        else:
            tails[at] = position
    return len(tails)


def compare(lines: Sequence[str], other_reading: str) -> CrossExtractor:
    """``lines`` in reading order against another extractor's text of the same document."""
    haystack = _normal(other_reading)
    positions: list[int] = []
    missing = 0
    found_characters = 0
    cursor = 0
    for line in lines:
        needle = _normal(line)
        if len(needle) < _MIN_LINE_LENGTH:
            continue
        # Look ahead of the last match first, so a line that repeats ("Pune, India")
        # is matched where the reading order expects it; fall back to anywhere.
        at = haystack.find(needle, cursor)
        if at == -1:
            at = haystack.find(needle)
        if at == -1:
            missing += len(needle)
            continue
        positions.append(at)
        found_characters += len(needle)
        cursor = at + len(needle)

    ours = sum(len(_normal(line)) for line in lines)
    if not positions:
        return CrossExtractor.DISAGREE if ours or haystack else CrossExtractor.AGREE
    ordered_share = _longest_ordered(positions) / len(positions)
    missing_from_theirs = missing / max(ours, 1)
    missing_from_ours = max(0, len(haystack) - ours) / max(len(haystack), 1)
    if ordered_share < MIN_ORDERED_SHARE:
        return CrossExtractor.DISAGREE
    if missing_from_theirs > MAX_MISSING_SHARE or missing_from_ours > MAX_MISSING_SHARE:
        return CrossExtractor.DISAGREE
    return CrossExtractor.AGREE
