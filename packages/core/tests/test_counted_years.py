"""Counted years: the candidate's flagged roles, unioned, rounded down (DECISIONS 2026-10-25).

Three rules from Dileep, one class each: overlapping roles count once, an unanswered role
blocks every years claim rather than being guessed, and "3+ years" needs 36 counted months.
"""

from __future__ import annotations

import datetime as dt
import uuid

from aria_core.graph.counted_years import counted_years
from aria_core.schemas.snapshot import (
    CandidateSnapshot,
    SnapshotContact,
    SnapshotExperience,
    SnapshotFact,
    SnapshotSkill,
)

AS_OF = dt.date(2026, 10, 1)


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
        taken_at=dt.datetime(2026, 10, 25, tzinfo=dt.UTC),
        as_of=AS_OF,
        taxonomy_version="test",
        contact=SnapshotContact(full_name="Jordan A. Rivera"),
        experiences=roles,
        facts=facts,
    )


class TestOverlappingRolesCountOnce:
    def test_two_concurrent_roles_are_the_union_not_the_sum(self) -> None:
        # A research post and a teaching post at the same school, half overlapping:
        # 12 months each, 18 on the calendar.
        research = _role(1, dt.date(2024, 1, 1), dt.date(2024, 12, 1), True)
        teaching = _role(2, dt.date(2024, 7, 1), dt.date(2025, 6, 1), True)
        held = counted_years(_snapshot(research, teaching))
        assert held.months == 18
        assert held.claim() == "1+ years"

    def test_a_role_inside_another_adds_nothing(self) -> None:
        outer = _role(1, dt.date(2022, 1, 1), dt.date(2024, 12, 1), True)
        inner = _role(2, dt.date(2023, 3, 1), dt.date(2023, 8, 1), True)
        assert counted_years(_snapshot(outer, inner)).months == 36

    def test_a_current_role_is_closed_at_the_snapshot_month(self) -> None:
        current = _role(1, dt.date(2025, 11, 1), None, True)
        assert counted_years(_snapshot(current)).months == 12


class TestTheFlagIsTheCandidatesAnswer:
    def test_an_unanswered_role_blocks_the_claim(self) -> None:
        # Six counted years and one role nobody has answered for: no figure at all,
        # rather than a guess either way.
        counted = _role(1, dt.date(2019, 1, 1), dt.date(2024, 12, 1), True)
        unanswered = _role(2, dt.date(2025, 1, 1), dt.date(2025, 8, 1), None)
        held = counted_years(_snapshot(counted, unanswered))
        assert held.unresolved == (unanswered.experience_id,)
        assert held.months == 72  # the answered role only; the other is not taken as a yes
        assert held.claim() is None
        assert not held.allows(1)

    def test_a_role_answered_no_adds_nothing_and_blocks_nothing(self) -> None:
        counted = _role(1, dt.date(2023, 1, 1), dt.date(2024, 12, 1), True)
        declined = _role(2, dt.date(2020, 1, 1), dt.date(2022, 12, 1), False)
        held = counted_years(_snapshot(counted, declined))
        assert (held.months, held.unresolved) == (24, ())

    def test_the_kind_is_not_read(self) -> None:
        # The importer's kind is a guess from the title. A part-time role the candidate
        # counts is counted; a "professional" one they do not is not.
        part_time = _role(1, dt.date(2023, 1, 1), dt.date(2024, 12, 1), True, kind="part_time")
        professional = _role(2, dt.date(2020, 1, 1), dt.date(2022, 12, 1), False)
        assert counted_years(_snapshot(part_time, professional)).months == 24

    def test_a_skill_claim_waits_only_on_roles_that_use_the_skill(self) -> None:
        python_role = _role(1, dt.date(2023, 1, 1), dt.date(2024, 12, 1), True)
        other = _role(2, dt.date(2025, 1, 1), dt.date(2025, 8, 1), None)
        fact = SnapshotFact(
            fact_id=uuid.UUID(int=50),
            text="Moved the billing service to Python 3.12",
            experience_id=python_role.experience_id,
            skills=(SnapshotSkill(canonical="python", surface_form="Python"),),
        )
        snapshot = _snapshot(python_role, other, facts=(fact,))
        assert counted_years(snapshot, skill="python").claim() == "2+ years"
        assert counted_years(snapshot).claim() is None


class TestRoundingDown:
    def test_thirty_five_months_is_two_years_not_three(self) -> None:
        held = counted_years(_snapshot(_role(1, dt.date(2022, 1, 1), dt.date(2024, 11, 1), True)))
        assert held.months == 35
        assert held.whole_years == 2
        assert held.claim() == "2+ years"
        assert not held.allows(3)

    def test_thirty_six_months_is_three_years(self) -> None:
        held = counted_years(_snapshot(_role(1, dt.date(2022, 1, 1), dt.date(2024, 12, 1), True)))
        assert held.months == 36
        assert held.claim() == "3+ years"
        assert held.allows(3)
        assert not held.allows(3.5)

    def test_under_a_year_makes_no_claim(self) -> None:
        held = counted_years(_snapshot(_role(1, dt.date(2024, 1, 1), dt.date(2024, 11, 1), True)))
        assert held.months == 11
        assert held.claim() is None
