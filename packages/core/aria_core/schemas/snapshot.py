"""The candidate as the Resume Engine sees them: approved rows only, frozen at one moment.

The engine is pure (DECISIONS 2026-10-16): it never reads the database, so this type is
the whole of what it knows about the candidate. It is built by
:func:`aria_core.graph.snapshot.build_candidate_snapshot` from approved rows only, which
makes "only verified facts can appear on a resume" (RESUME_ENGINE §1) a property of the
input rather than of every stage remembering to filter.

Two rules hold by construction and are checked again here:

- every fact hangs off a role, project or degree that is itself in the snapshot, or off
  nothing; an approved bullet under an unapproved role is left out, because the role's
  employer, title and dates are claims the candidate has not yet made;
- no field is S2 or S3. A resume prints contact details (S1) and nothing more sensitive
  exists here to print.

No database or ORM import belongs in this module: the engine imports it, and the
engine's boundary test forbids database code.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Self

from pydantic import Field, model_validator

from aria_core.schemas.base import AriaMessage
from aria_core.sensitivity import Sensitivity

__all__ = [
    "CandidateSnapshot",
    "SnapshotAchievement",
    "SnapshotContact",
    "SnapshotEducation",
    "SnapshotExperience",
    "SnapshotFact",
    "SnapshotMetric",
    "SnapshotProject",
    "SnapshotSkill",
    "SnapshotSkillYears",
]


class SnapshotContact(AriaMessage):
    """The approved contact details a resume prints. Each is absent until approved."""

    full_name: Annotated[str | None, Sensitivity.S1] = None
    email: Annotated[str | None, Sensitivity.S1] = None
    phone: Annotated[str | None, Sensitivity.S1] = None
    location_city: Annotated[str | None, Sensitivity.S1] = None
    location_region: Annotated[str | None, Sensitivity.S1] = None
    linkedin_url: Annotated[str | None, Sensitivity.S1] = None
    github_url: Annotated[str | None, Sensitivity.S1] = None
    portfolio_url: Annotated[str | None, Sensitivity.S1] = None


class SnapshotExperience(AriaMessage):
    experience_id: Annotated[uuid.UUID, Sensitivity.S0]
    employer: Annotated[str, Sensitivity.S1]
    title: Annotated[str, Sensitivity.S1]
    #: The client a consultant was placed with ("Carvana via Accenture").
    client: Annotated[str | None, Sensitivity.S1] = None
    #: professional, internship, part_time, research...: decides the experience bucket.
    kind: Annotated[str, Sensitivity.S0]
    location_city: Annotated[str | None, Sensitivity.S1] = None
    location_region: Annotated[str | None, Sensitivity.S1] = None
    start_month: Annotated[dt.date, Sensitivity.S1]
    #: None while the role is current.
    end_month: Annotated[dt.date | None, Sensitivity.S1] = None
    #: The candidate's own answer: does this role count toward "years of experience"?
    #: None until they say, and while any role is None no years claim is made.
    counts_toward_years: Annotated[bool | None, Sensitivity.S0] = None


class SnapshotProject(AriaMessage):
    project_id: Annotated[uuid.UUID, Sensitivity.S0]
    name: Annotated[str, Sensitivity.S1]
    kind: Annotated[str, Sensitivity.S0]
    url: Annotated[str | None, Sensitivity.S1] = None
    repository_url: Annotated[str | None, Sensitivity.S1] = None
    start_month: Annotated[dt.date | None, Sensitivity.S1] = None
    end_month: Annotated[dt.date | None, Sensitivity.S1] = None


class SnapshotEducation(AriaMessage):
    education_id: Annotated[uuid.UUID, Sensitivity.S0]
    school: Annotated[str, Sensitivity.S1]
    degree: Annotated[str, Sensitivity.S1]
    field_of_study: Annotated[str | None, Sensitivity.S1] = None
    gpa: Annotated[str | None, Sensitivity.S1] = None
    location_city: Annotated[str | None, Sensitivity.S1] = None
    location_region: Annotated[str | None, Sensitivity.S1] = None
    start_month: Annotated[dt.date | None, Sensitivity.S1] = None
    end_month: Annotated[dt.date | None, Sensitivity.S1] = None
    coursework: Annotated[str | None, Sensitivity.S1] = None


class SnapshotAchievement(AriaMessage):
    achievement_id: Annotated[uuid.UUID, Sensitivity.S0]
    kind: Annotated[str, Sensitivity.S0]
    title: Annotated[str, Sensitivity.S1]
    issuer: Annotated[str | None, Sensitivity.S1] = None
    awarded_month: Annotated[dt.date | None, Sensitivity.S1] = None
    url: Annotated[str | None, Sensitivity.S1] = None


class SnapshotSkill(AriaMessage):
    canonical: Annotated[str, Sensitivity.S0]
    #: The candidate's own spelling, for ATS matching that is literal.
    surface_form: Annotated[str, Sensitivity.S1]


class SnapshotMetric(AriaMessage):
    #: Exactly as the candidate wrote it: "30%", "500K+". Never rounded or converted.
    exact_value: Annotated[str, Sensitivity.S1]
    measures: Annotated[str | None, Sensitivity.S1] = None
    baseline: Annotated[str | None, Sensitivity.S1] = None


class SnapshotFact(AriaMessage):
    """One approved, true statement: the atomic unit every bullet must cite."""

    fact_id: Annotated[uuid.UUID, Sensitivity.S0]
    text: Annotated[str, Sensitivity.S1]
    experience_id: Annotated[uuid.UUID | None, Sensitivity.S0] = None
    project_id: Annotated[uuid.UUID | None, Sensitivity.S0] = None
    education_id: Annotated[uuid.UUID | None, Sensitivity.S0] = None
    skills: tuple[SnapshotSkill, ...] = ()
    metrics: tuple[SnapshotMetric, ...] = ()
    themes: Annotated[tuple[str, ...], Sensitivity.S0] = ()


class SnapshotSkillYears(AriaMessage):
    """Months of evidence for one skill, by bucket, derived from approved facts (never typed in)."""

    canonical: Annotated[str, Sensitivity.S0]
    full_time_months: Annotated[int, Sensitivity.S1] = Field(default=0, ge=0)
    part_time_months: Annotated[int, Sensitivity.S1] = Field(default=0, ge=0)
    internship_months: Annotated[int, Sensitivity.S1] = Field(default=0, ge=0)
    project_months: Annotated[int, Sensitivity.S1] = Field(default=0, ge=0)
    #: Reaching the skill only through taxonomy implication: for matching, never printed.
    implied_full_time_months: Annotated[int, Sensitivity.S1] = Field(default=0, ge=0)
    fact_count: Annotated[int, Sensitivity.S1] = Field(default=0, ge=0)
    #: The last month evidence for it ends, or None while a role using it is current.
    last_used: Annotated[dt.date | None, Sensitivity.S1] = None


class CandidateSnapshot(AriaMessage):
    """Everything the engine may say about the candidate, and nothing it may not."""

    candidate_id: Annotated[uuid.UUID, Sensitivity.S0]
    taken_at: Annotated[dt.datetime, Sensitivity.S0]
    #: The month derived values are closed at, so a snapshot does not age between reads.
    as_of: Annotated[dt.date, Sensitivity.S0]
    taxonomy_version: Annotated[str, Sensitivity.S0]
    contact: SnapshotContact
    experiences: tuple[SnapshotExperience, ...] = ()
    projects: tuple[SnapshotProject, ...] = ()
    education: tuple[SnapshotEducation, ...] = ()
    achievements: tuple[SnapshotAchievement, ...] = ()
    facts: tuple[SnapshotFact, ...] = ()
    #: Approved skills the candidate listed, canonical ids, whether or not a fact uses them.
    declared_skills: tuple[SnapshotSkill, ...] = ()
    skill_years: tuple[SnapshotSkillYears, ...] = ()
    full_time_months: Annotated[int, Sensitivity.S1] = Field(default=0, ge=0)
    part_time_months: Annotated[int, Sensitivity.S1] = Field(default=0, ge=0)
    internship_months: Annotated[int, Sensitivity.S1] = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _every_fact_hangs_off_something_in_the_snapshot(self) -> Self:
        experiences = {item.experience_id for item in self.experiences}
        projects = {item.project_id for item in self.projects}
        education = {item.education_id for item in self.education}
        for fact in self.facts:
            anchors = [fact.experience_id, fact.project_id, fact.education_id]
            if sum(anchor is not None for anchor in anchors) > 1:
                raise ValueError(f"fact {fact.fact_id} hangs off more than one thing")
            if fact.experience_id is not None and fact.experience_id not in experiences:
                raise ValueError(f"fact {fact.fact_id} hangs off a role that is not in the snapshot")
            if fact.project_id is not None and fact.project_id not in projects:
                raise ValueError(f"fact {fact.fact_id} hangs off a project that is not in the snapshot")
            if fact.education_id is not None and fact.education_id not in education:
                raise ValueError(f"fact {fact.fact_id} hangs off a degree that is not in the snapshot")
        ids = [fact.fact_id for fact in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("a fact appears twice")
        return self

    def fact(self, fact_id: uuid.UUID) -> SnapshotFact:
        """The fact with this id. A KeyError means a stage cited something it was not given."""
        for fact in self.facts:
            if fact.fact_id == fact_id:
                return fact
        raise KeyError(str(fact_id))
