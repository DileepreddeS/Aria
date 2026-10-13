"""Line classification from visual signals (RESUME_ENGINE §5.3), on layouts built in code."""

from __future__ import annotations

from typing import Any

from aria_core.documents import LayoutDocument, WeightSource
from aria_core.importer.classify import ClassifiedLine, LineClass, classify
from aria_core.importer.sections import SectionKind, section_for_heading

BODY = 10.0


def _line(
    text: str,
    *,
    top: float,
    x0: float = 36.0,
    size: float = BODY,
    bold: bool = False,
    marker: str = "",
    rule_below: bool = False,
    right: tuple[str, float] | None = None,
    weight_source: WeightSource = WeightSource.FONT_NAME,
    first_bold: bool | None = None,
) -> dict[str, Any]:
    text_x0 = x0 + (12.0 if marker else 0.0)
    left_end = text_x0 + 5.0 * len(text)
    segments = [
        {
            "text": text,
            "x0": text_x0,
            "x1": left_end,
            "font_size": size,
            "bold": bold if first_bold is None else first_bold,
        }
    ]
    full = text
    x1 = left_end
    if right is not None:
        words, at = right
        segments.append(
            {"text": words, "x0": at, "x1": at + 5.0 * len(words), "font_size": size, "bold": False}
        )
        full = f"{text} {words}"
        x1 = at + 5.0 * len(words)
    return {
        "page": 1,
        "column": 0,
        "x0": x0,
        "x1": x1,
        "top": top,
        "bottom": top + size,
        "text_x0": text_x0,
        "text": full,
        "marker": marker,
        "font_size": size,
        "bold": bold,
        "weight_source": weight_source,
        "segments": segments,
        "rule_below": rule_below,
    }


def _classify(*lines: dict[str, Any]) -> list[ClassifiedLine]:
    numbered = [{**line, "index": index} for index, line in enumerate(lines)]
    document = LayoutDocument.model_validate(
        {
            "triage": "readable",
            "cross_extractor": "agree",
            "pages": [
                {
                    "number": 1,
                    "width": 612.0,
                    "height": 792.0,
                    "character_count": 2000,
                    "unreadable_share": 0.0,
                    "body_size": BODY,
                }
            ],
            "lines": numbered,
        },
        strict=False,
    )
    return classify(document)


def _kinds(*lines: dict[str, Any]) -> list[LineClass]:
    return [line.kind for line in _classify(*lines)]


# A body of ordinary full-width lines, so the modal size is the body size and the
# content runs margin to margin as on a real page — the date band is measured from it.
FILLER = [
    _line(
        f"Ordinary body text line {number} " + "carrying the width of a real page " * 3,
        top=600.0 + 14 * number,
    )
    for number in range(8)
]


class TestSectionHeaders:
    def test_a_known_heading_in_capitals_with_a_rule(self) -> None:
        found = _classify(_line("EXPERIENCE", top=100.0, bold=True, rule_below=True), *FILLER)[0]
        assert found.kind is LineClass.SECTION_HEADER and found.section is SectionKind.EXPERIENCE

    def test_a_heading_in_another_language_resolves_by_its_look(self) -> None:
        found = _classify(_line("BERUFSERFAHRUNG", top=100.0, bold=True), *FILLER)[0]
        assert found.kind is LineClass.SECTION_HEADER and found.section is SectionKind.UNKNOWN

    def test_title_case_with_a_rule_beneath_is_a_heading(self) -> None:
        found = _classify(_line("Work History", top=100.0, bold=True, rule_below=True), *FILLER)[0]
        assert found.kind is LineClass.SECTION_HEADER and found.section is SectionKind.EXPERIENCE

    def test_a_larger_size_stands_in_for_bold(self) -> None:
        found = _classify(_line("PROJECTS", top=100.0, size=12.0), *FILLER)[0]
        assert found.kind is LineClass.SECTION_HEADER

    def test_the_words_alone_do_not_make_a_heading(self) -> None:
        # The vocabulary only raises confidence: "Experience" in body type is text.
        assert _kinds(_line("Experience", top=100.0), *FILLER)[0] is LineClass.TEXT

    def test_a_long_bold_line_is_not_a_heading(self) -> None:
        line = _line("SELECTED EXPERIENCE ACROSS PLATFORM AND PAYMENTS TEAMS", top=100.0, bold=True)
        assert _kinds(line, *FILLER)[0] is not LineClass.SECTION_HEADER

    def test_a_heading_that_rests_on_measured_weight_alone_is_less_certain(self) -> None:
        line = _line("EXPERIENCE", top=100.0, bold=True, weight_source=WeightSource.GLYPH_DENSITY)
        found = _classify(line, *FILLER)[0]
        assert found.kind is LineClass.SECTION_HEADER and not found.certain


