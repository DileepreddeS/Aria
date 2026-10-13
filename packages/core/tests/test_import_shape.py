"""Shape rules and refusal gates (RESUME_ENGINE §5.5): one test per rule and per gate."""

from __future__ import annotations

import datetime as dt

import pytest

from aria_core.graph.months import MonthSpan
from aria_core.importer.plan import (
    ImportPlan,
    ProposedEducation,
    ProposedExperience,
    ProposedFact,
    ProposedProject,
)
from aria_core.importer.shape import IntakeStatus, assess

TODAY = dt.date(2026, 10, 12)
LONG = "Reduced the nightly batch from six hours to forty minutes"


def _fact(line: int, text: str = LONG) -> ProposedFact:
    return ProposedFact(text=text, source_line=line)


def _role(
    line: int,
    employer: str = "Nordvale Consulting",
    title: str = "Software Engineer",
    *,
    facts: tuple[ProposedFact, ...] | None = None,
    span: MonthSpan | None = None,
) -> ProposedExperience:
    return ProposedExperience(
        employer=employer,
        title=title,
        source_line=line,
        facts=(_fact(line + 1),) if facts is None else facts,
        span=span,
    )


def _span(start: str, end: str | None = None) -> MonthSpan:
    def month(text: str) -> dt.date:
        year, number = text.split("-")
        return dt.date(int(year), int(number), 1)

    return MonthSpan(start=month(start), end=month(end) if end else None)


def _roles(count: int) -> list[ProposedExperience]:
    return [_role(index * 10, employer=f"Employer {index}") for index in range(count)]


def test_a_well_formed_plan_is_ready_and_unchanged() -> None:
    plan = ImportPlan(experiences=tuple(_roles(3)))
    verdict = assess(plan, today=TODAY)
    assert verdict.status is IntakeStatus.READY
    assert verdict.plan == plan
    assert verdict.reasons == ()
    assert verdict.dropped_entries == ()
    assert verdict.dropped_bullets == ()


def test_a_short_bullet_is_dropped_and_its_role_kept() -> None:
    role = _role(0, facts=(_fact(1), _fact(2, "Used Python")))
    verdict = assess(ImportPlan(experiences=(role,)), today=TODAY)
    assert verdict.status is IntakeStatus.READY
    assert [fact.text for fact in verdict.plan.experiences[0].facts] == [LONG]
    assert [bullet.source_line for bullet in verdict.dropped_bullets] == [2]
    assert verdict.dropped_entries == ()


def test_a_bullet_of_exactly_twenty_characters_is_kept() -> None:
    role = _role(0, facts=(_fact(1, "x" * 20),))
    assert assess(ImportPlan(experiences=(role,)), today=TODAY).dropped_bullets == ()


def test_a_short_project_bullet_is_dropped_too() -> None:
    project = ProposedProject(name="ORBIT", source_line=0, facts=(_fact(1), _fact(2, "Fun")))
    verdict = assess(ImportPlan(projects=(project,)), today=TODAY)
    assert [fact.source_line for fact in verdict.plan.projects[0].facts] == [1]
    assert [bullet.source_line for bullet in verdict.dropped_bullets] == [2]


def test_a_role_whose_only_bullet_was_too_short_is_dropped() -> None:
    roles = [*_roles(3), _role(90, employer="Pinecrest Labs", facts=(_fact(91, "Did things"),))]
    verdict = assess(ImportPlan(experiences=tuple(roles)), today=TODAY)
    assert verdict.status is IntakeStatus.READY
    assert [entry.label for entry in verdict.dropped_entries] == ["Pinecrest Labs \u2014 Software Engineer"]
    assert "Pinecrest Labs" not in {role.employer for role in verdict.plan.experiences}


