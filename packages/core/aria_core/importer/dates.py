"""Reading dates off a resume, and refusing to invent the ones that are not there.

Resumes write dates a dozen ways and some of those ways do not carry a month:
"Summer 2023", "2021-2023", "Fall 2022". A parser that turns "Summer 2023" into
2023-06 has made up a fact, and every month figure downstream inherits it —
:mod:`aria_core.graph.months` counts to the month precisely so that the number can be
defended, which is worthless if the inputs were guessed.

So this module reports **what the resume said**, at the precision it said it. A range
is ``usable`` only when it has a real start month and a real end (a month, or
"Present"). Anything else becomes a question for the candidate, which is the "ask,
don't guess" rule applied to the one field they can always answer from memory.

Two more refusals worth naming:

* An end before its start is a typo, not a negative span. It is reported, not silently
  swapped — swapping would produce a confident span the resume never claimed.
* A year outside living memory, or in the future, is not a date. "2051" is a typo.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum

from aria_core.graph.months import MonthSpan
from aria_core.schemas.base import utc_now

__all__ = ["DatePrecision", "DateRange", "ParsedDate", "parse_date_range", "parse_month"]

#: The earliest year a working life plausibly starts. Below this, it is a typo or a
#: number that is not a date at all.
EARLIEST_YEAR = 1950

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip

_SEASONS = frozenset({"spring", "summer", "fall", "autumn", "winter"})

_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_SEASON_NAMES = "|".join(sorted(_SEASONS))

#: Words a resume uses for "still there". "to date" is handled by the separator, so
#: "date" appears here on its own.
_PRESENT_WORDS = r"present|current(?:ly)?|now|ongoing|date|today"

#: Ordered alternation: the longest, most specific forms first, so the year inside
#: "Jun 2024" is consumed by the month-year branch instead of matching as a bare year.
_TOKEN = re.compile(
    r"""
    (?P<present>\b(?:"""
    + _PRESENT_WORDS
    + r""")\b)
  | (?P<month_year>\b(?P<mname>"""
    + _MONTH_NAMES
    + r""")\.?,?\s+(?P<myear>\d{4})\b)
  | (?P<year_month>\b(?P<ymy>\d{4})\s*[/\-]\s*(?P<ymm>0?[1-9]|1[0-2])\b)
  | (?P<month_num>\b(?P<mnm>0?[1-9]|1[0-2])\s*[/\-]\s*(?P<mny>\d{4})\b)
  | (?P<season>\b(?P<sname>"""
    + _SEASON_NAMES
    + r""")\s+(?P<syear>\d{4})\b)
  | (?P<year>\b(?P<yonly>\d{4})\b)
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: What may sit between the two ends of a range, and nothing else. A comma is not
#: here: "Jun 2024, Aug 2024" is a list, not a range.
_SEPARATOR = re.compile(r"^\s*(?:[-\u2013\u2014\u2212]+|to|until|through|thru|\u2192)\s*$", re.IGNORECASE)


class DatePrecision(StrEnum):
    """How precisely the resume stated a date. Only ``MONTH`` is usable as one."""

    MONTH = "month"
    YEAR = "year"
    """"2021" \u2014 the year is known, the month is not. Never assumed to be January."""
    SEASON = "season"
    """"Summer 2023" \u2014 a season is not a month, and its months differ by hemisphere."""
    PRESENT = "present"
    """Still going. Usable as an end; :class:`MonthSpan` leaves it open."""
    ABSENT = "absent"
    """The resume did not say."""


@dataclass(frozen=True, slots=True)
class ParsedDate:
    """One end of a range, at the precision the resume gave it."""

    precision: DatePrecision
    #: The first of the month, set only when ``precision`` is ``MONTH``.
    month: dt.date | None = None
    #: Set when the resume named a year, whatever the precision. Enough to sort roles
    #: into the right order even when the month has to be asked about.
    year: int | None = None
    #: What the resume actually said, for the question and the approval view.
    text: str = ""

    @property
    def is_month(self) -> bool:
        return self.precision is DatePrecision.MONTH and self.month is not None


_ABSENT = ParsedDate(precision=DatePrecision.ABSENT)


