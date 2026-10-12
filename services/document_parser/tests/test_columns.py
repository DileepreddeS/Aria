"""Columns, reading order and running heads, from the page's geometry (RESUME_ENGINE §5.2).

Ported from test_layout.py, which tested the pdfminer text-box detector this replaces,
with two changes the 2026-10-11 decisions made: three or more columns are read left to
right rather than refused, and the content is the synthetic identity rather than a real
person's employers. Each ported test was run against the bug it was written for,
reinserted in the new code, and fails there.

The case that matters most is MAIN beside SIDEBAR: flattened a visual row at a time it
reads plausibly, the two extractors agree with each other completely, and nothing in
the words gives it away. Only the geometry knows.
"""

from __future__ import annotations

import io
import re
from collections.abc import Sequence
from pathlib import Path

import pdfplumber
from fixtures import (  # type: ignore[import-not-found]  # sibling helper, see conftest
    _assemble,
    _escape_pdf_string,
    _stream_object,
    make_pdf,
    multi_page_pdf,
    three_column_pdf,
    two_column_pdf,
)

from aria_core.documents import LayoutLine, WeightSource
from aria_document_parser import reading_order
from aria_document_parser.glyphs import page_glyphs
from aria_document_parser.lines import drawn_markers, horizontal_rules, rows
from aria_document_parser.reading_order import PageReading, read_page, set_aside_running_heads

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "resumes"

#: A main column of achievements beside a sidebar of skills.
MAIN = [
    "EXPERIENCE",
    "Nordvale Consulting - Software Engineer",
    "Feb 2024 - Jul 2025",
    "Built 20+ React components used by 4 teams",
    "Cut p95 API latency by 30% with Redis",
    "Shipped the internal metrics dashboard",
    "Pinecrest Labs - Software Engineer Intern",
    "Oct 2021 - Apr 2022",
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
    "Portland, OR",
]
#: Long enough to reach across the gutter half way down the page.
BANNER = "PROFESSIONAL DEVELOPMENT, AWARDS, CERTIFICATIONS AND SELECTED PUBLICATIONS 2024"


def read(content: bytes) -> list[PageReading]:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        pages = []
        for number, page in enumerate(pdf.pages, start=1):
            page_rows, _ = rows(page_glyphs(page), number)
            pages.append(
                read_page(
                    page_rows,
                    page=number,
                    width=float(page.width),
                    weight_source=WeightSource.FONT_NAME,
                    markers=drawn_markers(page),
                    rules=horizontal_rules(page),
                )
            )
        heights = [float(page.height) for page in pdf.pages]
    return set_aside_running_heads(pages, heights)


def texts(page: PageReading) -> list[str]:
    return [line.text for line in page.lines]


def two_pages(first: Sequence[tuple[float, float, str]], second: Sequence[tuple[float, float, str]]) -> bytes:
    """Two pages of text placed at (x, y) in PDF coordinates."""

    def content(items: Sequence[tuple[float, float, str]]) -> bytes:
        return b"".join(
            b"BT /F1 10 Tf " + f"{x} {y} Td (".encode() + _escape_pdf_string(text) + b") Tj ET "
            for x, y, text in items
        )

    pdf: bytes = _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
            b"/Contents 6 0 R >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
            b"/Contents 7 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            _stream_object(content(first)),
            _stream_object(content(second)),
        ]
    )
    return pdf