@pytest.mark.parametrize(
    ("employer", "reason"),
    [
        ("A" * 61, "longer than 60"),
        ("One Two Three Four Five Six Seven Eight Nine", "more than 8 words"),
        ("Cut the release cycle in half.", "sentence"),
        ("Grew revenue 40%", "metric"),
        ("Scaled to 10K users", "metric"),
        ("Built the payments service", "verb"),
        ("", "no employer"),
    ],
)
def test_an_employer_without_the_shape_of_one_drops_the_role(employer: str, reason: str) -> None:
    roles = [*_roles(3), _role(90, employer=employer)]
    verdict = assess(ImportPlan(experiences=tuple(roles)), today=TODAY)
    assert verdict.status is IntakeStatus.READY
    assert len(verdict.dropped_entries) == 1
    assert reason in verdict.dropped_entries[0].reason
    assert len(verdict.plan.experiences) == 3


@pytest.mark.parametrize("employer", ["E" * 60, "One Two Three Four Five Six Seven Eight"])
def test_an_employer_at_the_limits_is_kept(employer: str) -> None:
    assert assess(ImportPlan(experiences=(_role(0, employer=employer),)), today=TODAY).dropped_entries == ()


@pytest.mark.parametrize(
    ("title", "reason"),
    [("T" * 61, "longer than 60"), ("Led a team of 12+ engineers", "metric"), ("Engineer.", "sentence")],
)
def test_a_title_without_the_shape_of_one_drops_the_role(title: str, reason: str) -> None:
    roles = [*_roles(3), _role(90, title=title)]
    verdict = assess(ImportPlan(experiences=tuple(roles)), today=TODAY)
    assert len(verdict.dropped_entries) == 1
    assert reason in verdict.dropped_entries[0].reason


def test_a_project_name_that_reads_like_a_bullet_drops_the_project() -> None:
    projects = [
        ProposedProject(name=name, source_line=index * 10, facts=(_fact(index * 10 + 1),))
        for index, name in enumerate(["ORBIT", "DentScan", "VERA-RAG", "Designed a crawler for job boards"])
    ]
    verdict = assess(ImportPlan(projects=tuple(projects)), today=TODAY)
    assert verdict.status is IntakeStatus.READY
    assert [entry.kind for entry in verdict.dropped_entries] == ["project"]
    assert [project.name for project in verdict.plan.projects] == ["ORBIT", "DentScan", "VERA-RAG"]


def test_a_start_before_1950_drops_the_role() -> None:
    roles = [*_roles(3), _role(90, span=_span("1949-12", "1952-01"))]
    verdict = assess(ImportPlan(experiences=tuple(roles)), today=TODAY)
    assert "before 1950" in verdict.dropped_entries[0].reason


def test_a_start_in_1950_is_kept() -> None:
    verdict = assess(ImportPlan(experiences=(_role(0, span=_span("1950-01", "1952-01")),)), today=TODAY)
    assert verdict.dropped_entries == ()


def test_a_start_more_than_a_year_ahead_is_flagged_not_dropped() -> None:
    verdict = assess(ImportPlan(experiences=(_role(0, span=_span("2027-11")),)), today=TODAY)
    assert verdict.status is IntakeStatus.READY
    assert len(verdict.plan.experiences) == 1
    assert len(verdict.flags) == 1 and "more than a year ahead" in verdict.flags[0]


def test_a_start_exactly_a_year_ahead_is_not_flagged() -> None:
    verdict = assess(ImportPlan(experiences=(_role(0, span=_span("2027-10")),)), today=TODAY)
    assert verdict.flags == ()


def test_a_role_with_no_bullets_is_dropped() -> None:
    roles = [*_roles(3), _role(90, facts=())]
    verdict = assess(ImportPlan(experiences=tuple(roles)), today=TODAY)
    assert verdict.status is IntakeStatus.READY
    assert verdict.dropped_entries[0].reason == "a role with no bullets under it"


# Document gates. A refused plan is handed back exactly as it was assembled.


def _refused(plan: ImportPlan, reason: str) -> None:
    verdict = assess(plan, today=TODAY)
    assert verdict.status is IntakeStatus.NEEDS_USER_FIX
    assert verdict.plan is plan
    assert any(reason in text for text in verdict.reasons), verdict.reasons


def test_two_consecutive_roles_without_bullets_refuse_the_document() -> None:
    roles = [*_roles(9), _role(200, facts=()), _role(210, facts=())]
    _refused(ImportPlan(experiences=tuple(roles)), "in a row")