class TestEntryHeaders:
    def test_a_bold_name_with_a_separator_and_a_date_in_the_right_band(self) -> None:
        line = _line(
            "Nordvale Consulting \u2014 Software Engineer",
            top=100.0,
            bold=True,
            right=("Feb 2024 \u2013 Jul 2025", 480.0),
        )
        found = _classify(line, *FILLER)[0]
        assert found.kind is LineClass.ENTRY_HEADER and found.date_text == "Feb 2024 \u2013 Jul 2025"

    def test_a_date_in_the_left_half_is_not_taken(self) -> None:
        line = _line(
            "Nordvale Consulting \u2014 Software Engineer", top=100.0, bold=True, right=("Feb 2024", 250.0)
        )
        found = _classify(line, *FILLER)[0]
        assert found.kind is LineClass.ENTRY_HEADER and found.date_text == ""

    def test_a_bold_line_with_neither_separator_nor_date_is_not_an_entry(self) -> None:
        assert _kinds(_line("Selected highlights", top=100.0, bold=True), *FILLER)[0] is LineClass.TEXT

    def test_a_plain_line_with_a_separator_is_not_an_entry(self) -> None:
        assert (
            _kinds(_line("Client: Autolane \u2014 retail platform", top=100.0), *FILLER)[0] is LineClass.TEXT
        )

    def test_only_the_first_part_needs_to_be_bold(self) -> None:
        line = _line("ORBIT \u2014 Job Agent", top=100.0, bold=False, first_bold=True)
        assert _kinds(line, *FILLER)[0] is LineClass.ENTRY_HEADER


