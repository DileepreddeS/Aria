"""The five parts of a (requirement, fact) score (RESUME_ENGINE §7), each 0 to 1.

```
score = 0.40 * skill_match      # exact 1.0, implied via the taxonomy 0.6, sibling 0.3
      + 0.25 * semantic_sim     # local embeddings, fact text against requirement text
      + 0.15 * metric_strength  # the fact states a result; a bigger relative one scores higher
      + 0.10 * recency          # falls with the years since the experience ended
      + 0.10 * context_fit      # professional > research > academic > personal (reversed for research roles)
```

The weights and every threshold here are configuration, set from the spec and to be
tuned on the eval (step 12), never by feel on one posting.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from aria_core.graph.status import ExperienceKind, ProjectKind
from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotFact
from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.schemas import Requirement, RequirementKind

__all__ = [
    "MatchWeights",
    "context_fit",
    "metric_strength",
    "recency",
    "skill_match",
]


@dataclass(frozen=True, slots=True)
class MatchWeights:
    skill_match: float = 0.40
    semantic_sim: float = 0.25
    metric_strength: float = 0.15
    recency: float = 0.10
    context_fit: float = 0.10


EXACT, IMPLIED, SIBLING = 1.0, 0.6, 0.3

_SKILL_KINDS = (RequirementKind.SKILL, RequirementKind.DOMAIN, RequirementKind.SOFT)


def skill_match(requirement: Requirement, named: set[str], taxonomy: SkillTaxonomy) -> tuple[float, str]:
    """How the skills a fact names meet the requirement's: exact, implied, sibling or not at all.

    Implied runs one way: a Next.js fact implies React, a React fact does not imply
    Next.js. A sibling (Memcached for Redis) is a partial match for matching only; it is
    never evidence a bullet may print (§9 6b).
    """
    if requirement.kind not in _SKILL_KINDS or requirement.canonical not in taxonomy:
        return 0.0, "none"
    if requirement.canonical in named:
        return EXACT, "exact"
    known = [canonical for canonical in named if canonical in taxonomy]
    if any(requirement.canonical in taxonomy.implications(canonical) for canonical in known):
        return IMPLIED, "implied"
    if any(requirement.canonical in taxonomy.siblings(canonical) for canonical in known):
        return SIBLING, "sibling"
    return 0.0, "none"


_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_MULTIPLIER = re.compile(r"(\d+(?:\.\d+)?)\s*[x\u00d7]\b", re.IGNORECASE)


def metric_strength(fact: SnapshotFact) -> float:
    """0 without a metric; more for a bigger relative change (30% or 2x over 5%)."""
    if not fact.metrics:
        return 0.0
    best = 0.6
    for metric in fact.metrics:
        percent = _PERCENT.search(metric.exact_value)
        multiplier = _MULTIPLIER.search(metric.exact_value)
        if (percent and float(percent.group(1)) >= 25) or (multiplier and float(multiplier.group(1)) >= 2):
            best = max(best, 1.0)
        elif percent and float(percent.group(1)) >= 10:
            best = max(best, 0.8)
    return best


#: Recency falls by this much per year since the work ended, to a floor.
RECENCY_PER_YEAR, RECENCY_FLOOR, UNDATED = 0.15, 0.2, 0.5


def _anchor_end(fact: SnapshotFact, snapshot: CandidateSnapshot) -> tuple[bool, dt.date | None]:
    """(dated, end month) of the role, project or degree the fact hangs off; None end = current."""
    for experience in snapshot.experiences:
        if experience.experience_id == fact.experience_id:
            return True, experience.end_month
    for project in snapshot.projects:
        if project.project_id == fact.project_id:
            return project.start_month is not None or project.end_month is not None, project.end_month
    for degree in snapshot.education:
        if degree.education_id == fact.education_id:
            return degree.end_month is not None, degree.end_month
    return False, None


def recency(fact: SnapshotFact, snapshot: CandidateSnapshot) -> float:
    dated, end = _anchor_end(fact, snapshot)
    if not dated:
        return UNDATED
    if end is None:
        return 1.0
    years = max(0, (snapshot.as_of.year - end.year) * 12 + snapshot.as_of.month - end.month) / 12
    return max(RECENCY_FLOOR, 1.0 - RECENCY_PER_YEAR * years)


_INDUSTRY: dict[str, float] = {
    ExperienceKind.PROFESSIONAL.value: 1.0,
    ExperienceKind.CONTRACT.value: 1.0,
    ExperienceKind.INTERNSHIP.value: 0.85,
    ExperienceKind.PART_TIME.value: 0.75,
    ExperienceKind.RESEARCH.value: 0.6,
    ProjectKind.RESEARCH.value: 0.6,
    ProjectKind.OPEN_SOURCE.value: 0.6,
    ProjectKind.ACADEMIC.value: 0.5,
    ProjectKind.PERSONAL.value: 0.4,
    ExperienceKind.VOLUNTEER.value: 0.4,
}
_RESEARCH_ROLE: dict[str, float] = {
    ExperienceKind.RESEARCH.value: 1.0,
    ProjectKind.RESEARCH.value: 1.0,
    ProjectKind.ACADEMIC.value: 0.85,
    ExperienceKind.PROFESSIONAL.value: 0.7,
    ExperienceKind.CONTRACT.value: 0.7,
    ExperienceKind.INTERNSHIP.value: 0.65,
    ExperienceKind.PART_TIME.value: 0.6,
    ProjectKind.OPEN_SOURCE.value: 0.6,
    ProjectKind.PERSONAL.value: 0.4,
    ExperienceKind.VOLUNTEER.value: 0.4,
}


def context_fit(fact: SnapshotFact, snapshot: CandidateSnapshot, *, research_role: bool) -> float:
    table = _RESEARCH_ROLE if research_role else _INDUSTRY
    kind = next(
        (item.kind for item in snapshot.experiences if item.experience_id == fact.experience_id),
        next((item.kind for item in snapshot.projects if item.project_id == fact.project_id), None),
    )
    if kind is None:
        # A fact under a degree reads as academic work; one under nothing, as personal.
        kind = ProjectKind.ACADEMIC.value if fact.education_id is not None else ProjectKind.PERSONAL.value
    return table.get(kind, table[ProjectKind.PERSONAL.value])
