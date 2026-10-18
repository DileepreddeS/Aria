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

__all__ = [
    "Bullet",
    "JDSection",
    "NormalizedJD",
    "ParsedJD",
    "RawRequirement",
    "Requirement",
    "RequirementKind",
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