class TestBulletsAndTheirTails:
    def test_a_marked_line_is_a_bullet_and_its_aligned_tail_continues_it(self) -> None:
        bullet = _line("Built 15+ React screens for the checkout platform", top=100.0, marker="\u2022")
        tail = _line("mobile", top=112.0, x0=48.0)
        assert _kinds(bullet, tail, *FILLER)[:2] == [LineClass.BULLET, LineClass.CONTINUATION]

    def test_a_tail_far_below_is_not_a_continuation(self) -> None:
        bullet = _line("Built 15+ React screens", top=100.0, marker="\u2022")
        later = _line("mobile", top=160.0, x0=48.0)
        assert _kinds(bullet, later, *FILLER)[1] is not LineClass.CONTINUATION

    def test_a_tail_at_another_indent_is_not_a_continuation(self) -> None:
        bullet = _line("Built 15+ React screens", top=100.0, marker="\u2022")
        elsewhere = _line("Portland, OR", top=112.0, x0=36.0)
        assert _kinds(bullet, elsewhere, *FILLER)[1] is not LineClass.CONTINUATION

    def test_a_hanging_indent_without_any_marker(self) -> None:
        header = _line("Nordvale Consulting \u2014 Software Engineer", top=88.0, bold=True)
        first = _line("Built 15+ React screens for the vehicle checkout platform", top=102.0, x0=36.0)
        wrap = _line("shipping weekly releases", top=114.0, x0=48.0)
        second = _line("Cut defect rates by 30% across microservices", top=128.0, x0=36.0)
        kinds = _kinds(header, first, wrap, second, *FILLER)
        assert kinds[:3] == [LineClass.ENTRY_HEADER, LineClass.BULLET, LineClass.CONTINUATION]

    # The geometry of no_marker_bullets.pdf: entry edge at 36, bullets at 40, wraps at 49.
    def test_a_one_line_bullet_at_the_proven_marker_column_is_a_bullet(self) -> None:
        header = _line("Nordvale Consulting \u2014 Software Engineer", top=88.0, bold=True)
        place = _line("Pune, India", top=100.0, x0=36.0)
        first = _line("Built 15+ React screens for the vehicle checkout platform", top=112.0, x0=40.0)
        wrap = _line("mobile", top=124.0, x0=49.0)
        single = _line("Led 15+ code reviews that cut post-release bugs by 28%", top=136.0, x0=40.0)
        kinds = _kinds(header, place, first, wrap, single, *FILLER)
        assert kinds[:5] == [
            LineClass.ENTRY_HEADER,
            LineClass.TEXT,
            LineClass.BULLET,
            LineClass.CONTINUATION,
            LineClass.BULLET,
        ]

    def test_an_entry_where_nothing_wraps_uses_the_column_proven_elsewhere(self) -> None:
        proven = [
            _line("Nordvale Consulting \u2014 Software Engineer", top=88.0, bold=True),
            _line("Built 15+ React screens for the vehicle checkout platform", top=100.0, x0=40.0),
            _line("mobile", top=112.0, x0=49.0),
        ]
        header = _line("Brightfold Systems \u2014 Associate Software Engineer", top=130.0, bold=True)
        place = _line("India", top=142.0, x0=36.0)
        bullets = [
            _line("Built 8 full-stack applications with React and Node", top=154.0, x0=40.0),
            _line("Added server-side rendering, cutting TTFB by 45%", top=166.0, x0=40.0),
        ]
        kinds = _kinds(*proven, header, place, *bullets, *FILLER)
        assert kinds[3:7] == [LineClass.ENTRY_HEADER, LineClass.TEXT, LineClass.BULLET, LineClass.BULLET]

    def test_without_a_proven_column_an_indented_line_is_not_a_bullet(self) -> None:
        header = _line("Brightfold Systems \u2014 Associate Software Engineer", top=88.0, bold=True)
        single = _line("Built 8 full-stack applications with React and Node", top=100.0, x0=40.0)
        assert _kinds(header, single, *FILLER)[:2] == [LineClass.ENTRY_HEADER, LineClass.TEXT]

    def test_a_marker_column_flush_with_the_entry_proves_nothing(self) -> None:
        # Bullets flush with the header: the wrap proves its own bullet, but a one-line
        # bullet at the same x cannot be told from a location line (§5.9 measures this).
        header = _line("Nordvale Consulting \u2014 Software Engineer", top=88.0, bold=True)
        first = _line("Built 15+ React screens for the vehicle checkout platform", top=100.0, x0=36.0)
        wrap = _line("mobile", top=112.0, x0=48.0)
        single = _line("Pune, India", top=124.0, x0=36.0)
        kinds = _kinds(header, first, wrap, single, *FILLER)
        assert kinds[:4] == [LineClass.ENTRY_HEADER, LineClass.BULLET, LineClass.CONTINUATION, LineClass.TEXT]

    def test_outside_an_entry_an_indent_is_not_read_as_a_bullet(self) -> None:
        first = _line("A summary sentence about the candidate", top=102.0, x0=36.0)
        wrap = _line("and what they want next", top=114.0, x0=48.0)
        assert _kinds(first, wrap, *FILLER)[:2] == [LineClass.TEXT, LineClass.TEXT]


class TestTheOpeningLines:
    def test_name_then_contact(self) -> None:
        name = _line("JORDAN A. RIVERA", top=40.0, size=17.5, bold=True)
        contact = _line("(555) 014-2288 | jordan.rivera@example.com", top=62.0)
        assert _kinds(name, contact, *FILLER)[:2] == [LineClass.NAME, LineClass.CONTACT]

    def test_contact_details_further_down_are_not_contact_lines(self) -> None:
        heading = _line("EXPERIENCE", top=100.0, bold=True)
        later = _line("Reach the team at jobs@example.com", top=120.0)
        assert _kinds(heading, later, *FILLER)[1] is LineClass.TEXT


def test_the_heading_vocabulary_folds_case_and_punctuation() -> None:
    assert section_for_heading("Work Experience:") is SectionKind.EXPERIENCE
    assert section_for_heading("SKILLS & TOOLS") is SectionKind.SKILLS
    assert section_for_heading("Berufserfahrung") is None
