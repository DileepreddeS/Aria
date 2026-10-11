"""Month arithmetic and interval unions.

Pulled out of :mod:`aria_core.graph.derive` because this is the part that decides
whether "3 years of React" is true, and it should be readable and testable without a
database in sight.

Two decisions, both about not overstating:

* **A month is a unit, not a point.** A role from June 2024 to June 2024 is one month
  of experience, not zero. The span of a closed interval is ``end - start + 1`` months.
* **Nothing is rounded up.** 23 months is 1.9 years, not 2. The product's whole claim
  is that its numbers survive scrutiny, and "almost two years" presented as two is the
  smallest possible version of the thing it refuses to do.

Concurrent roles are merged before counting, so two overlapping jobs do not add up to
more experience than the calendar allows.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

__all__ = ["MonthSpan", "months_to_years", "total_months", "union_spans"]


def first_of_month(value: dt.date) -> dt.date:
    return value.replace(day=1)


def month_index(value: dt.date) -> int:
    """Months since year 0, so spans are integer arithmetic rather than date maths."""
    return value.year * 12 + (value.month - 1)


def from_month_index(index: int) -> dt.date:
    return dt.date(index // 12, index % 12 + 1, 1)


@dataclass(frozen=True, slots=True, order=True)
class MonthSpan:
    """A closed range of whole months. ``end`` is included.

    ``end=None`` means "still going": the caller supplies ``now`` when it needs a
    length, so a graph does not silently age between two reads of the same row.
    """

    start: dt.date
    end: dt.date | None = None

    def __post_init__(self) -> None:
        if self.start.day != 1 or (self.end is not None and self.end.day != 1):
            raise ValueError("a MonthSpan is built from the first of a month")
        if self.end is not None and self.end < self.start:
            raise ValueError(f"span ends ({self.end}) before it starts ({self.start})")

    @property
    def is_open(self) -> bool:
        return self.end is None

    def resolve(self, now: dt.date) -> MonthSpan:
        """Close an open span at the current month.

        "Present" means through this month, inclusive: someone who started this month
        has one month of experience, not zero.
        """
        if self.end is not None:
            return self
        current = first_of_month(now)
        return MonthSpan(self.start, max(current, self.start))

    def length_in_months(self, now: dt.date) -> int:
        closed = self.resolve(now)
        assert closed.end is not None
        return month_index(closed.end) - month_index(closed.start) + 1

    def overlaps_or_touches(self, other: MonthSpan, now: dt.date) -> bool:
        """True when the two spans are adjacent or overlapping.

        Adjacent counts: March-June followed by July-December is ten continuous
        months, and treating them as separate would lose the month at the boundary.
        """
        left, right = self.resolve(now), other.resolve(now)
        assert left.end is not None and right.end is not None
        return month_index(left.start) <= month_index(right.end) + 1 and (
            month_index(right.start) <= month_index(left.end) + 1
        )


def union_spans(spans: list[MonthSpan], *, now: dt.date) -> list[MonthSpan]:
    """Merge overlapping and adjacent spans. Returns them sorted, non-overlapping.

    This is what keeps two concurrent roles from counting twice. Someone who held a
    part-time job while interning has the calendar months they lived through, not the
    sum of both contracts.
    """
    if not spans:
        return []

    resolved = sorted(span.resolve(now) for span in spans)
    merged: list[MonthSpan] = [resolved[0]]
    for span in resolved[1:]:
        last = merged[-1]
        assert last.end is not None and span.end is not None
        if month_index(span.start) <= month_index(last.end) + 1:
            merged[-1] = MonthSpan(last.start, max(last.end, span.end))
        else:
            merged.append(span)
    return merged


def total_months(spans: list[MonthSpan], *, now: dt.date) -> int:
    """Whole months covered by the union of ``spans``."""
    return sum(span.length_in_months(now) for span in union_spans(spans, now=now))


def months_to_years(months: int) -> float:
    """Years to one decimal place, **truncated**.

    Truncated rather than rounded: 23 months is 1.9 years. Rounding 23 up to 2.0 would
    be a claim the calendar does not support, and a resume that says "2 years" when the
    answer is 23 months has told an employer something untrue.
    """
    tenths = (months * 10) // 12
    return tenths / 10
