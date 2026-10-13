"""Classification on the four gate fixtures, against their truth files (RESUME_ENGINE §5.3, §5.9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aria_core.importer.classify import LineClass, classify
from aria_core.importer.dates import DatePrecision, parse_date_range
from aria_document_parser.pdf import extract_pdf

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "resumes"
GATE = [
    "clean_inline_block",
    "float_scrambled",
    "markerless_bullets",
    "no_marker_bullets",
    "per_glyph_contact",
]


def _truth(name: str) -> dict[str, Any]:
    truth: dict[str, Any] = json.loads((FIXTURES / f"{name}.truth.json").read_text(encoding="utf-8"))
    return truth


def _month(text: str) -> str:
    return text if text == "present" else text[:7]


@pytest.mark.parametrize("name", GATE)
def test_every_entry_header_is_found_with_its_name(name: str) -> None:
    classified = classify(extract_pdf((FIXTURES / f"{name}.pdf").read_bytes()).layout)
    headers = [line for line in classified if line.kind is LineClass.ENTRY_HEADER]
    entries = _truth(name)["entries"]
    assert len(headers) == len(entries)
    for header, entry in zip(headers, entries, strict=True):
        assert header.line.segments[0].text.startswith(entry.get("employer") or entry["name"])


@pytest.mark.parametrize("name", GATE)
def test_every_dated_entry_carries_its_dates(name: str) -> None:
    classified = classify(extract_pdf((FIXTURES / f"{name}.pdf").read_bytes()).layout)
    headers = [line for line in classified if line.kind is LineClass.ENTRY_HEADER]
    for header, entry in zip(headers, _truth(name)["entries"], strict=True):
        if "end" not in entry:
            assert header.date_text == ""
            continue
        dates = parse_date_range(header.date_text)
        assert dates is not None, header.line.text
        if "start" in entry:
            assert dates.start.month is not None and dates.start.month.isoformat()[:7] == entry["start"]
            if entry["end"] == "present":
                assert dates.end.precision is DatePrecision.PRESENT
            else:
                assert dates.end.month is not None and dates.end.month.isoformat()[:7] == entry["end"]
        else:
            # A single date, the end: a degree's completion.
            assert dates.start.month is not None and dates.start.month.isoformat()[:7] == entry["end"]


@pytest.mark.parametrize("name", GATE)
def test_bullets_and_sections_match_the_truth(name: str) -> None:
    classified = classify(extract_pdf((FIXTURES / f"{name}.pdf").read_bytes()).layout)
    kinds = [line.kind for line in classified]
    assert kinds.count(LineClass.BULLET) == sum(entry["bullet_count"] for entry in _truth(name)["entries"])
    assert kinds.count(LineClass.NAME) == 1 and kinds.count(LineClass.CONTACT) == 1
    headings = [line.line.text for line in classified if line.kind is LineClass.SECTION_HEADER]
    assert headings == ["EDUCATION", "EXPERIENCE", "PROJECTS", "SKILLS"]


def test_every_fixture_classifies_identically() -> None:
    # The same document rendered five ways: any difference in what a line *is* would be
    # a pathology leaking through. Where a bullet wraps can differ — a bullet without a
    # marker's indent is wider and one of them fits on a single line — so the comparison
    # is of classes with continuations left out, and of entry headers' text, which never
    # wrap.
    readings = [classify(extract_pdf((FIXTURES / f"{name}.pdf").read_bytes()).layout) for name in GATE]
    kinds = [
        [line.kind for line in reading if line.kind is not LineClass.CONTINUATION] for reading in readings
    ]
    headers = [
        [line.line.text for line in reading if line.kind is LineClass.ENTRY_HEADER] for reading in readings
    ]
    assert all(other == kinds[0] for other in kinds[1:])
    assert all(other == headers[0] for other in headers[1:])
