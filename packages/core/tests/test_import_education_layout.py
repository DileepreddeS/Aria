"""parse_education on the head lines the layout path hands it (RESUME_ENGINE §5.4).

The geometric assembler gives an education entry its school, degree, coursework and
date as separate head lines; these are the shapes the four gate fixtures produced.
"""

from __future__ import annotations

import datetime as dt

from aria_core.importer.entries import Entry, parse_education
from aria_core.importer.lines import SourceLine
from aria_core.taxonomy.loader import load_default_taxonomy

TODAY = dt.date(2026, 10, 12)


def _entry(*head: str) -> Entry:
    lines = tuple(
        SourceLine(number=index + 1, text=text, raw=text, bullet=False) for index, text in enumerate(head)
    )
    return Entry(head=lines, bullets=())


def test_a_school_line_s_trailing_place_is_its_location() -> None:
    education = parse_education(
        _entry(
            "Westbrook Institute, Pune, India", "B.Tech, Electrical & Electronics Engineering", "Jun 2022"
        ),
        load_default_taxonomy(),
        today=TODAY,
    )
    assert education.school == "Westbrook Institute"
    assert (education.location_city, education.location_region) == ("Pune", "India")


def test_a_coursework_line_is_coursework_and_not_a_location() -> None:
    # Regression: "Deep Learning, Artificial Intelligence" has the shape of "City, Region".
    education = parse_education(
        _entry(
            "Cascadia State University",
            "M.S. Computer Science",
            "Relevant Coursework: Deep Learning, Artificial Intelligence",
            "Dec 2026",
        ),
        load_default_taxonomy(),
        today=TODAY,
    )
    assert education.coursework == "Deep Learning, Artificial Intelligence"
    assert education.location_city is None and education.location_region is None


def test_a_location_on_a_line_of_its_own_is_still_read() -> None:
    education = parse_education(
        _entry("Cascadia State University", "M.S. Computer Science", "Portland, OR", "Dec 2026"),
        load_default_taxonomy(),
        today=TODAY,
    )
    assert (education.location_city, education.location_region) == ("Portland", "OR")
    assert education.school == "Cascadia State University"


def test_a_school_whose_comma_is_not_a_place_keeps_its_name() -> None:
    education = parse_education(
        _entry("University of California, Berkeley", "B.S. Physics", "May 2020"),
        load_default_taxonomy(),
        today=TODAY,
    )
    assert education.school == "University of California, Berkeley"
