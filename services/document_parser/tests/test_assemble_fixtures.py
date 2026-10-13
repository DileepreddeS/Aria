"""Entry assembly and the §5.5 gates on the four gate fixtures, against their truth files.

The layout path must read every fixture exactly and pass every gate. The flat text of
the two hard fixtures — what the old importer read — must be refused, not imported
scrambled: that is what the gates are for.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from functools import cache
from pathlib import Path
from typing import Any

import pypdf
import pytest

from aria_core.graph.status import ProfileFieldKind
from aria_core.importer.assemble import parse_resume_layout
from aria_core.importer.dates import parse_date_range
from aria_core.importer.plan import ImportPlan
from aria_core.importer.resume import parse_resume_text
from aria_core.importer.shape import IntakeStatus, IntakeVerdict, assess
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_document_parser.pdf import extract_pdf

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "resumes"
GATE = ["clean_inline_block", "float_scrambled", "markerless_bullets", "per_glyph_contact"]
TODAY = dt.date(2026, 10, 12)


def _truth(name: str) -> dict[str, Any]:
    truth: dict[str, Any] = json.loads((FIXTURES / f"{name}.truth.json").read_text(encoding="utf-8"))
    return truth


def _entries(name: str, section: str) -> list[dict[str, Any]]:
    return [entry for entry in _truth(name)["entries"] if entry["section"] == section]


@cache
def _verdict(name: str) -> IntakeVerdict:
    layout = extract_pdf((FIXTURES / f"{name}.pdf").read_bytes()).layout
    plan = parse_resume_layout(layout, load_default_taxonomy(), today=TODAY)
    return assess(plan, today=TODAY)


def _plan(name: str) -> ImportPlan:
    return _verdict(name).plan


def _month(value: dt.date | None) -> str:
    return "present" if value is None else value.isoformat()[:7]


@pytest.mark.parametrize("name", GATE)
def test_the_layout_reading_passes_every_gate_and_drops_nothing(name: str) -> None:
    verdict = _verdict(name)
    assert verdict.status is IntakeStatus.READY, verdict.reasons
    assert verdict.dropped_entries == ()
    assert verdict.dropped_bullets == ()
    assert verdict.flags == ()
    assert _plan(name).unparsed == ()


@pytest.mark.parametrize("name", GATE)
def test_every_role_is_read_exactly(name: str) -> None:
    got = [
        (
            role.employer,
            role.title,
            _month(role.span.start) if role.span else None,
            _month(role.span.end) if role.span else None,
            len(role.facts),
        )
        for role in _plan(name).experiences
    ]
    want = [
        (entry["employer"], entry["title"], entry["start"], entry["end"], entry["bullet_count"])
        for entry in _entries(name, "experience")
    ]
    assert got == want


@pytest.mark.parametrize("name", GATE)
def test_every_degree_is_read_with_its_date(name: str) -> None:
    educations = _plan(name).educations
    want = _entries(name, "education")
    assert [(item.school, item.degree) for item in educations] == [
        (entry["employer"], entry["title"]) for entry in want
    ]
    for item, entry in zip(educations, want, strict=True):
        dates = parse_date_range(item.date_text)
        assert dates is not None and dates.start.month is not None, item.date_text
        assert dates.start.month.isoformat()[:7] == entry["end"]
        assert len(item.facts) == entry["bullet_count"]


@pytest.mark.parametrize("name", GATE)
def test_every_project_is_read_with_its_bullets(name: str) -> None:
    projects = _plan(name).projects
    want = _entries(name, "project")
    assert [project.name for project in projects] == [entry["name"] for entry in want]
    for project, entry in zip(projects, want, strict=True):
        # The header's description ("Autonomous Job Application Agent") is kept as a
        # fact on the header's own line; the truth counts the bullets under it.
        header = [fact.text for fact in project.facts if fact.source_line == project.source_line]
        assert header == [entry["title"]]
        bullets = [fact for fact in project.facts if fact.source_line != project.source_line]
        assert len(bullets) == entry["bullet_count"]


@pytest.mark.parametrize("name", GATE)
def test_no_entry_is_proposed_that_the_truth_does_not_hold(name: str) -> None:
    plan = _plan(name)
    proposed = len(plan.experiences) + len(plan.educations) + len(plan.projects) + len(plan.achievements)
    assert proposed == len(_truth(name)["entries"])
    assert plan.loose_facts == ()


@pytest.mark.parametrize("name", GATE)
def test_the_contact_line_is_read_and_nothing_else_is_proposed(name: str) -> None:
    fields = {field.kind: field.value for field in _plan(name).profile_fields}
    assert fields == {
        ProfileFieldKind.FULL_NAME: "JORDAN A. RIVERA",
        ProfileFieldKind.EMAIL: "jordan.rivera@example.com",
        ProfileFieldKind.PHONE: "(555) 014-2288",
        ProfileFieldKind.LINKEDIN_URL: "https://linkedin.com/in/jordan-rivera-example",
        ProfileFieldKind.GITHUB_URL: "https://github.com/jordan-rivera-example",
        ProfileFieldKind.LOCATION_CITY: "Portland",
        ProfileFieldKind.LOCATION_REGION: "OR",
    }


def _flat_text(name: str) -> str:
    reader = pypdf.PdfReader(io.BytesIO((FIXTURES / f"{name}.pdf").read_bytes()))
    return "\n".join(page.extract_text() for page in reader.pages)


@pytest.mark.parametrize(
    ("name", "gate"),
    [
        # Bullets torn from their roles: a third of the entries are bullets read as headers.
        ("float_scrambled", "do not have the shape of an entry"),
        # Every bullet glued to the line above it: eleven roles in a row with none.
        ("markerless_bullets", "in a row have no bullets"),
    ],
)
def test_the_flat_text_of_a_hard_fixture_is_refused_not_imported(name: str, gate: str) -> None:
    # The old importer's reading of these pages scrambled roles and bullets. The same
    # gates that pass the layout reading must refuse it, and for the reason that fits.
    plan = parse_resume_text(_flat_text(name), load_default_taxonomy(), today=TODAY)
    verdict = assess(plan, today=TODAY)
    assert verdict.status is IntakeStatus.NEEDS_USER_FIX
    assert any(gate in reason for reason in verdict.reasons), verdict.reasons
