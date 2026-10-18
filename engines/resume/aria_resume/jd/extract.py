"""What rules read reliably from a posting: years, degrees, location, work mode, and the
sentences about sponsorship and citizenship, quoted as written.

Each rule reads only what the posting states. A posting that mentions both remote and
on-site work has no work mode the rules can name, so it is ``unknown``; a first line that
reads as a sentence carries no seniority. The model step reads what the rules leave.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from aria_resume.jd.sections import Item
from aria_resume.schemas import JDSection, Seniority, WorkMode

__all__ = [
    "YearsMention",
    "citizenship_sentences",
    "degree_lines",
    "general_years",
    "location",
    "seniority",
    "sponsorship_sentences",
    "work_mode",
    "years_mentions",
]

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_NUMBER = r"(?:\d{1,2}|" + "|".join(_NUMBER_WORDS) + ")"
#: "3+ years", "2-4 years", "two years", "5 or more years".
_YEARS = re.compile(
    rf"(?<![\w.])(?P<low>{_NUMBER})\s*(?:\+|plus|or more)?\s*"
    rf"(?:(?:-|\u2013|to)\s*{_NUMBER}\s*)?\+?\s*years?\b",
    re.IGNORECASE,
)
#: "years of experience", "years of professional software engineering experience": the
#: general floor. A skill named here is what the experience was in, not what the years
#: are of.
_GENERAL = re.compile(r"^\s*(?:of\s+)?(?:\w[\w-]*\s+){0,3}experience\b", re.IGNORECASE)


class YearsMention:
    __slots__ = ("end", "general", "low", "start")

    def __init__(self, low: int, start: int, end: int, general: bool) -> None:
        self.low, self.start, self.end, self.general = low, start, end, general


def _value(raw: str) -> int:
    return _NUMBER_WORDS.get(raw.lower()) or int(raw)


def years_mentions(text: str) -> list[YearsMention]:
    return [
        YearsMention(
            _value(match.group("low")), match.start(), match.end(), bool(_GENERAL.match(text[match.end() :]))
        )
        for match in _YEARS.finditer(text)
    ]


def general_years(items: Sequence[Item]) -> int | None:
    """The general experience floor the required lines state; the highest if several do."""
    lows = [
        mention.low
        for item in items
        if item.section is JDSection.REQUIRED
        for mention in years_mentions(item.text)
        if mention.general
    ]
    return max(lows) if lows else None


_DEGREE = re.compile(
    r"\b(?:B\.?S\.?|B\.?A\.?|BSc|BEng|M\.?S\.?|MSc|MEng|Ph\.?D\.?|bachelor'?s|master'?s|doctorate|"
    r"(?:undergraduate|graduate|college|university|four-year)\s+degree|degree\s+in)\b",
    re.IGNORECASE,
)


def degree_lines(items: Sequence[Item]) -> tuple[str, ...]:
    return tuple(
        item.text
        for item in items
        if item.section not in (JDSection.ABOUT, JDSection.BENEFITS)
        and _DEGREE.search(item.text.replace("\u2019", "'"))
    )


_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
    "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
).split()
_LOCATION_LINE = re.compile(
    r"^\s*(?:location|locations|based in|office)\s*:\s*(?P<where>.+?)\s*$", re.IGNORECASE
)
_CITY_STATE = re.compile(r"\b(?P<where>[A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2},\s(?:" + "|".join(_STATES) + r"))\b")


def location(text: str) -> str | None:
    """A "Location:" line, as written; else the first "City, ST" the posting names."""
    for line in text.split("\n"):
        match = _LOCATION_LINE.match(line)
        if match:
            return match.group("where")
    match = _CITY_STATE.search(text)
    return match.group("where") if match else None


_MODES = {
    WorkMode.HYBRID: re.compile(r"\bhybrid\b", re.IGNORECASE),
    WorkMode.REMOTE: re.compile(r"\bremote\b", re.IGNORECASE),
    WorkMode.ONSITE: re.compile(r"\b(?:on-?site|in[- ]office|in[- ]person)\b", re.IGNORECASE),
}


def work_mode(text: str) -> WorkMode:
    """Hybrid when the posting says hybrid (it usually names office days too); remote or
    on-site when it names only that one; unknown when it names both or neither."""
    named = {mode for mode, pattern in _MODES.items() if pattern.search(text)}
    if WorkMode.HYBRID in named:
        return WorkMode.HYBRID
    if len(named) == 1:
        return next(iter(named))
    return WorkMode.UNKNOWN


_SPONSORSHIP = re.compile(
    r"\b(?:sponsor\w*|visas?|h-?1b|work authori[sz]ation|authori[sz]ed to work|"
    r"employment authori[sz]ation)\b",
    re.IGNORECASE,
)
_CITIZENSHIP = re.compile(
    r"\b(?:security clearance|clearance|u\.?s\.? citizen\w*|citizenship|green card|permanent resident|itar|"
    r"export control\w*)\b",
    re.IGNORECASE,
)


def _quoted(items: Sequence[Item], pattern: re.Pattern[str]) -> str | None:
    found = [item.text for item in items if pattern.search(item.text)]
    return " ".join(found) if found else None


def sponsorship_sentences(items: Sequence[Item]) -> str | None:
    return _quoted([item for item in items if not _CITIZENSHIP.search(item.text)], _SPONSORSHIP)


def citizenship_sentences(items: Sequence[Item]) -> str | None:
    return _quoted(items, _CITIZENSHIP)


_SENIORITY = (
    (Seniority.INTERN, re.compile(r"\bintern(?:ship)?\b", re.IGNORECASE)),
    (
        Seniority.NEW_GRAD,
        re.compile(r"\b(?:new\s+grad(?:uate)?|entry[- ]level|early career)\b", re.IGNORECASE),
    ),
    (Seniority.STAFF_PLUS, re.compile(r"\b(?:staff|principal|distinguished)\b", re.IGNORECASE)),
    (Seniority.SENIOR, re.compile(r"\b(?:senior|sr\.?)(?=\s|$)", re.IGNORECASE)),
    (Seniority.JUNIOR, re.compile(r"\b(?:junior|jr\.?)(?=\s|$)", re.IGNORECASE)),
    (Seniority.MID, re.compile(r"\bmid[- ]level\b", re.IGNORECASE)),
)


def seniority(text: str) -> Seniority:
    """From the first line, when it reads as a job title: short, and not a sentence."""
    first = next((line.strip() for line in text.split("\n") if line.strip()), "")
    if not first or len(first.split()) > 10 or first.endswith((".", "!", "?")):
        return Seniority.UNKNOWN
    for level, pattern in _SENIORITY:
        if pattern.search(first):
            return level
    return Seniority.UNKNOWN
