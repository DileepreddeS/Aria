"""Month arithmetic: the part that decides whether a years claim is true.

No database. These are the cases that would show up as an overstated resume.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aria_core.graph.months import (
    MonthSpan,
    first_of_month,
    month_index,
    months_to_years,
    total_months,
    union_spans,
)

NOW = dt.date(2026, 10, 8)


def month(year: int, month_number: int) -> dt.date:
    return dt.date(year, month_number, 1)


class TestSpanConstruction:
    def test_a_span_must_start_on_the_first(self) -> None:
        with pytest.raises(ValueError, match="first of a month"):
            MonthSpan(dt.date(2024, 6, 15))

    def test_a_span_must_end_on_the_first(self) -> None:
        with pytest.raises(ValueError, match="first of a month"):
            MonthSpan(month(2024, 6), dt.date(2024, 9, 30))

    def test_a_span_cannot_end_before_it_starts(self) -> None:
        with pytest.raises(ValueError, match=r"ends .* before it starts"):
            MonthSpan(month(2024, 6), month(2024, 1))

    def test_first_of_month_truncates(self) -> None:
        assert first_of_month(dt.date(2026, 10, 8)) == month(2026, 10)


class TestLength:
    def test_a_single_month_role_is_one_month(self) -> None:
        # Not zero: June to June is a month of work, and treating it as zero would
        # under-report a short internship out of existence.
        assert MonthSpan(month(2024, 6), month(2024, 6)).length_in_months(NOW) == 1

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            ((2024, 6), (2024, 7), 2),
            ((2024, 1), (2024, 12), 12),
            ((2023, 6), (2024, 5), 12),
            ((2022, 1), (2024, 12), 36),
        ],
    )
    def test_closed_spans(self, start: tuple[int, int], end: tuple[int, int], expected: int) -> None:
        assert MonthSpan(month(*start), month(*end)).length_in_months(NOW) == expected

    def test_an_open_span_runs_to_the_current_month_inclusive(self) -> None:
        # "Present" means through this month: Jun 2026 to Oct 2026 is five months.
        assert MonthSpan(month(2026, 6)).length_in_months(NOW) == 5

    def test_someone_who_started_this_month_has_one_month(self) -> None:
        assert MonthSpan(month(2026, 10)).length_in_months(NOW) == 1

    def test_a_future_start_does_not_go_negative(self) -> None:
        # A typo in an import, or a signed offer not yet started. One month, not -3.
        assert MonthSpan(month(2027, 1)).length_in_months(NOW) == 1

    def test_the_clock_is_supplied_not_read(self) -> None:
        # Two reads of one graph must agree, so nothing here calls today() itself.
        span = MonthSpan(month(2026, 1))
        assert span.length_in_months(dt.date(2026, 6, 1)) == 6
        assert span.length_in_months(dt.date(2026, 12, 1)) == 12


class TestUnion:
    def test_disjoint_spans_stay_separate(self) -> None:
        spans = [MonthSpan(month(2022, 1), month(2022, 6)), MonthSpan(month(2024, 1), month(2024, 6))]
        assert len(union_spans(spans, now=NOW)) == 2
        assert total_months(spans, now=NOW) == 12

    def test_overlapping_spans_merge(self) -> None:
        # Two concurrent roles are not twice the experience.
        spans = [MonthSpan(month(2024, 1), month(2024, 12)), MonthSpan(month(2024, 6), month(2025, 5))]
        merged = union_spans(spans, now=NOW)
        assert merged == [MonthSpan(month(2024, 1), month(2025, 5))]
        assert total_months(spans, now=NOW) == 17

    def test_a_contained_span_adds_nothing(self) -> None:
        spans = [MonthSpan(month(2024, 1), month(2024, 12)), MonthSpan(month(2024, 3), month(2024, 6))]
        assert total_months(spans, now=NOW) == 12

    def test_adjacent_spans_merge(self) -> None:
        # Mar-Jun then Jul-Dec is ten continuous months. Treating them as separate
        # ranges would be right by luck here but wrong where a gap matters.
        spans = [MonthSpan(month(2024, 3), month(2024, 6)), MonthSpan(month(2024, 7), month(2024, 12))]
        assert union_spans(spans, now=NOW) == [MonthSpan(month(2024, 3), month(2024, 12))]
        assert total_months(spans, now=NOW) == 10

    def test_a_one_month_gap_is_not_merged(self) -> None:
        # Jan-Mar, then May-Jun: April was not worked, and the total says so.
        spans = [MonthSpan(month(2024, 1), month(2024, 3)), MonthSpan(month(2024, 5), month(2024, 6))]
        assert len(union_spans(spans, now=NOW)) == 2
        assert total_months(spans, now=NOW) == 5

    def test_an_open_span_merges_with_what_precedes_it(self) -> None:
        spans = [MonthSpan(month(2025, 1), month(2025, 12)), MonthSpan(month(2026, 1))]
        assert total_months(spans, now=NOW) == 22

    def test_three_overlapping_spans_collapse_to_one(self) -> None:
        spans = [
            MonthSpan(month(2023, 1), month(2023, 6)),
            MonthSpan(month(2023, 4), month(2023, 9)),
            MonthSpan(month(2023, 8), month(2024, 2)),
        ]
        assert union_spans(spans, now=NOW) == [MonthSpan(month(2023, 1), month(2024, 2))]
        assert total_months(spans, now=NOW) == 14

    def test_order_does_not_matter(self) -> None:
        spans = [
            MonthSpan(month(2024, 6), month(2024, 12)),
            MonthSpan(month(2022, 1), month(2022, 3)),
            MonthSpan(month(2023, 5), month(2023, 8)),
        ]
        assert total_months(spans, now=NOW) == total_months(list(reversed(spans)), now=NOW) == 14

    def test_no_spans_is_zero_not_an_error(self) -> None:
        assert union_spans([], now=NOW) == []
        assert total_months([], now=NOW) == 0

    def test_duplicates_count_once(self) -> None:
        span = MonthSpan(month(2024, 1), month(2024, 6))
        assert total_months([span, span, span], now=NOW) == 6


class TestYearsAreNeverRoundedUp:
    @pytest.mark.parametrize(
        ("months", "years"),
        [
            (0, 0.0),
            (1, 0.0),
            (6, 0.5),
            (11, 0.9),
            (12, 1.0),
            (13, 1.0),
            # The case this rule exists for: 23 months is not two years.
            (23, 1.9),
            (24, 2.0),
            (35, 2.9),
            (36, 3.0),
        ],
    )
    def test_truncation(self, months: int, years: float) -> None:
        assert months_to_years(months) == years

    def test_a_month_short_of_a_year_never_reads_as_a_year(self) -> None:
        for whole_years in range(1, 11):
            months = whole_years * 12
            assert months_to_years(months - 1) < float(whole_years)
            assert months_to_years(months) == float(whole_years)


class TestMonthIndex:
    def test_it_is_monotonic_across_a_year_boundary(self) -> None:
        assert month_index(month(2024, 12)) + 1 == month_index(month(2025, 1))

    def test_a_year_is_twelve(self) -> None:
        assert month_index(month(2025, 6)) - month_index(month(2024, 6)) == 12
