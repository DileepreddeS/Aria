"""The years a resume may claim: computed from role dates, never written (DECISIONS 2026-10-25).

Three rules, each Dileep's:

* **Which roles count is the candidate's answer.** Each role carries
  ``counts_toward_years``, set only when the candidate answers the question asked when the
  role was approved. Nothing here reads the title, the employer or the kind: a student or
  part-time role is exactly where that inference would be wrong.
* **An unanswered role blocks the claim.** If any role that could contribute has no answer,
  there is no figure at all, so the engine says nothing about years rather than guessing
  either way. The question is already waiting for the candidate.
* **Union, then round down.** Counted roles' months are merged before counting, so two
  concurrent roles add no more time than the calendar holds; "3+ years" needs 36 counted
  months, and 35 is "2+ years".

Pure: it reads a :class:`CandidateSnapshot` and nothing else, so the engine may import it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from aria_core.graph.months import MonthSpan, total_months
from aria_core.schemas.snapshot import CandidateSnapshot

__all__ = ["CountedYears", "counted_years"]


@dataclass(frozen=True, slots=True)
class CountedYears:
    """Counted months, and the roles still waiting on the candidate's answer."""

    months: int
    #: Roles that would contribute but whose flag nobody has set.
    unresolved: tuple[uuid.UUID, ...] = ()

    @property
    def whole_years(self) -> int:
        """Completed years only: 35 months is 2, never 3."""
        return self.months // 12

    def allows(self, claimed: float) -> bool:
        """Whether a claim of ``claimed`` years is true: answered, and not a month over."""
        return not self.unresolved and claimed * 12 <= self.months

    def claim(self) -> str | None:
        """The phrase a summary may print, or None when it may print none."""
        if self.unresolved or self.whole_years < 1:
            return None
        return f"{self.whole_years}+ years"


def counted_years(snapshot: CandidateSnapshot, *, skill: str | None = None) -> CountedYears:
    """Months of counted roles, unioned, as of the snapshot.

    With ``skill`` (a canonical id), only roles with an approved fact naming that skill
    contribute, and only their flags matter.
    """
    roles = snapshot.experiences
    if skill is not None:
        using = {
            fact.experience_id
            for fact in snapshot.facts
            if fact.experience_id is not None and any(s.canonical == skill for s in fact.skills)
        }
        roles = tuple(role for role in roles if role.experience_id in using)
    unresolved = tuple(role.experience_id for role in roles if role.counts_toward_years is None)
    spans = [
        MonthSpan(role.start_month, role.end_month).resolve(snapshot.as_of)
        for role in roles
        if role.counts_toward_years is True
    ]
    return CountedYears(months=total_months(spans, now=snapshot.as_of), unresolved=unresolved)
