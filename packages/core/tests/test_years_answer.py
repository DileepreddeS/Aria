"""A form's years answer: the resume's counted figure, or a question (DECISIONS 2026-10-27).

The same per-role answers, the same union, the same rounding down as the resume, so the
form and the resume in one application cannot disagree. An unanswered role blocks the
answer; a question scoped any other way than plain "years of experience" is asked.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest

from aria_core.graph.counted_years import counted_years
from aria_core.graph.years_answer import YearsScope, answer_years
from aria_core.schemas.snapshot import (
    CandidateSnapshot,
    SnapshotContact,
    SnapshotExperience,
    SnapshotFact,
    SnapshotSkill,
)

CORE = Path(__file__).resolve().parents[1] / "aria_core"


def _role(
    number: int, start: dt.date, end: dt.date | None, counts: bool | None, *, kind: str = "professional"
) -> SnapshotExperience:
    return SnapshotExperience(
        experience_id=uuid.UUID(int=number),
        employer="Cascadia State University",
        title=f"Role {number}",
        kind=kind,
        start_month=start,
        end_month=end,
        counts_toward_years=counts,
    )


def _snapshot(*roles: SnapshotExperience, facts: tuple[SnapshotFact, ...] = ()) -> CandidateSnapshot:
    return CandidateSnapshot(
        candidate_id=uuid.UUID(int=9),
        taken_at=dt.datetime(2026, 10, 27, tzinfo=dt.UTC),
        as_of=dt.date(2026, 10, 1),
        taxonomy_version="test",
        contact=SnapshotContact(full_name="Jordan A. Rivera"),
        experiences=roles,
        facts=facts,
    )


class TestTheSameFigureAsTheResume:
    @pytest.mark.parametrize(
        ("roles", "years"),
        [
            # Two concurrent roles, 18 months on the calendar: 1, not 2.
            (
                (
                    _role(1, dt.date(2024, 1, 1), dt.date(2024, 12, 1), True),
                    _role(2, dt.date(2024, 7, 1), dt.date(2025, 6, 1), True),
                ),
                1,
            ),
            # 35 months is 2 years; 36 is 3.
            ((_role(1, dt.date(2022, 1, 1), dt.date(2024, 11, 1), True),), 2),
            ((_role(1, dt.date(2022, 1, 1), dt.date(2024, 12, 1), True),), 3),
            # Under a year: 0 is the true answer to a number field.
            ((_role(1, dt.date(2024, 1, 1), dt.date(2024, 11, 1), True),), 0),
        ],
    )
    def test_the_answer_is_the_resume_s_whole_years(
        self, roles: tuple[SnapshotExperience, ...], years: int
    ) -> None:
        snapshot = _snapshot(*roles)
        answer = answer_years(snapshot, YearsScope.EXPERIENCE)
        assert (answer.years, answer.ask) == (years, None)
        assert answer.years == counted_years(snapshot).whole_years

    def test_an_internship_the_candidate_counts_is_counted_and_a_job_they_do_not_is_not(self) -> None:
        # The kind is not read, and neither is the old count_internships setting.
        intern = _role(1, dt.date(2023, 1, 1), dt.date(2024, 12, 1), True, kind="internship")
        job = _role(2, dt.date(2020, 1, 1), dt.date(2022, 12, 1), False)
        assert answer_years(_snapshot(intern, job), YearsScope.EXPERIENCE).years == 2

    def test_a_skill_s_years_are_its_counted_roles(self) -> None:
        python_role = _role(1, dt.date(2023, 1, 1), dt.date(2024, 12, 1), True)
        other = _role(2, dt.date(2019, 1, 1), dt.date(2022, 12, 1), True)
        fact = SnapshotFact(
            fact_id=uuid.UUID(int=50),
            text="Moved the billing service to Python 3.12",
            experience_id=python_role.experience_id,
            skills=(SnapshotSkill(canonical="python", surface_form="Python"),),
        )
        snapshot = _snapshot(python_role, other, facts=(fact,))
        assert answer_years(snapshot, YearsScope.EXPERIENCE, skill="python").years == 2
        assert answer_years(snapshot, YearsScope.EXPERIENCE).years == 6


class TestWhatIsAskedInstead:
    def test_an_unanswered_role_blocks_the_answer(self) -> None:
        counted = _role(1, dt.date(2019, 1, 1), dt.date(2024, 12, 1), True)
        unanswered = _role(2, dt.date(2025, 1, 1), dt.date(2025, 8, 1), None)
        answer = answer_years(_snapshot(counted, unanswered), YearsScope.EXPERIENCE)
        assert answer.years is None
        assert answer.unresolved == (unanswered.experience_id,)
        assert answer.ask is not None and "not yet marked" in answer.ask

    def test_a_differently_scoped_question_is_asked_even_when_every_role_is_answered(self) -> None:
        # "Years of full-time experience" with a counted part-time role: the counted
        # figure would overstate, and the flag cannot say which roles were full-time.
        part_time = _role(1, dt.date(2021, 1, 1), dt.date(2024, 12, 1), True, kind="part_time")
        answer = answer_years(_snapshot(part_time), YearsScope.OTHER)
        assert answer.years is None
        assert answer.ask is not None


class TestTheOldInputsAreGone:
    def test_nothing_reads_the_internship_setting(self) -> None:
        # The column stays (dropping it is a migration for later); only the model
        # declares it. The per-role answer is how an internship is counted now.
        readers = [
            str(path.relative_to(CORE))
            for path in CORE.rglob("*.py")
            if "count_internships_as_experience" in path.read_text(encoding="utf-8")
        ]
        assert readers == [str(Path("db") / "models" / "graph.py")]
