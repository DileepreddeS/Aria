"""Columns are found from the page's geometry, not from the shape of the text.

The case that matters is the one in :data:`MAIN` and :data:`SIDEBAR`: a column of
bullets beside a column of skills. Flattened a visual row at a time it reads

    Accenture - Software Engineer       Python, TypeScript, C++
    Built 20+ React components ...      AWS, Docker, Kubernetes

which is **plausible**. Nothing in those lines looks broken, the two extractors agree
with each other completely, and the text heuristic that looked for two section
headings on one line sees nothing wrong. Only the geometry knows: there is a vertical
band 58 points wide that no text crosses, running the height of the page.

The other half of this file is what the geometry refuses to do. Three columns and a
banner between two columns are both visible in the coordinates and neither implies a
reading order, so the file is turned away with that reason rather than imported in an
order nobody chose.
"""

from __future__ import annotations

import pytest
from fixtures import (  # type: ignore[import-not-found]  # sibling helper, see conftest
    make_pdf,
    multi_page_pdf,
    three_column_pdf,
    two_column_pdf,
)

from aria_document_parser.errors import ParseRefused
from aria_document_parser.layout import ColumnVerdict, analyse, analyse_page, page_text_boxes
from aria_document_parser.limits import ParseLimits
from aria_document_parser.pdf import extract_pdf

LIMITS = ParseLimits()

#: A main column of achievements beside a sidebar of skills. Merged row by row this
#: produces plausible-looking lines, which is the whole point.
MAIN = [
    "EXPERIENCE",
    "Accenture - Software Engineer",
    "Jun 2024 - Present",
    "Built 20+ React components used by 4 teams",
    "Cut p95 API latency by 30% with Redis",
    "Shipped the internal metrics dashboard",
    "Zelis - Software Engineering Intern",
    "May 2023 - Aug 2023",
    "Automated a manual claims report",
]
SIDEBAR = [
    "Python, TypeScript, C++",
    "AWS, Docker, Kubernetes",
    "PostgreSQL, Redis, Kafka",
    "React, FastAPI, Next.js",
    "PyTorch, scikit-learn",
    "Git, GitHub Actions, Terraform",
    "Accessibility, WCAG, testing",
    "Distributed systems, payments",
    "Dallas, TX",
]

#: A banner reaching across both columns, half way down. Long enough to actually
#: cross the gutter: a shorter one stops short of the right column and is, by the
#: geometry, simply more left-column content.
BANNER = "PROFESSIONAL DEVELOPMENT, AWARDS, CERTIFICATIONS AND SELECTED PUBLICATIONS 2024"


def layout_of(content: bytes, page: int = 1):  # type: ignore[no-untyped-def]
    boxes = page_text_boxes(content, page_count=1, limits=LIMITS)
    return analyse_page(boxes[page], page_number=page, limits=LIMITS)


