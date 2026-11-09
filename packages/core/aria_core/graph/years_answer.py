"""Years in a form answer: the resume's figure, or a question (DECISIONS 2026-10-27).

A form answer that contradicts the resume sent with it, inside one application, is worse
than either being conservative. So a years question on a form is answered from exactly
the figure the resume's summary is checked against,
:func:`aria_core.graph.counted_years.counted_years`: the roles the candidate said count,
unioned, rounded down, with any unanswered role blocking the answer.

This replaces ``ClaimPolicy``, which widened or narrowed the figure by the question's
wording using each role's importer-proposed kind. The per-role answer is a plain yes or
no, so it can answer one question, "years of experience", and no other. A question
scoped any other way ("full-time only", "including internships", "professional
experience") cannot be answered from it without reading the kind again, so it is not
answered at all: it goes to the candidate as a question. Which scope a question has is
the Answer Engine's to classify; this module only refuses what it cannot answer truly.

The candidate-wide internship setting (migration 0002) is no longer read: answering yes
for an internship is how a candidate counts it now.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from aria_core.graph.counted_years import counted_years
from aria_core.schemas.snapshot import CandidateSnapshot

__all__ = ["YearsAnswer", "YearsScope", "answer_years"]


class YearsScope(StrEnum):
    """What a form's years question asks about, as the Answer Engine classified it."""

    EXPERIENCE = "experience"
    """"Years of experience", or of work experience generally: the counted figure."""
    OTHER = "other"
    """Anything scoped differently (full-time only, including internships, professional,
    industry...). Not answerable from a yes-or-no per role: asked, never computed."""


@dataclass(frozen=True, slots=True)
class YearsAnswer:
    """Either a number of whole years, or why the candidate has to be asked."""

    #: Completed years, rounded down; 0 is a true answer. None when it must be asked.
    years: int | None
    #: Why it must be asked, for the question put to the candidate. None when answered.
    ask: str | None = None
    #: Roles still waiting on their counts-toward-years answer.
    unresolved: tuple[uuid.UUID, ...] = ()


def answer_years(snapshot: CandidateSnapshot, scope: YearsScope, *, skill: str | None = None) -> YearsAnswer:
    """The years a form may state, from the same figure the resume uses.

    With ``skill`` (a canonical id), the years of that skill: counted roles whose facts
    use it, exactly as the summary check computes them.
    """
    if scope is not YearsScope.EXPERIENCE:
        return YearsAnswer(
            years=None,
            ask="the question asks for a kind of experience a per-role yes or no cannot answer",
        )
    held = counted_years(snapshot, skill=skill)
    if held.unresolved:
        return YearsAnswer(
            years=None,
            ask=f"{len(held.unresolved)} role(s) not yet marked as counting toward years",
            unresolved=held.unresolved,
        )
    return YearsAnswer(years=held.whole_years)
