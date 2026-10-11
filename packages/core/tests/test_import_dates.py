"""Dates are read at the precision the resume wrote them, or not at all.

The rule under test: a range is ``usable`` only with a real start month and a real
end. "Summer 2023" and "2021-2023" are **not** usable, because a season is not a month
and a year is not a month, and every years-of-experience figure downstream is built on
these. Turning "Summer 2023" into June would be inventing the one fact the candidate
can always supply from memory.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aria_core.importer.dates import DatePrecision, parse_date_range, parse_month

TODAY = dt.date(2026, 10, 8)


def month(year: int, month_number: int) -> dt.date:
    return dt.date(year, month_number, 1)


class TestMonthFormats:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Jun 2024", month(2024, 6)),
            ("June 2024", month(2024, 6)),
            ("Jun. 2024", month(2024, 6)),
            ("Jun, 2024", month(2024, 6)),
            ("Sept 2022", month(2022, 9)),
            ("September 2022", month(2022, 9)),
            ("06/2024", month(2024, 6)),
            ("6/2024", month(2024, 6)),
            ("2024-06", month(2024, 6)),
            ("06-2024", month(2024, 6)),
        ],
    )
    def test_a_month_and_year_resolve(self, text: str, expected: dt.date) -> None:
        parsed = parse_month(text, today=TODAY)
        assert parsed.precision is DatePrecision.MONTH
        assert parsed.month == expected

    def test_the_resume_text_is_kept(self) -> None:
        # Shown back to the candidate in the approval view, so it has to be theirs.
        assert parse_month("Sept 2022", today=TODAY).text == "Sept 2022"


class TestPrecisionShortOfAMonth:
    @pytest.mark.parametrize("season", ["Summer 2023", "Fall 2022", "Spring 2024", "Winter 2021"])
    def test_a_season_is_not_a_month(self, season: str) -> None:
        parsed = parse_month(season, today=TODAY)
        assert parsed.precision is DatePrecision.SEASON
        assert parsed.month is None
        # The year is still known, which is enough to order the roles correctly.
        assert parsed.year is not None

    def test_a_year_alone_is_not_a_month(self) -> None:
        parsed = parse_month("2021", today=TODAY)
        assert parsed.precision is DatePrecision.YEAR
        assert parsed.month is None
        assert parsed.year == 2021

    def test_nothing_at_all(self) -> None:
        assert parse_month("Software Engineer", today=TODAY).precision is DatePrecision.ABSENT


class TestRanges:
    @pytest.mark.parametrize("separator", ["-", "\u2013", "\u2014", "to", "until", "through", "\u2192"])
    def test_separators(self, separator: str) -> None:
        found = parse_date_range(f"Jun 2024 {separator} Aug 2024", today=TODAY)
        assert found is not None and found.usable
        assert found.span is not None
        assert found.span.start == month(2024, 6)
        assert found.span.end == month(2024, 8)

    @pytest.mark.parametrize(
        "text",
        [
            "Jun 2024 - Present",
            "Jun 2024 - present",
            "Jun 2024 \u2013 Current",
            "Jun 2024 - now",
            "Jun 2024 to date",
        ],
    )
    def test_an_open_end(self, text: str) -> None:
        found = parse_date_range(text, today=TODAY)
        assert found is not None and found.usable
        assert found.span is not None and found.span.is_open

    def test_a_tight_en_dash_with_no_spaces(self) -> None:
        found = parse_date_range("Jun 2024\u2013Aug 2024", today=TODAY)
        assert found is not None and found.usable

    def test_a_comma_is_a_list_not_a_range(self) -> None:
        # "Jun 2024, Aug 2024" names two months; reading it as a range would invent a
        # three-month role.
        found = parse_date_range("Jun 2024, Aug 2024", today=TODAY)
        assert found is not None
        assert found.end.precision is DatePrecision.ABSENT
        assert not found.usable


class TestWhatIsNotUsable:
    def test_a_start_with_no_end(self) -> None:
        found = parse_date_range("Accenture, Jun 2024", today=TODAY)
        assert found is not None and not found.usable
        assert "no end is given" in found.problem

    def test_a_season_range(self) -> None:
        found = parse_date_range("Summer 2023 - Fall 2023", today=TODAY)
        assert found is not None and not found.usable
        assert "season" in found.problem

    def test_a_year_range(self) -> None:
        found = parse_date_range("2021 - 2023", today=TODAY)
        assert found is not None and not found.usable
        assert "year but not a month" in found.problem

    def test_an_end_before_its_start_is_reported_not_swapped(self) -> None:
        # Swapping would produce a confident span the resume never claimed.
        found = parse_date_range("Aug 2024 - Jun 2024", today=TODAY)
        assert found is not None
        assert found.span is None
        assert not found.usable
        assert "ends before it starts" in found.problem

    @pytest.mark.parametrize("year", ["1823", "2051"])
    def test_a_year_outside_living_memory_is_not_a_date(self, year: str) -> None:
        assert parse_month(f"Jun {year}", today=TODAY).precision is not DatePrecision.MONTH

    def test_next_year_is_still_allowed(self) -> None:
        # A start date a few months out is a real thing on a resume.
        assert parse_month("Jan 2027", today=TODAY).precision is DatePrecision.MONTH


class TestProblemMessages:
    def test_a_usable_range_has_no_problem(self) -> None:
        found = parse_date_range("Jun 2024 - Aug 2024", today=TODAY)
        assert found is not None and found.problem == ""

    def test_the_problem_quotes_what_the_resume_said(self) -> None:
        found = parse_date_range("Summer 2023 - Fall 2023", today=TODAY)
        assert found is not None and "Summer 2023" in found.problem
