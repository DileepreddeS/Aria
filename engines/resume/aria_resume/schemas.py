"""What the engine's stages hand each other.

The JD side (Stage 1 ParsedJD, Stage 2 Requirement) and the writer's bullet so far. The
other stage types (EvidenceMap, ResumePlan, ResumeResult) arrive with their stages.

A job description is untrusted text (SECURITY.md §5): everything here that came from
one is data to compare and print, never an instruction to follow.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from aria_core.schemas.base import AriaMessage
from aria_core.sensitivity import Sensitivity
from aria_resume.document import SectionKind

__all__ = [
    "Bullet",
    "BulletSlot",
    "EvidenceMap",
    "FitDecision",
    "Gap",
    "JDSection",
    "NormalizedJD",
    "ParsedJD",
    "PlannedEntry",
    "PlannedSection",
    "RawRequirement",
    "Requirement",
    "RequirementKind",
    "ResumePlan",
    "RoleType",
    "ScoreBreakdown",
    "ScoredFact",
    "Seniority",
    "WorkMode",
]


class Bullet(AriaMessage):
    """One bullet as Stage 5b writes it (RESUME_ENGINE §8), with what it says it used.

    Every declaration is checked, never trusted (§9 6b): the facts must be ones the slot
    was given, the metrics exact strings from those facts, the technologies on the
    allowed list. The model proposes; :func:`aria_resume.validate.truth.check_written`
    decides.
    """

    text: Annotated[str, Sensitivity.S1] = Field(min_length=1, max_length=400)
    fact_ids: Annotated[tuple[uuid.UUID, ...], Sensitivity.S0] = Field(min_length=1)
    #: Exact strings from the cited facts' metrics: "30%", "500K+".
    metrics_used: Annotated[tuple[str, ...], Sensitivity.S1] = ()
    tech_used: Annotated[tuple[str, ...], Sensitivity.S1] = ()
    requirement_ids: Annotated[tuple[str, ...], Sensitivity.S0] = ()


class JDSection(StrEnum):
    """Where in the posting a line sat, by its heading (RESUME_ENGINE §3)."""

    REQUIRED = "required"
    PREFERRED = "preferred"
    RESPONSIBILITIES = "responsibilities"
    ABOUT = "about"
    BENEFITS = "benefits"
    #: Under a heading the vocabulary does not know, or before any heading.
    OTHER = "other"


class Seniority(StrEnum):
    INTERN = "intern"
    NEW_GRAD = "new_grad"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF_PLUS = "staff_plus"
    UNKNOWN = "unknown"


class WorkMode(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class RawRequirement(AriaMessage):
    """One line of the posting, as written, with the section it came from."""

    text: Annotated[str, Sensitivity.S0] = Field(min_length=1, max_length=1000)
    section: Annotated[JDSection, Sensitivity.S0]


class ParsedJD(AriaMessage):
    """Stage 1's reading of a posting (RESUME_ENGINE §3).

    The rules fill what rules can read reliably: sections, years, degrees, location,
    work mode, the sponsorship and citizenship sentences, keywords and suspicious
    instructions. ``title`` and ``company`` stay None until the model step reads them:
    the spec types them as strings, and a first-line guess would be a guess (DECISIONS
    2026-10-18).
    """

    title: Annotated[str | None, Sensitivity.S0] = None
    company: Annotated[str | None, Sensitivity.S0] = None
    seniority: Annotated[Seniority, Sensitivity.S0] = Seniority.UNKNOWN
    #: The general experience floor; a skill's own years sit on its Requirement.
    min_years: Annotated[int | None, Sensitivity.S0] = None
    #: The posting's own degree lines, verbatim.
    education: Annotated[tuple[str, ...], Sensitivity.S0] = ()
    location: Annotated[str | None, Sensitivity.S0] = None
    work_mode: Annotated[WorkMode, Sensitivity.S0] = WorkMode.UNKNOWN
    responsibilities: Annotated[tuple[str, ...], Sensitivity.S0] = Field(default=(), max_length=12)
    required: tuple[RawRequirement, ...] = ()
    preferred: tuple[RawRequirement, ...] = ()
    #: Exact surface forms as the posting wrote them ("PostgreSQL", "CI/CD").
    keywords: Annotated[tuple[str, ...], Sensitivity.S0] = ()
    sponsorship_language: Annotated[str | None, Sensitivity.S0] = None
    clearance_or_citizenship: Annotated[str | None, Sensitivity.S0] = None
    #: "<kind>: <quote>" for each instruction unrelated to applying. Never acted on.
    suspicious_instructions: Annotated[tuple[str, ...], Sensitivity.S0] = ()


class RequirementKind(StrEnum):
    SKILL = "skill"
    DOMAIN = "domain"
    RESPONSIBILITY = "responsibility"
    EDUCATION = "education"
    YEARS = "years"
    SOFT = "soft"


class Requirement(AriaMessage):
    """One canonical, weighted requirement (RESUME_ENGINE §4)."""

    #: "<kind>:<canonical>", stable across runs of the same posting.
    id: Annotated[str, Sensitivity.S0]
    kind: Annotated[RequirementKind, Sensitivity.S0]
    canonical: Annotated[str, Sensitivity.S0]
    #: How the JD wrote it, in order of first appearance: printed verbatim for ATS.
    surface_forms: Annotated[tuple[str, ...], Sensitivity.S0] = Field(min_length=1)
    weight: Annotated[float, Sensitivity.S0] = Field(gt=0.0, le=1.0)
    must_have: Annotated[bool, Sensitivity.S0]
    min_years: Annotated[int | None, Sensitivity.S0] = None
    #: Requirements sharing a group are alternatives ("Python or Go", "Zustand, MobX or
    #: Redux"): any one meets it. Not in the spec's type; added because reading each
    #: alternative as a must-have reports a gap the posting never asked for (DECISIONS
    #: 2026-10-18). None when the requirement stands on its own anywhere in the posting.
    any_of: Annotated[str | None, Sensitivity.S0] = None


class NormalizedJD(AriaMessage):
    """Stage 2's output: the requirements, and what it could not place."""

    requirements: tuple[Requirement, ...]
    #: Required and preferred lines that named no skill the taxonomy knows, kept for
    #: review and for the matcher's text similarity, never dropped.
    unplaced: tuple[RawRequirement, ...] = ()
    taxonomy_version: Annotated[str, Sensitivity.S0]


