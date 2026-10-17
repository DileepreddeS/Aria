"""Finding claims in a line of resume text: numbers, number words, months, phrases.

Shared by the truth check (6b), which requires each claim to be in the cited facts, and
the recruiter check (6c), which counts bullets that carry a number. Both must see the
same numbers, or a bullet could count as quantified by a number the truth check never
looked at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "NumberMention",
    "has_phrase",
    "month_mentions",
    "number_mentions",
    "number_words",
    "phrase_spans",
    "words",
]

#: A quantity: digits with an optional currency sign, decimals or thousands, unit and
#: plus. Not a digit inside a name ("p95", "S3", "EC2", "v2"): those are not claims of
#: quantity, and the lookbehind leaves them alone. A comparison sign in front ("~30%",
#: "<5 ms") is part of the claim.
_NUMBER = re.compile(
    r"(?<![\w.])"
    r"(?P<sign>[~<>\u2248\u2264\u2265])?"
    r"(?P<value>[$\u00a3\u20ac]?\d+(?:[.,]\d+)*(?:\s?(?:%|ms|GB|MB|TB)|[xXKMBk])?\+?)"
    r"(?!\w|\.\d)"
)

#: Words that change what a number claims when they stand in front of it.
_QUALIFIERS = (
    "more than",
    "less than",
    "up to",
    "at least",
    "close to",
    "about",
    "around",
    "approximately",
    "roughly",
    "nearly",
    "almost",
    "over",
    "under",
)

#: Quantities written as words. "One" is left out: it is far more often "one of the"
#: than a count, and a count of one is not an inflation anyone gains from.
NUMBER_WORDS = frozenset(
    "two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty "
    "ninety hundred hundreds thousand thousands million millions billion billions dozen "
    "dozens double doubled doubling triple tripled tripling quadrupled twice halved "
    "tenfold".split()
)

#: Word values for a years claim ("three years").
WORD_VALUES = {
    word: index
    for index, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "sept": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
#: Capitalised only: "may" in a sentence is a verb, "May" a month.
_MONTH = re.compile(
    r"(?<![\w-])("
    + "|".join(sorted((name.capitalize() for name in _MONTHS), key=len, reverse=True))
    + r")\.?(?![\w-])"
)


@dataclass(frozen=True, slots=True)
class NumberMention:
    #: The number as written, spaces removed: "30%", "15+", "200ms".
    bare: str
    #: With what qualifies it, when something does: "~30%", "over 30%". None otherwise.
    qualified: str | None
    start: int
    end: int


def number_mentions(text: str) -> list[NumberMention]:
    found: list[NumberMention] = []
    for match in _NUMBER.finditer(text):
        bare = "".join(match.group("value").split())
        qualified = None
        if match.group("sign"):
            qualified = match.group("sign") + bare
        else:
            before = text[: match.start()].rstrip().lower()
            for word in _QUALIFIERS:
                if re.search(rf"(?<![\w-]){re.escape(word)}$", before):
                    qualified = f"{word} {bare}"
                    break
        found.append(NumberMention(bare, qualified, match.start(), match.end()))
    return found


def words(text: str) -> list[str]:
    """Lowercase words, hyphenated words kept whole."""
    return re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", text.lower())


def number_words(text: str) -> list[str]:
    return [word for word in words(text) if word in NUMBER_WORDS]


def month_mentions(text: str) -> list[int]:
    """The months the text names, as numbers."""
    return [_MONTHS[match.group(1).lower()] for match in _MONTH.finditer(text)]


def phrase_spans(text: str, phrase: str) -> list[tuple[int, int]]:
    """Where ``phrase`` occurs in ``text`` as whole words, ignoring case and spacing."""
    parts = [re.escape(part) for part in phrase.split()]
    if not parts:
        return []
    pattern = re.compile(r"(?<![\w-])" + r"\s+".join(parts) + r"(?![\w-])", re.IGNORECASE)
    return [(match.start(), match.end()) for match in pattern.finditer(text)]


def has_phrase(text: str, phrase: str) -> bool:
    return bool(phrase_spans(text, phrase))