class TestAPlausibleMergeIsStillWrong:
    def test_the_merged_text_would_look_fine(self) -> None:
        # The premise: flattened, no line holds two section headings, so the words give
        # nothing away. If this fails, the tests below stop testing what they claim.
        headings = {"experience", "education", "skills", "projects", "awards"}
        for main, side in zip(MAIN, SIDEBAR, strict=False):
            words = {word.strip(":,").lower() for word in f"{main} {side}".split()}
            assert len(words & headings) <= 1, f"{main} {side} gives itself away"

    def test_the_geometry_finds_the_columns_anyway(self) -> None:
        assert read(two_column_pdf(MAIN, SIDEBAR))[0].columns == 2

    def test_the_gutter_is_the_band_no_text_crosses(self) -> None:
        (gutter,) = read(two_column_pdf(MAIN, SIDEBAR))[0].gutters
        start, end = gutter
        # The sidebar starts at x=330 and the longest bullet ends short of it.
        assert 260 <= start < end <= 330
        assert end - start >= reading_order.MIN_GUTTER_WIDTH * 612

    def test_the_text_comes_back_in_reading_order(self) -> None:
        assert texts(read(two_column_pdf(MAIN, SIDEBAR))[0]) == MAIN + SIDEBAR

    def test_no_line_is_lost(self) -> None:
        # The worst failure a parser can have is text disappearing with nothing
        # reported. An earlier gutter landed inside three bullets and dropped them.
        found = texts(read(two_column_pdf(MAIN, SIDEBAR))[0])
        for line in MAIN + SIDEBAR:
            assert line in found, f"{line!r} was dropped"

    def test_a_long_bullet_is_not_mistaken_for_a_spanning_header(self) -> None:
        # The widest line on the page belongs to the left column. Treating width as the
        # signature of a spanning header is what moved the old gutter into it.
        page = read(two_column_pdf(MAIN, SIDEBAR))[0]
        bullet = next(line for line in page.lines if line.text.startswith("Built 20+"))
        assert page.columns == 2 and not page.ambiguous
        assert bullet.column == 0 and bullet.text == "Built 20+ React components used by 4 teams"

    def test_a_header_above_the_columns_comes_first(self) -> None:
        # Ends inside the gutter without reaching the right column: left-column text by
        # its geometry, read first because it is above everything.
        page = read(two_column_pdf(MAIN, SIDEBAR, header="JORDAN A. RIVERA - PORTLAND, OR - RESUME"))[0]
        assert page.lines[0].text.startswith("JORDAN A. RIVERA")
        assert page.columns == 2 and not page.ambiguous

    def test_a_header_spanning_both_columns_is_read_as_one_line_first(self) -> None:
        header = "JORDAN A. RIVERA - PORTLAND, OR - jordan.rivera@example.com - (555) 014-2288"
        page = read(two_column_pdf(MAIN, SIDEBAR, header=header))[0]
        assert page.lines[0].text == header and page.lines[0].column == -1
        assert page.columns == 2 and not page.ambiguous
        assert texts(page)[1:] == MAIN + SIDEBAR


class TestWhatTheGeometryDecides:
    def test_three_columns_are_read_left_to_right(self) -> None:
        first = ["EXPERIENCE", "Nordvale", "Feb 2024", "Built things", "Shipped things"]
        second = ["EDUCATION", "Cascadia State", "M.S. CS", "Aug 2025", "Dec 2026"]
        third = ["SKILLS", "Python", "React", "Docker", "Postgres"]
        page = read(three_column_pdf(first, second, third))[0]
        assert page.columns == 3 and not page.ambiguous
        assert texts(page) == first + second + third

    def test_a_banner_across_the_gutter_mid_page_makes_the_order_ambiguous(self) -> None:
        page = read(two_column_pdf(MAIN, SIDEBAR, banner=(BANNER, 640)))[0]
        assert page.ambiguous
        # Ambiguous is a refusal with a reason, never a quiet loss of the line.
        assert BANNER in texts(page)

    def test_a_banner_that_does_not_reach_the_second_column_is_left_column_text(self) -> None:
        page = read(two_column_pdf(MAIN, SIDEBAR, banner=("MID-PAGE NOTE", 640)))[0]
        assert not page.ambiguous and page.columns == 2
        note = next(line for line in page.lines if line.text == "MID-PAGE NOTE")
        assert note.column == 0