class RoleType(StrEnum):
    """The role template a posting calls for (RESUME_ENGINE \u00a77, \u00a78 5a)."""

    FRONTEND = "frontend"
    BACKEND = "backend"
    FULLSTACK = "fullstack"
    AI = "ai"
    ML_RESEARCH = "ml_research"
    DATA = "data"
    DEVOPS = "devops"
    SWE = "swe"


class FitDecision(StrEnum):
    STRONG = "strong"
    OK = "ok"
    WEAK = "weak"
    NO_FIT = "no_fit"


class ScoreBreakdown(AriaMessage):
    """The five parts of a (requirement, fact) score (RESUME_ENGINE \u00a77), each 0 to 1."""

    skill_match: Annotated[float, Sensitivity.S0] = Field(ge=0.0, le=1.0)
    semantic_sim: Annotated[float, Sensitivity.S0] = Field(ge=0.0, le=1.0)
    metric_strength: Annotated[float, Sensitivity.S0] = Field(ge=0.0, le=1.0)
    recency: Annotated[float, Sensitivity.S0] = Field(ge=0.0, le=1.0)
    context_fit: Annotated[float, Sensitivity.S0] = Field(ge=0.0, le=1.0)


class ScoredFact(AriaMessage):
    fact_id: Annotated[uuid.UUID, Sensitivity.S0]
    score: Annotated[float, Sensitivity.S0] = Field(ge=0.0, le=1.0)
    breakdown: ScoreBreakdown
    #: How the fact meets the skill: "exact", "implied", "sibling" or "none".
    match: Annotated[str, Sensitivity.S0]


