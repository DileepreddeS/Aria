"""A form's years answer never contradicts the resume summary check (DECISIONS 2026-10-27).

The number a form would state passes 6b's summary check as "N+ years", and one more
year fails it, for every shape of roles the count has rules about.
"""

from __future__ import annotations

import datetime as dt

import pytest
from aria_resume.document import DocumentSection, ResumeDocument, SectionKind
from aria_resume.validate.truth import check_truth

from aria_core.graph.years_answer import YearsScope, answer_years
from aria_core.schemas.snapshot import CandidateSnapshot
from aria_core.taxonomy.model import SkillTaxonomy


def _summary(line: str) -> ResumeDocument:
    return ResumeDocument(
        name="Jordan A. Rivera", sections=(DocumentSection(kind=SectionKind.SUMMARY, lines=(line,)),)
    )


@pytest.mark.parametrize(
    ("nordvale", "brightfold"),
    [
        # The fixture: two counted roles, 36 months.
        ({}, {}),
        # Nordvale ending a month earlier: 35 months.
        ({"end_month": dt.date(2025, 6, 1)}, {}),
        # Brightfold moved onto Nordvale's dates: 18 on the calendar, 36 by addition.
        ({}, {"start_month": dt.date(2024, 2, 1), "end_month": dt.date(2025, 7, 1)}),
        # Brightfold answered no.
        ({}, {"counts_toward_years": False}),
    ],
    ids=["fixture", "35_months", "overlap", "answered_no"],
)
def test_the_form_s_number_passes_the_summary_check_and_one_more_does_not(
    snapshot: CandidateSnapshot,
    taxonomy: SkillTaxonomy,
    nordvale: dict[str, object],
    brightfold: dict[str, object],
) -> None:
    roles = (
        snapshot.experiences[0].model_copy(update=nordvale),
        snapshot.experiences[1].model_copy(update=brightfold),
    )
    changed = snapshot.model_copy(update={"experiences": roles})
    years = answer_years(changed, YearsScope.EXPERIENCE).years
    assert years is not None and years >= 1
    assert (
        check_truth(_summary(f"Software engineer with {years}+ years of experience."), changed, taxonomy)
        == []
    )
    assert check_truth(
        _summary(f"Software engineer with {years + 1}+ years of experience."), changed, taxonomy
    )


def test_when_the_form_must_ask_the_summary_may_claim_nothing(
    snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
) -> None:
    unanswered = snapshot.experiences[1].model_copy(update={"counts_toward_years": None})
    changed = snapshot.model_copy(update={"experiences": (snapshot.experiences[0], unanswered)})
    assert answer_years(changed, YearsScope.EXPERIENCE).years is None
    assert check_truth(_summary("Software engineer with 1+ year of experience."), changed, taxonomy)