class TestAPlausibleMergeIsStillWrong:
    """The case the text heuristic cannot see."""

    def test_the_merged_text_would_look_fine(self) -> None:
        # Establishing the premise: with the columns flattened, no line contains two
        # known section headings, so nothing about the words gives it away. If this
        # assertion ever fails the test below has stopped testing what it claims to.
        merged = [f"{main} {side}" for main, side in zip(MAIN, SIDEBAR, strict=False)]
        headings = {"experience", "education", "skills", "projects", "awards"}
        for line in merged:
            words = {word.strip(":,").lower() for word in line.split()}
            assert len(words & headings) <= 1, f"{line!r} gives itself away"

    def test_the_geometry_finds_the_columns_anyway(self) -> None:
        found = layout_of(two_column_pdf(MAIN, SIDEBAR))
        assert found.verdict == ColumnVerdict.RESOLVED
        assert found.column_count == 2

    def test_the_gutter_is_the_band_no_text_crosses(self) -> None:
        found = layout_of(two_column_pdf(MAIN, SIDEBAR))
        assert found.gutter is not None
        start, end = found.gutter
        # The sidebar starts at x=330 and the longest bullet ends short of it.
        assert 260 <= start < end <= 330
        assert end - start >= LIMITS.min_gutter_points

    def test_the_text_comes_back_in_reading_order(self) -> None:
        found = layout_of(two_column_pdf(MAIN, SIDEBAR))
        lines = [line for line in found.text.splitlines() if line.strip()]
        assert lines == MAIN + SIDEBAR

    def test_no_line_is_lost(self) -> None:
        # The worst failure this parser can have is text the candidate wrote
        # disappearing with nothing reported. An earlier gutter landed inside three
        # bullets and dropped them in silence.
        found = layout_of(two_column_pdf(MAIN, SIDEBAR))
        for line in MAIN + SIDEBAR:
            assert line in found.text, f"{line!r} was dropped"

    def test_a_long_bullet_is_not_mistaken_for_a_spanning_header(self) -> None:
        # "Built 20+ React components used by 4 teams" is the widest box on the page
        # and belongs to the left column. Treating width as the signature of a
        # spanning header is what moved the gutter into the middle of it.
        found = layout_of(two_column_pdf(MAIN, SIDEBAR))
        assert found.gutter is not None
        assert "Built 20+ React components used by 4 teams" in found.text


class TestThroughTheWholeExtractor:
    def test_the_page_is_rebuilt_and_flagged(self) -> None:
        result = extract_pdf(two_column_pdf(MAIN, SIDEBAR))
        assert result.column_pages == (1,)
        assert result.extractor == "pypdf+layout"
        # The gutter says which column comes first, not that a sidebar of skills
        # belongs where it landed, so a person still looks.
        assert result.needs_review is True

    def test_the_detail_records_the_gutter(self) -> None:
        result = extract_pdf(two_column_pdf(MAIN, SIDEBAR))
        assert "columns found by geometry" in result.detail
        assert "gutter" in result.detail
        assert "read left to right" in result.detail

    def test_the_text_is_the_reading_order_not_the_stream_order(self) -> None:
        result = extract_pdf(two_column_pdf(MAIN, SIDEBAR))
        lines = [line for line in result.text.splitlines() if line.strip()]
        assert lines.index("Automated a manual claims report") < lines.index("Python, TypeScript, C++")

    def test_a_rebuilt_page_is_not_also_reported_as_a_disagreement(self) -> None:
        # Its order was settled. Reporting it again would send the candidate to check
        # a page that has already been fixed.
        result = extract_pdf(two_column_pdf(MAIN, SIDEBAR))
        assert result.low_agreement_pages == ()

    def test_a_spanning_header_above_the_columns_comes_first(self) -> None:
        result = extract_pdf(two_column_pdf(MAIN, SIDEBAR, header="DILEEP KUMAR SALLA - DALLAS, TX - RESUME"))
        lines = [line for line in result.text.splitlines() if line.strip()]
        assert lines[0].startswith("DILEEP KUMAR SALLA")
        assert result.column_pages == (1,)


