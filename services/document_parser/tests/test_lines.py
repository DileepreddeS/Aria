"""Line building (RESUME_ENGINE §5.2-§5.3): rows, runs, words, markers, rules and weight."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pdfplumber
import pytest
from layout_pdfs import page_pdf, per_glyph_text, text_at  # type: ignore[import-not-found]

from aria_core.documents import LayoutLine, WeightSource
from aria_document_parser.glyphs import page_glyphs
from aria_document_parser.ink import PageInk
from aria_document_parser.lines import (
    DRAWN_MARKER,
    Row,
    drawn_markers,
    horizontal_rules,
    make_line,
    resolve_weight,
    rows,
)

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "resumes"
GATE_FIXTURES = ["clean_inline_block", "float_scrambled", "markerless_bullets", "per_glyph_contact"]


def _lines(content: bytes, *, ink: bool = True) -> tuple[list[LayoutLine], WeightSource, int]:
    """Every row of every page as one line (columns are assigned later, in reading order)."""
    with pdfplumber.open(io.BytesIO(content)) as pdf, PageInk(content) as page_ink:
        all_rows: list[Row] = []
        extras = {}
        rotated = 0
        for number, page in enumerate(pdf.pages, start=1):
            page_rows, page_rotated = rows(page_glyphs(page), number)
            all_rows.extend(page_rows)
            rotated += page_rotated
            extras[number] = (drawn_markers(page), horizontal_rules(page))
        resolved, source = resolve_weight(all_rows, page_ink.share if ink else None)
    lines = [
        make_line(
            row.runs,
            index=index,
            page=row.page,
            column=0,
            weight_source=source,
            markers=extras[row.page][0],
            rules=extras[row.page][1],
        )
        for index, row in enumerate(resolved)
    ]
    return lines, source, rotated


def _truth(name: str) -> dict[str, object]:
    truth: dict[str, object] = json.loads((FIXTURES / f"{name}.truth.json").read_text(encoding="utf-8"))
    return truth


class TestTheGateFixtures:
    @pytest.mark.parametrize("name", GATE_FIXTURES)
    def test_the_contact_line_reads_exactly(self, name: str) -> None:
        # per_glyph_contact draws its contact line with a positioning operator at every
        # inline boundary; the words must come out the same as from the clean rendering.
        lines, _, _ = _lines((FIXTURES / f"{name}.pdf").read_bytes())
        assert lines[1].text == _truth(name)["contact_line"]

    @pytest.mark.parametrize("name", GATE_FIXTURES)
    def test_every_bullet_and_nothing_else_opens_with_a_marker(self, name: str) -> None:
        lines, _, _ = _lines((FIXTURES / f"{name}.pdf").read_bytes())
        entries = _truth(name)["entries"]
        assert isinstance(entries, list)
        assert sum(1 for line in lines if line.marker) == sum(entry["bullet_count"] for entry in entries)

    def test_list_style_bullets_are_read_from_the_shape_beside_them(self) -> None:
        # No marker glyph and no indent difference: the drawn circle is the only signal.
        lines, _, _ = _lines((FIXTURES / "markerless_bullets.pdf").read_bytes())
        tail = next(line for line in lines if line.text == "mobile")
        opener = lines[tail.index - 1]
        assert opener.marker == DRAWN_MARKER and opener.x0 < opener.text_x0
        assert tail.marker == "" and tail.x0 == tail.text_x0 == opener.text_x0

    def test_the_same_geometry_reads_the_same_whatever_the_content_order(self) -> None:
        clean, _, _ = _lines((FIXTURES / "clean_inline_block.pdf").read_bytes())
        scrambled, _, _ = _lines((FIXTURES / "float_scrambled.pdf").read_bytes())
        assert [line.text for line in scrambled] == [line.text for line in clean]

    def test_an_entry_header_keeps_its_date_as_a_separate_bold_and_plain_part(self) -> None:
        lines, source, _ = _lines((FIXTURES / "clean_inline_block.pdf").read_bytes())
        header = next(line for line in lines if line.text.startswith("Nordvale Consulting"))
        assert source is WeightSource.FONT_NAME
        assert [segment.text for segment in header.segments][-1] == "Feb 2024 \u2013 Jul 2025"
        assert header.segments[0].bold and not header.segments[-1].bold
        assert header.segments[-1].x0 > 0.55 * 612

    def test_section_headings_have_their_rule(self) -> None:
        lines, _, _ = _lines((FIXTURES / "clean_inline_block.pdf").read_bytes())
        ruled = [line.text for line in lines if line.rule_below]
        assert {"EDUCATION", "EXPERIENCE"} <= set(ruled)


class TestWordsAndRuns:
    def test_words_are_rebuilt_from_gaps_when_no_space_is_drawn(self) -> None:
        lines, _, _ = _lines(page_pdf(per_glyph_text(72, 700, "Built the checkout platform")))
        assert lines[0].text == "Built the checkout platform"

    def test_a_date_at_the_right_margin_is_a_second_run_on_the_same_line(self) -> None:
        content = text_at(72, 700, "Nordvale Consulting") + text_at(480, 700, "Feb 2024")
        line = _lines(page_pdf(content))[0][0]
        assert [segment.text for segment in line.segments] == ["Nordvale Consulting", "Feb 2024"]
        assert line.text == "Nordvale Consulting Feb 2024"

    def test_wide_word_spacing_does_not_split_a_run(self) -> None:
        # Justified text stretches spaces to about one em; that is still one run.
        content = b"BT /F1 10 Tf 10 Tw 72 700 Td (Built the checkout platform) Tj ET"
        line = _lines(page_pdf(content))[0][0]
        assert len(line.segments) == 1 and line.text == "Built the checkout platform"

    def test_characters_slightly_off_the_baseline_stay_on_it(self) -> None:
        content = text_at(72, 700, "React") + text_at(102, 701.5, "2") + text_at(110, 700, "years")
        assert len(_lines(page_pdf(content))[0]) == 1

    def test_a_character_drawn_twice_is_read_once(self) -> None:
        content = text_at(72, 700, "Nordvale") + text_at(72.15, 700, "Nordvale")
        assert _lines(page_pdf(content))[0][0].text == "Nordvale"

    def test_rotated_text_is_counted_not_dropped_silently(self) -> None:
        content = text_at(72, 700, "Jordan A. Rivera") + b"BT /F1 10 Tf 0 1 -1 0 30 300 Tm (RESUME) Tj ET"
        lines, _, rotated = _lines(page_pdf(content))
        assert [line.text for line in lines] == ["Jordan A. Rivera"]
        assert rotated == len("RESUME")


class TestMarkers:
    @pytest.mark.parametrize("marker", ["\u2022", "-", "\u2013", "*", "o"])
    def test_a_marker_glyph_followed_by_a_space_opens_a_bullet(self, marker: str) -> None:
        line = _lines(page_pdf(text_at(72, 700, f"{marker} Built the platform")))[0][0]
        assert line.marker == marker
        assert line.text == "Built the platform"
        assert line.text_x0 > line.x0

    @pytest.mark.parametrize("text", ["-5% latency", "Re-designed the flow", "on-call rotation"])
    def test_a_hyphen_inside_text_is_not_a_marker(self, text: str) -> None:
        line = _lines(page_pdf(text_at(72, 700, text)))[0][0]
        assert line.marker == "" and line.text == text

    def test_a_small_filled_shape_beside_a_line_is_its_marker(self) -> None:
        content = b"q 0 g 60 702 3 3 re f Q " + text_at(72, 700, "Built the platform")
        line = _lines(page_pdf(content))[0][0]
        assert (
            line.marker == DRAWN_MARKER and line.x0 == pytest.approx(60) and line.text_x0 == pytest.approx(72)
        )

    def test_an_icon_as_large_as_the_text_is_not_a_marker(self) -> None:
        # A phone or envelope icon drawn beside a contact line is about the size of the
        # text; a bullet is well under it. Small enough to pass the absolute size cap,
        # too large for the relative one.
        content = b"q 0 g 60 699 9 9 re f Q " + text_at(72, 700, "jordan.rivera@example.com")
        assert _lines(page_pdf(content))[0][0].marker == ""

    def test_a_shape_far_from_the_text_is_not_its_marker(self) -> None:
        content = b"q 0 g 20 702 3 3 re f Q " + text_at(150, 700, "Built the platform")
        assert _lines(page_pdf(content))[0][0].marker == ""


class TestRules:
    def test_a_thin_rule_just_below_a_heading_belongs_to_it(self) -> None:
        content = text_at(72, 700, "EXPERIENCE", font="F2") + b"q 0 g 72 694 468 0.75 re f Q"
        assert _lines(page_pdf(content))[0][0].rule_below

    def test_a_rule_far_below_is_not_the_line_s(self) -> None:
        content = text_at(72, 700, "EXPERIENCE", font="F2") + b"q 0 g 72 650 468 0.75 re f Q"
        assert not _lines(page_pdf(content))[0][0].rule_below


class TestWeight:
    def test_named_weights_are_used_when_any_font_names_one(self) -> None:
        content = text_at(72, 700, "Nordvale Consulting", font="F2") + text_at(72, 680, "Built the platform")
        lines, source, _ = _lines(page_pdf(content))
        assert source is WeightSource.FONT_NAME
        assert [line.bold for line in lines] == [True, False]

    def test_ink_decides_when_no_font_name_says(self) -> None:
        # One font, named without a weight; one line drawn faux-bold by stroking.
        fonts = {"F3": "Helvetica"}
        content = (
            text_at(72, 720, "Nordvale Consulting Software", font="F3", size=11)
            + text_at(72, 690, "Nordvale Consulting Software", font="F3", size=11, state=b"2 Tr 0.45 w")
            + text_at(72, 660, "Nordvale Consulting Software", font="F3", size=11, state=b"0 Tr")
            + text_at(72, 630, "Built fifteen checkout screens", font="F3", size=11)
        )
        lines, source, _ = _lines(page_pdf(content, fonts))
        assert source is WeightSource.GLYPH_DENSITY
        assert [line.bold for line in lines] == [False, True, False, False]

    def test_without_ink_or_names_weight_is_unknown_and_never_guessed_bold(self) -> None:
        fonts = {"F3": "Helvetica"}
        content = text_at(72, 700, "Nordvale Consulting", font="F3") + text_at(72, 680, "Built it", font="F3")
        lines, source, _ = _lines(page_pdf(content, fonts), ink=False)
        assert source is WeightSource.UNKNOWN
        assert not any(line.bold for line in lines)