@dataclass(frozen=True, slots=True)
class DateRange:
    """A resume's statement about when something happened."""

    start: ParsedDate
    end: ParsedDate
    #: The matched substring, so the entry parser can strip it off a head line.
    text: str = ""
    #: Where in the line the match began, for the same reason.
    position: int = -1

    @property
    def usable(self) -> bool:
        """True when a dated row can be created from this without guessing."""
        if not self.start.is_month:
            return False
        if self.end.precision is DatePrecision.PRESENT:
            return True
        return self.end.is_month and self.span is not None

    @property
    def span(self) -> MonthSpan | None:
        """The span, or ``None`` if the resume did not state one precisely.

        An end before its start returns ``None`` rather than raising: a typo in a
        resume is an ordinary thing to find, and it becomes a question.
        """
        if not self.start.is_month or self.start.month is None:
            return None
        if self.end.precision is DatePrecision.PRESENT:
            return MonthSpan(self.start.month)
        if not self.end.is_month or self.end.month is None:
            return None
        if self.end.month < self.start.month:
            return None
        return MonthSpan(self.start.month, self.end.month)

    @property
    def problem(self) -> str:
        """Why this range cannot be used, phrased for the candidate. "" when usable."""
        if self.usable:
            return ""
        if self.start.precision is DatePrecision.ABSENT:
            return "I could not find any dates for this"
        if self.start.precision is DatePrecision.SEASON:
            return f"{self.start.text!r} names a season rather than a month"
        if self.start.precision is DatePrecision.YEAR:
            return f"{self.start.text!r} gives a year but not a month"
        if self.end.precision is DatePrecision.ABSENT:
            return f"the start is {self.start.text!r} but no end is given"
        if self.end.precision is DatePrecision.SEASON:
            return f"{self.end.text!r} names a season rather than a month"
        if self.end.precision is DatePrecision.YEAR:
            return f"{self.end.text!r} gives a year but not a month"
        return f"{self.text!r} ends before it starts"


def _valid_year(year: int, *, today: dt.date) -> bool:
    return EARLIEST_YEAR <= year <= today.year + 1


def _from_match(match: re.Match[str], *, today: dt.date) -> ParsedDate | None:
    """One date token, or ``None`` if it is not a date after all."""
    text = match.group(0).strip()

    if match.group("present"):
        return ParsedDate(precision=DatePrecision.PRESENT, text=text)

    def month_date(year: int, month: int) -> ParsedDate | None:
        if not _valid_year(year, today=today):
            return None
        return ParsedDate(
            precision=DatePrecision.MONTH,
            month=dt.date(year, month, 1),
            year=year,
            text=text,
        )

    if match.group("month_year"):
        return month_date(int(match.group("myear")), _MONTHS[match.group("mname").lower()])
    if match.group("year_month"):
        return month_date(int(match.group("ymy")), int(match.group("ymm")))
    if match.group("month_num"):
        return month_date(int(match.group("mny")), int(match.group("mnm")))

    if match.group("season"):
        year = int(match.group("syear"))
        if not _valid_year(year, today=today):
            return None
        return ParsedDate(precision=DatePrecision.SEASON, year=year, text=text)

    year = int(match.group("yonly"))
    if not _valid_year(year, today=today):
        return None
    return ParsedDate(precision=DatePrecision.YEAR, year=year, text=text)


def parse_month(text: str, *, today: dt.date | None = None) -> ParsedDate:
    """The first date in ``text``, at whatever precision it was written."""
    now = today or utc_now().date()
    for match in _TOKEN.finditer(text):
        parsed = _from_match(match, today=now)
        if parsed is not None:
            return parsed
    return _ABSENT


def parse_date_range(text: str, *, today: dt.date | None = None) -> DateRange | None:
    """The first date range in ``text``, or ``None`` if there is no date in it.

    A range is two date tokens with nothing but a separator between them. A single
    token is returned as a range with an absent end — which is not usable, and says
    so, because "Accenture, Jun 2024" does not state when the role finished.
    """
    now = today or utc_now().date()
    found: list[tuple[ParsedDate, re.Match[str]]] = []
    for match in _TOKEN.finditer(text):
        parsed = _from_match(match, today=now)
        if parsed is not None:
            found.append((parsed, match))

    if not found:
        return None

    # A leading "Present" is not a start. It happens when a line reads
    # "Current role: ..."; treat the next token as the start instead.
    while found and found[0][0].precision is DatePrecision.PRESENT and len(found) > 1:
        found.pop(0)

    start, start_match = found[0]
    for end, end_match in found[1:]:
        between = text[start_match.end() : end_match.start()]
        if _SEPARATOR.match(between):
            return DateRange(
                start=start,
                end=end,
                text=text[start_match.start() : end_match.end()].strip(),
                position=start_match.start(),
            )
        break

    return DateRange(start=start, end=_ABSENT, text=start.text, position=start_match.start())
