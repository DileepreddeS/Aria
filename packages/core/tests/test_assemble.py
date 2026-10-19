"""Entry assembly from classified lines (RESUME_ENGINE §5.4), on layouts built in code."""

from __future__ import annotations

import datetime as dt
from typing import Any

from aria_core.documents import LayoutDocument
from aria_core.graph.months import MonthSpan
from aria_core.graph.status import ProfileFieldKind
from aria_core.importer.assemble import parse_resume_layout
from aria_core.importer.plan import ImportPlan
from aria_core.taxonomy.loader import load_default_taxonomy

BODY = 10.0
TODAY = dt.date(2026, 10, 12)


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
) -> dict[str, Any]:
    text_x0 = x0 + (12.0 if marker else 0.0)
    x1 = text_x0 + 5.0 * len(text)
    segments = [{"text": text, "x0": text_x0, "x1": x1, "font_size": size, "bold": bold}]
    full = text
    if right is not None:
        words, at = right
        x1 = at + 5.0 * len(words)
        segments.append({"text": words, "x0": at, "x1": x1, "font_size": size, "bold": False})
        full = f"{text} {words}"
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
        "weight_source": "font_name",
        "segments": segments,
        "rule_below": rule_below,
    }


def _plan(*lines: dict[str, Any]) -> ImportPlan:
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
            "lines": [{**line, "index": index} for index, line in enumerate(lines)],
        },
        strict=False,
    )
    return parse_resume_layout(document, load_default_taxonomy(), today=TODAY)


HEADER = [
    _line("JORDAN A. RIVERA", top=40.0, size=18.0, bold=True),
    _line("jordan.rivera@example.com | Portland, OR", top=62.0),
]
ROLE = [
    _line("EXPERIENCE", top=90.0, bold=True, rule_below=True),
    _line("Nordvale Consulting", top=110.0, bold=True, right=("Feb 2024 - Jul 2025", 480.0)),
    _line("Software Engineer", top=124.0),
    _line("Reduced the nightly batch from six hours to forty minutes by", top=138.0, marker="*"),
    _line("moving the joins into the warehouse", top=150.0, x0=48.0),
    _line("Built a deployment pipeline that cut release time in half", top=164.0, marker="*"),
]


def test_a_role_is_read_from_its_header_its_title_and_its_bullets() -> None:
    (role,) = _plan(*HEADER, *ROLE).experiences
    assert (role.employer, role.title) == ("Nordvale Consulting", "Software Engineer")
    assert role.span == MonthSpan(start=dt.date(2024, 2, 1), end=dt.date(2025, 7, 1))
    assert [fact.text for fact in role.facts] == [
        "Reduced the nightly batch from six hours to forty minutes by moving the joins into the warehouse",
        "Built a deployment pipeline that cut release time in half",
    ]


def test_a_bold_employer_and_a_regular_title_are_both_read() -> None:
    # The header as most resumes set it: the employer bold, the title regular after the
    # dash. Split by weight into two touching parts, and the date further right.
    head = _line("Brightfold Systems", top=110.0, bold=True, right=("Jul 2022 - Dec 2023", 480.0))
    title = {
        "text": "\u2014 Associate Software Engineer",
        "x0": 129.0,
        "x1": 280.0,
        "font_size": BODY,
        "bold": False,
    }
    segments = [head["segments"][0], title, head["segments"][1]]
    head = {**head, "segments": segments, "bold": False, "text": " ".join(str(s["text"]) for s in segments)}
    plan = _plan(
        *HEADER,
        _line("EXPERIENCE", top=90.0, bold=True, rule_below=True),
        head,
        _line("Shipped 8 full-stack applications with Node.js and PostgreSQL", top=124.0, marker="*"),
    )
    (role,) = plan.experiences
    assert (role.employer, role.title) == ("Brightfold Systems", "Associate Software Engineer")
    assert len(role.facts) == 1


def test_each_bullet_keeps_the_line_it_started_on() -> None:
    (role,) = _plan(*HEADER, *ROLE).experiences
    assert [fact.source_line for fact in role.facts] == [6, 8]


def test_the_lines_above_the_first_section_are_the_contact_header() -> None:
    fields = {field.kind: field.value for field in _plan(*HEADER, *ROLE).profile_fields}
    assert fields[ProfileFieldKind.FULL_NAME] == "JORDAN A. RIVERA"
    assert fields[ProfileFieldKind.EMAIL] == "jordan.rivera@example.com"
    assert (fields[ProfileFieldKind.LOCATION_CITY], fields[ProfileFieldKind.LOCATION_REGION]) == (
        "Portland",
        "OR",
    )


def test_an_education_header_is_split_into_school_and_degree() -> None:
    plan = _plan(
        *HEADER,
        _line("EDUCATION", top=90.0, bold=True, rule_below=True),
        _line(
            "Cascadia State University \u2014 M.S. Computer Science",
            top=110.0,
            bold=True,
            right=("Dec 2026", 540.0),
        ),
    )
    (education,) = plan.educations
    assert (education.school, education.degree, education.date_text) == (
        "Cascadia State University",
        "M.S. Computer Science",
        "Dec 2026",
    )


def test_an_unknown_section_is_reported_and_asked_about_not_imported() -> None:
    plan = _plan(
        *HEADER,
        *ROLE,
        _line("BERUFSERFAHRUNG", top=240.0, bold=True, rule_below=True),
        _line("Something in another section entirely here", top=256.0),
    )
    assert len(plan.experiences) == 1
    assert [(item.first_line, item.last_line) for item in plan.unparsed] == [(10, 10)]
    assert any("BERUFSERFAHRUNG" in question.prompt for question in plan.questions)


def test_text_before_the_first_entry_of_a_section_is_reported_not_guessed_at() -> None:
    plan = _plan(
        *HEADER,
        _line("EXPERIENCE", top=90.0, bold=True, rule_below=True),
        _line("Selected roles, most recent first", top=104.0),
        *[{**line, "top": line["top"] + 14.0, "bottom": line["bottom"] + 14.0} for line in ROLE[1:]],
    )
    assert [role.employer for role in plan.experiences] == ["Nordvale Consulting"]
    assert [(item.first_line, item.reason) for item in plan.unparsed] == [
        (4, "not part of any entry in its section")
    ]


def test_a_project_named_in_capitals_is_a_project_with_its_bullets() -> None:
    # Every heading on the page carries a rule; the project's name, in capitals, does not.
    # Read as a heading it took its bullet into an unknown section (S0-24).
    plan = _plan(
        *HEADER,
        _line("PROJECTS", top=90.0, bold=True, rule_below=True),
        _line("VERA-RAG", top=110.0, bold=True),
        _line("Built a closed-loop RAG system with hybrid retrieval", top=124.0, marker="*"),
    )
    (project,) = plan.projects
    assert project.name == "VERA-RAG"
    assert len(project.facts) == 1
    assert plan.unparsed == ()