def test_consecutive_is_by_position_on_the_page_not_by_list_order() -> None:
    roles = [_role(210, facts=()), *_roles(9), _role(200, facts=())]
    _refused(ImportPlan(experiences=tuple(roles)), "in a row")


def test_two_roles_without_bullets_apart_from_each_other_are_only_dropped() -> None:
    roles = _roles(9)
    roles[2] = _role(20, facts=())
    roles[6] = _role(60, facts=())
    verdict = assess(ImportPlan(experiences=tuple(roles)), today=TODAY)
    assert verdict.status is IntakeStatus.READY
    assert len(verdict.dropped_entries) == 2


def test_degrees_without_bullets_are_normal() -> None:
    educations = tuple(
        ProposedEducation(school=f"School {index}", degree="B.S.", source_line=index) for index in range(2)
    )
    verdict = assess(ImportPlan(educations=educations, experiences=tuple(_roles(2))), today=TODAY)
    assert verdict.status is IntakeStatus.READY


def test_three_entries_and_no_facts_refuse_the_document() -> None:
    educations = tuple(
        ProposedEducation(school=f"School {index}", degree="B.S.", source_line=index) for index in range(3)
    )
    _refused(ImportPlan(educations=educations), "not one achievement")


def test_two_entries_and_no_facts_are_not_refused() -> None:
    educations = tuple(
        ProposedEducation(school=f"School {index}", degree="B.S.", source_line=index) for index in range(2)
    )
    assert assess(ImportPlan(educations=educations), today=TODAY).status is IntakeStatus.READY


def test_loose_facts_count_as_facts() -> None:
    educations = tuple(
        ProposedEducation(school=f"School {index}", degree="B.S.", source_line=index) for index in range(3)
    )
    plan = ImportPlan(educations=educations, loose_facts=(_fact(50),))
    assert assess(plan, today=TODAY).status is IntakeStatus.READY


def test_more_than_a_quarter_of_entries_failing_refuse_the_document() -> None:
    roles = [*_roles(2), _role(80, employer="Built it all."), _role(90, employer="Grew sales 30%")]
    _refused(ImportPlan(experiences=tuple(roles)), "2 of 4 entries")


def test_exactly_a_quarter_failing_is_not_refused() -> None:
    roles = [*_roles(3), _role(90, employer="Built it all.")]
    assert assess(ImportPlan(experiences=tuple(roles)), today=TODAY).status is IntakeStatus.READY


def test_more_than_three_overlapping_pairs_refuse_the_document() -> None:
    # A, B and C overlap each other (three pairs); D overlaps C only (a fourth).
    roles = [
        _role(0, employer="A", span=_span("2020-01", "2021-12")),
        _role(10, employer="B", span=_span("2020-06", "2021-06")),
        _role(20, employer="C", span=_span("2021-01", "2022-06")),
        _role(30, employer="D", span=_span("2022-03", "2023-01")),
    ]
    _refused(ImportPlan(experiences=tuple(roles)), "4 pairs")


def test_three_overlapping_pairs_are_not_refused() -> None:
    roles = [
        _role(0, employer="A", span=_span("2020-01", "2021-12")),
        _role(10, employer="B", span=_span("2020-06", "2021-06")),
        _role(20, employer="C", span=_span("2021-01", "2022-06")),
        _role(30, employer="D", span=_span("2022-07", "2023-01")),
    ]
    assert assess(ImportPlan(experiences=tuple(roles)), today=TODAY).status is IntakeStatus.READY


def test_a_current_role_overlaps_up_to_today() -> None:
    roles = [
        _role(0, employer="A", span=_span("2025-01")),
        _role(10, employer="B", span=_span("2025-06", "2025-09")),
        _role(20, employer="C", span=_span("2026-01", "2026-03")),
        _role(30, employer="D", span=_span("2026-05", "2026-08")),
        _role(40, employer="E", span=_span("2026-09", "2026-10")),
    ]
    _refused(ImportPlan(experiences=tuple(roles)), "4 pairs")