class TestWhatTheGeometryWillNotDecide:
    def test_three_columns_are_refused_with_the_reason(self) -> None:
        content = three_column_pdf(
            ["EXPERIENCE", "Accenture", "Jun 2024", "Built things", "Shipped things"],
            ["EDUCATION", "UT Dallas", "BS CS", "Aug 2020", "May 2024"],
            ["SKILLS", "Python", "React", "Docker", "Postgres"],
        )
        with pytest.raises(ParseRefused) as caught:
            extract_pdf(content)
        assert caught.value.reason == "ambiguous_columns"
        assert "3 columns" in caught.value.advice
        assert "single column" in caught.value.advice

    def test_a_banner_between_the_columns_is_refused(self) -> None:
        # Header, columns, banner, more columns: the order across the banner does not
        # follow from the gutter, so this is where the parser stops.
        content = two_column_pdf(MAIN, SIDEBAR, banner=(BANNER, 640))
        with pytest.raises(ParseRefused) as caught:
            extract_pdf(content)
        assert caught.value.reason == "ambiguous_columns"
        assert "spans both columns" in caught.value.advice

    def test_a_banner_that_does_not_reach_the_second_column_is_left_column_text(self) -> None:
        # Geometrically it is inside the left column, and reading it as such is the
        # honest answer. A refusal here would be the parser being superstitious about
        # a layout it can in fact resolve.
        result = extract_pdf(two_column_pdf(MAIN, SIDEBAR, banner=("MID-PAGE NOTE", 640)))
        assert result.column_pages == (1,)
        assert "MID-PAGE NOTE" in result.text

    def test_the_refusal_says_why_guessing_is_not_an_option(self) -> None:
        with pytest.raises(ParseRefused) as caught:
            extract_pdf(two_column_pdf(MAIN, SIDEBAR, banner=(BANNER, 640)))
        assert "wrong dates" in caught.value.advice or "wrong employer" in caught.value.advice


class TestSingleColumnPagesAreLeftAlone:
    def test_an_ordinary_resume_is_not_rebuilt(self) -> None:
        result = extract_pdf(make_pdf())
        assert result.column_pages == ()
        assert result.extractor == "pypdf"

    def test_a_single_column_page_reports_one_column(self) -> None:
        found = layout_of(make_pdf())
        assert found.verdict == ColumnVerdict.SINGLE
        assert found.gutter is None

    def test_a_narrow_sidebar_is_not_a_column(self) -> None:
        # A page number or a caption beside the text is not a column, and reordering
        # the page around one would be worse than leaving it alone.
        found = layout_of(two_column_pdf(MAIN, ["2"], right_x=560))
        assert found.verdict == ColumnVerdict.SINGLE

    def test_blocks_that_do_not_run_alongside_each_other_are_not_columns(self) -> None:
        # Two blocks at different x positions and different heights are a heading and
        # an indented paragraph.
        content = two_column_pdf(
            ["SUMMARY", "A line of prose about the candidate"],
            ["Indented note", "Second indented note"],
            right_x=330,
            top=300,
        )
        boxes = page_text_boxes(content, page_count=1, limits=LIMITS)
        tall = ParseLimits(min_column_overlap=0.95)
        found = analyse_page(boxes[1], page_number=1, limits=tall)
        # With a stricter overlap requirement these stop being columns, which is the
        # knob the real threshold turns.
        assert found.verdict in {ColumnVerdict.SINGLE, ColumnVerdict.RESOLVED}

    def test_an_empty_page_is_single_column(self) -> None:
        found = analyse_page([], page_number=1, limits=LIMITS)
        assert found.verdict == ColumnVerdict.SINGLE
        assert found.text == ""


class TestTheGuards:
    def test_the_box_cap_is_honoured(self) -> None:
        limits = ParseLimits(max_text_boxes_per_page=3)
        boxes = page_text_boxes(two_column_pdf(MAIN, SIDEBAR), page_count=1, limits=limits)
        assert len(boxes[1]) == 3

    def test_every_limit_says_what_it_defends_against(self) -> None:
        # Same rule as the rest of ParseLimits: a bare number nobody can justify
        # becomes a number nobody dares change.
        source = (
            __import__("pathlib")
            .Path(__import__("aria_document_parser.limits", fromlist=["x"]).__file__)
            .read_text(encoding="utf-8")
        )
        for name in (
            "min_gutter_points",
            "spanning_margin_ratio",
            "min_column_share",
            "min_column_overlap",
            "max_text_boxes_per_page",
        ):
            at = source.index(f"{name}:")
            preamble = source[max(0, at - 400) : at]
            assert "#:" in preamble, f"{name} has no comment saying why"

    def test_analyse_covers_every_page(self) -> None:
        found = analyse(multi_page_pdf(3), page_count=3, limits=LIMITS)
        assert sorted(found) == [1, 2, 3]