class Gap(AriaMessage):
    """A requirement the evidence meets weakly or not at all, said so the candidate can act."""

    requirement_id: Annotated[str, Sensitivity.S0]
    #: The posting's own spelling of what is missing.
    label: Annotated[str, Sensitivity.S0]
    must_have: Annotated[bool, Sensitivity.S0]
    best_score: Annotated[float, Sensitivity.S0] = Field(ge=0.0, le=1.0)
    #: The closest approved fact, when there is one.
    closest_fact_id: Annotated[uuid.UUID | None, Sensitivity.S0] = None
    #: What to prepare: never a claim to add, always something to be ready to discuss.
    suggestion: Annotated[str, Sensitivity.S1]


class EvidenceMap(AriaMessage):
    """Stage 4's answer: which approved facts prove which requirements, and how well."""

    #: The best three facts for each requirement, with every score's parts.
    per_requirement: dict[str, tuple[ScoredFact, ...]]
    #: Weighted share of must-haves with evidence at or above the strong threshold; a
    #: group of alternatives counts once, met by any member.
    coverage: Annotated[float, Sensitivity.S0] = Field(ge=0.0, le=1.0)
    strong: Annotated[tuple[str, ...], Sensitivity.S0] = ()
    light_gaps: tuple[Gap, ...] = ()
    missing: tuple[Gap, ...] = ()
    #: Years and degree requirements are met by the profile, not by a fact: why, in words.
    profile_checks: Annotated[dict[str, str], Sensitivity.S1] = Field(default_factory=dict)
    role_type: Annotated[RoleType, Sensitivity.S0]
    fit_decision: Annotated[FitDecision, Sensitivity.S0]
    #: The embedding model's id, so a map can be explained with the model that made it.
    embedder: Annotated[str, Sensitivity.S0]


class BulletSlot(AriaMessage):
    """One bullet the planner keeps room for: which facts it says, and which requirements it serves."""

    fact_ids: Annotated[tuple[uuid.UUID, ...], Sensitivity.S0] = Field(min_length=1)
    requirement_ids: Annotated[tuple[str, ...], Sensitivity.S0] = ()
    #: Requirement weight the bullet carries: sum of weight x score over what its facts prove.
    value: Annotated[float, Sensitivity.S0] = Field(ge=0.0)
    #: The best evidence for a must-have: removed only after every other bullet.
    mandatory: Annotated[bool, Sensitivity.S0] = False
    #: Printed lines, estimated from the fact's own text (the writer may change it; the page
    #: is checked again after rendering).
    lines: Annotated[int, Sensitivity.S0] = Field(ge=1)


class PlannedEntry(AriaMessage):
    kind: Annotated[SectionKind, Sensitivity.S0]
    #: The approved role, project or degree: its header is looked up, never written.
    source_id: Annotated[uuid.UUID, Sensitivity.S0]
    bullets: tuple[BulletSlot, ...] = ()
    #: A role that cannot hold two bullets keeps its employer, title and months, so the page
    #: shows no gap in employment (DECISIONS 2026-10-18).
    header_only: Annotated[bool, Sensitivity.S0] = False


class PlannedSection(AriaMessage):
    kind: Annotated[SectionKind, Sensitivity.S0]
    entries: tuple[PlannedEntry, ...] = ()
    #: For the skills section: "Languages: Python, Go", in the posting's spelling.
    lines: Annotated[tuple[str, ...], Sensitivity.S1] = ()


class ResumePlan(AriaMessage):
    """Stage 5a's output (RESUME_ENGINE \u00a78): the page before a word of it is written."""

    role_type: Annotated[RoleType, Sensitivity.S0]
    template_version: Annotated[str, Sensitivity.S0]
    sections: tuple[PlannedSection, ...]
    #: canonical skill -> the spelling to print: the posting's when it names the same skill.
    surface_forms: Annotated[dict[str, str], Sensitivity.S0] = Field(default_factory=dict)
    #: Estimated height in points, against the page's usable height.
    estimated_height: Annotated[float, Sensitivity.S0]
    usable_height: Annotated[float, Sensitivity.S0]
    #: Facts the budget left out, lowest value first, so a reviewer can see what was cut.
    dropped: Annotated[tuple[uuid.UUID, ...], Sensitivity.S0] = ()
