"""The cross-extractor check compares line order, not token counts (RESUME_ENGINE §5.6)."""

from __future__ import annotations

from aria_core.documents import CrossExtractor
from aria_document_parser.crosscheck import compare

LINES = [
    "Nordvale Consulting - Software Engineer",
    "Built 15+ React screens for the checkout platform",
    "Cut defect rates by 30% across distributed microservices",
    "Brightfold Systems - Associate Software Engineer",
    "Built 8 full-stack applications with event-driven flows",
    "Added server-side rendering, cutting TTFB by 45%",
]


def test_the_same_lines_in_the_same_order_agree() -> None:
    assert compare(LINES, "\n".join(LINES)) is CrossExtractor.AGREE


def test_spacing_case_and_punctuation_do_not_matter() -> None:
    other = "\n".join(line.upper().replace(" ", "") for line in LINES)
    assert compare(LINES, other) is CrossExtractor.AGREE


def test_the_same_words_in_another_order_disagree() -> None:
    # What a token multiset cannot see: every word is present, the headers are not
    # where their bodies are.
    headers_first = [LINES[0], LINES[3], LINES[1], LINES[2], LINES[4], LINES[5]]
    assert compare(LINES, "\n".join(headers_first)) is CrossExtractor.DISAGREE


def test_text_missing_from_the_other_reading_disagrees() -> None:
    assert compare(LINES, "\n".join(LINES[:4])) is CrossExtractor.DISAGREE


def test_text_only_the_other_reading_has_disagrees() -> None:
    extra = "\n".join(LINES) + "\n" + "Hidden text drawn under the page in white " * 6
    assert compare(LINES, extra) is CrossExtractor.DISAGREE


def test_a_repeated_line_is_matched_where_the_order_expects_it() -> None:
    repeated = [*LINES[:3], "Portland, Oregon, United States", *LINES[3:], "Portland, Oregon, United States"]
    assert compare(repeated, "\n".join(repeated)) is CrossExtractor.AGREE


def test_lines_too_short_to_locate_are_not_counted_against_either_reading() -> None:
    with_short = [*LINES, "OR", "2024"]
    assert compare(with_short, "\n".join(LINES)) is CrossExtractor.AGREE


def test_the_verdict_is_a_word_and_never_a_number() -> None:
    verdict = compare(LINES, "\n".join(LINES))
    assert isinstance(verdict, CrossExtractor)
    assert {member.value for member in CrossExtractor} == {"agree", "disagree"}
