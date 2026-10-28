"""Build the Resume Engine's input from the graph: approved rows only.

The engine never reads the database (DECISIONS 2026-10-16). What it may say about the
candidate is exactly what this function puts in a :class:`CandidateSnapshot`: approved
contact details, roles, projects, degrees, achievements and listed skills; approved facts
with their skills, metrics and themes, through the fact repository's approved-only read
path; and skill-years derived from those, never typed in.

An approved fact under a role, project or degree that is not itself approved is left
out. The bullet may be true, but the employer, title and dates around it are claims the
candidate has not made yet, and a resume would print them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.db.models import Achievement, DeclaredSkill, Education, Experience, ProfileField, Project
from aria_core.graph.derive import SkillExperience, compute_career_totals
from aria_core.graph.facts import approved_fact_evidence
from aria_core.graph.status import ApprovalStatus, ProfileFieldKind
from aria_core.schemas.base import utc_now
from aria_core.schemas.snapshot import (
    CandidateSnapshot,
    SnapshotAchievement,
    SnapshotContact,
    SnapshotEducation,
    SnapshotExperience,
    SnapshotFact,
    SnapshotMetric,
    SnapshotProject,
    SnapshotSkill,
    SnapshotSkillYears,
)
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_core.taxonomy.model import SkillTaxonomy

__all__ = ["build_candidate_snapshot"]

_APPROVED = ApprovalStatus.APPROVED.value

#: The profile fields a resume prints, by the snapshot field each fills.
_CONTACT_FIELDS: dict[ProfileFieldKind, str] = {
    ProfileFieldKind.FULL_NAME: "full_name",
    ProfileFieldKind.EMAIL: "email",
    ProfileFieldKind.PHONE: "phone",
    ProfileFieldKind.LOCATION_CITY: "location_city",
    ProfileFieldKind.LOCATION_REGION: "location_region",
    ProfileFieldKind.LINKEDIN_URL: "linkedin_url",
    ProfileFieldKind.GITHUB_URL: "github_url",
    ProfileFieldKind.PORTFOLIO_URL: "portfolio_url",
}


async def _approved(session: AsyncSession, model: Any, candidate_id: uuid.UUID, *columns: Any) -> list[Any]:
    rows = await session.execute(
        select(*columns)
        .where(model.candidate_id == candidate_id, model.status == _APPROVED)
        .order_by(model.created_at, model.id)
    )
    return list(rows.all())


def _skill_years(experience: SkillExperience) -> SnapshotSkillYears:
    spans = [
        *experience.full_time_spans,
        *experience.part_time_spans,
        *experience.internship_spans,
        *experience.project_spans,
    ]
    ends = [span.end for span in spans]
    return SnapshotSkillYears(
        canonical=experience.canonical,
        full_time_months=experience.full_time_months,
        part_time_months=experience.part_time_months,
        internship_months=experience.internship_months,
        project_months=experience.project_months,
        implied_full_time_months=experience.implied_full_time_months,
        fact_count=experience.fact_count,
        last_used=None if not ends or any(end is None for end in ends) else max(end for end in ends if end),
    )


async def build_candidate_snapshot(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    taxonomy: SkillTaxonomy | None = None,
    now: dt.date | None = None,
) -> CandidateSnapshot:
    """The candidate as the Resume Engine may see them, at ``now``."""
    taxonomy = taxonomy or load_default_taxonomy()
    moment = now or utc_now().date()

    contact: dict[str, str] = {}
    for kind, value in await _approved(
        session, ProfileField, candidate_id, ProfileField.kind, ProfileField.value
    ):
        field = _CONTACT_FIELDS.get(ProfileFieldKind(kind))
        if field is not None and field not in contact:
            contact[field] = value

    experiences = tuple(
        SnapshotExperience(
            experience_id=row[0],
            employer=row[1],
            title=row[2],
            client=row[3],
            kind=row[4],
            location_city=row[5],
            location_region=row[6],
            start_month=row[7],
            end_month=row[8],
            counts_toward_years=row[9],
        )
        for row in await _approved(
            session,
            Experience,
            candidate_id,
            Experience.id,
            Experience.employer,
            Experience.title,
            Experience.client,
            Experience.kind,
            Experience.location_city,
            Experience.location_region,
            Experience.start_month,
            Experience.end_month,
            Experience.counts_toward_years,
        )
    )
    projects = tuple(
        SnapshotProject(
            project_id=row[0],
            name=row[1],
            kind=row[2],
            url=row[3],
            repository_url=row[4],
            start_month=row[5],
            end_month=row[6],
        )
        for row in await _approved(
            session,
            Project,
            candidate_id,
            Project.id,
            Project.name,
            Project.kind,
            Project.url,
            Project.repository_url,
            Project.start_month,
            Project.end_month,
        )
    )
    education = tuple(
        SnapshotEducation(
            education_id=row[0],
            school=row[1],
            degree=row[2],
            field_of_study=row[3],
            gpa=row[4],
            location_city=row[5],
            location_region=row[6],
            start_month=row[7],
            end_month=row[8],
            coursework=row[9],
        )
        for row in await _approved(
            session,
            Education,
            candidate_id,
            Education.id,
            Education.school,
            Education.degree,
            Education.field_of_study,
            Education.gpa,
            Education.location_city,
            Education.location_region,
            Education.start_month,
            Education.end_month,
            Education.coursework,
        )
    )
    achievements = tuple(
        SnapshotAchievement(
            achievement_id=row[0], kind=row[1], title=row[2], issuer=row[3], awarded_month=row[4], url=row[5]
        )
        for row in await _approved(
            session,
            Achievement,
            candidate_id,
            Achievement.id,
            Achievement.kind,
            Achievement.title,
            Achievement.issuer,
            Achievement.awarded_month,
            Achievement.url,
        )
    )
    declared = tuple(
        SnapshotSkill(canonical=row[0], surface_form=row[1])
        for row in await _approved(
            session, DeclaredSkill, candidate_id, DeclaredSkill.skill_canonical, DeclaredSkill.surface_form
        )
    )

    # A fact is in only when what it hangs off is in: the role around a bullet is a claim too.
    known = (
        {item.experience_id for item in experiences}
        | {item.project_id for item in projects}
        | {item.education_id for item in education}
    )
    facts = tuple(
        SnapshotFact(
            fact_id=evidence.fact_id,
            text=evidence.text,
            experience_id=evidence.experience_id,
            project_id=evidence.project_id,
            education_id=evidence.education_id,
            skills=tuple(
                SnapshotSkill(canonical=canonical, surface_form=surface)
                for canonical, surface in evidence.skills
            ),
            metrics=tuple(
                SnapshotMetric(exact_value=value, measures=measures, baseline=baseline)
                for value, measures, baseline in evidence.metrics
            ),
            themes=evidence.themes,
        )
        for evidence in await approved_fact_evidence(session, candidate_id)
        if all(
            anchor is None or anchor in known
            for anchor in (evidence.experience_id, evidence.project_id, evidence.education_id)
        )
    )

    totals = await compute_career_totals(session, candidate_id, taxonomy=taxonomy, now=moment)
    return CandidateSnapshot(
        candidate_id=candidate_id,
        taken_at=utc_now(),
        as_of=moment,
        taxonomy_version=taxonomy.version,
        contact=SnapshotContact.model_validate(contact),
        experiences=experiences,
        projects=projects,
        education=education,
        achievements=achievements,
        facts=facts,
        declared_skills=declared,
        skill_years=tuple(_skill_years(item) for _, item in sorted(totals.skills.items())),
        full_time_months=totals.full_time_months,
        part_time_months=totals.part_time_months,
        internship_months=totals.internship_months,
    )