class TestSingleColumnPagesAreLeftAlone:
    def test_an_ordinary_page_is_one_column(self) -> None:
        page = read(make_pdf())[0]
        assert page.columns == 1 and page.gutters == ()

    def test_a_page_number_beside_the_text_is_not_a_column(self) -> None:
        # At the foot of the page, level with the last line: in the top row it would
        # sit in the header band, which column detection leaves out, and the test would
        # never reach the rules it is about.
        page = read(two_column_pdf(MAIN, [""] * (len(MAIN) - 1) + ["2"], right_x=560))[0]
        assert page.columns == 1
        assert any(line.text.endswith("2") for line in page.lines)

    def test_a_narrow_strip_of_short_text_beside_the_body_is_not_a_column(self) -> None:
        # Text alongside the whole body, but a few characters a row: a share rule
        # matter, not a height one.
        body = [f"Achievement number {index} with enough words to read as a bullet" for index in range(10)]
        page = read(two_column_pdf(body, ["x"] * 10, right_x=520))[0]
        assert page.columns == 1

    def test_blocks_at_different_heights_are_not_columns(self) -> None:
        left = [(50, 720 - 18 * index, f"Summary line {index} of the candidate") for index in range(6)]
        right = [(330, 400 - 18 * index, f"Indented note {index}") for index in range(6)]
        page = read(two_pages(left + right, [(50, 720, "Second page text " * 15)]))[0]
        assert page.columns == 1

    def test_right_aligned_dates_are_not_a_column(self) -> None:
        for name in ("clean_inline_block", "float_scrambled", "markerless_bullets", "per_glyph_contact"):
            for page in read((FIXTURES / f"{name}.pdf").read_bytes()):
                assert page.columns == 1, (name, page.page)
                assert not page.ambiguous

    def test_an_empty_page_is_one_column(self) -> None:
        page = read_page([], page=1, width=612.0, weight_source=WeightSource.FONT_NAME)
        assert page.lines == [] and page.columns == 1


class TestRunningHeads:
    def test_text_repeated_in_the_margins_of_every_page_is_set_aside(self) -> None:
        content = two_pages(
            [(50, 770, "Jordan A. Rivera - Resume - Page 1 of 2"), (50, 700, "EXPERIENCE")],
            [(50, 770, "Jordan A. Rivera - Resume - Page 2 of 2"), (50, 700, "PROJECTS")],
        )
        first, second = read(content)
        assert texts(first) == ["EXPERIENCE"] and texts(second) == ["PROJECTS"]
        assert first.running_heads == ["Jordan A. Rivera - Resume - Page 1 of 2"]

    def test_a_margin_line_on_one_page_only_is_body_text(self) -> None:
        content = two_pages([(50, 770, "Jordan A. Rivera"), (50, 700, "EXPERIENCE")], [(50, 700, "PROJECTS")])
        first, _ = read(content)
        assert texts(first) == ["Jordan A. Rivera", "EXPERIENCE"]
        assert first.running_heads == []

    def test_line_indexes_run_from_zero_after_heads_are_removed(self) -> None:
        content = two_pages(
            [(50, 770, "Page 1"), (50, 700, "EXPERIENCE"), (50, 680, "Nordvale")],
            [(50, 770, "Page 2"), (50, 700, "PROJECTS")],
        )
        first, _ = read(content)
        assert [line.index for line in first.lines] == [0, 1]


class TestTheGuards:
    def test_every_page_is_read(self) -> None:
        assert [page.page for page in read(multi_page_pdf(3))] == [1, 2, 3]

    def test_every_threshold_says_what_it_is_for(self) -> None:
        # A bare number nobody can justify becomes a number nobody dares change.
        source = Path(reading_order.__file__).read_text(encoding="utf-8")
        for name in (
            "MIN_GUTTER_WIDTH",
            "MIN_COLUMN_HEIGHT",
            "MIN_COLUMN_SHARE",
            "HEADER_BAND",
            "RUNNING_HEAD_BAND",
        ):
            at = re.search(rf"^{name} = ", source, re.MULTILINE)
            assert at is not None, name
            assert "#:" in source[max(0, at.start() - 300) : at.start()], f"{name} has no comment saying why"

    def test_lines_are_layout_lines(self) -> None:
        assert all(isinstance(line, LayoutLine) for line in read(make_pdf())[0].lines)
