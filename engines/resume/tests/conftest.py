"""The synthetic candidate the validator tests judge resumes against.

Jordan A. Rivera, three years full-time across two roles and one project. Every fact is
approved (a snapshot holds nothing else), so a bullet that says only what these facts say
must pass, and one that says more must not.
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest

from aria_core.schemas.snapshot import (
    CandidateSnapshot,
    SnapshotContact,
    SnapshotExperience,
    SnapshotFact,
    SnapshotMetric,
    SnapshotProject,
    SnapshotSkill,
    SnapshotSkillYears,
)
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_core.taxonomy.model import SkillTaxonomy

IDS = SimpleNamespace(
    nordvale=uuid.UUID(int=1),
    brightfold=uuid.UUID(int=2),
    vera=uuid.UUID(int=3),
    latency=uuid.UUID(int=101),
    screens=uuid.UUID(int=102),
    apps=uuid.UUID(int=103),
    billing=uuid.UUID(int=104),
    rag=uuid.UUID(int=105),
    cloud=uuid.UUID(int=106),
)


def _skill(canonical: str, surface: str) -> SnapshotSkill:
    return SnapshotSkill(canonical=canonical, surface_form=surface)


def _fact(
    fact_id: uuid.UUID,
    text: str,
    *,
    skills: tuple[SnapshotSkill, ...] = (),
    metrics: tuple[str, ...] = (),
    experience_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> SnapshotFact:
    return SnapshotFact(
        fact_id=fact_id,
        text=text,
        skills=skills,
        metrics=tuple(SnapshotMetric(exact_value=value) for value in metrics),
        experience_id=experience_id,
        project_id=project_id,
    )


@pytest.fixture
def ids() -> SimpleNamespace:
    return IDS


@pytest.fixture
def taxonomy() -> SkillTaxonomy:
    return load_default_taxonomy()


@pytest.fixture
def snapshot() -> CandidateSnapshot:
    return CandidateSnapshot(
        candidate_id=uuid.UUID(int=9),
        taken_at=dt.datetime(2026, 10, 16, tzinfo=dt.UTC),
        as_of=dt.date(2026, 10, 1),
        taxonomy_version="2026.10.2",
        contact=SnapshotContact(full_name="Jordan A. Rivera", email="jordan.rivera@example.com"),
        experiences=(
            SnapshotExperience(
                experience_id=IDS.nordvale,
                employer="Nordvale Consulting",
                title="Software Engineer",
                kind="professional",
                start_month=dt.date(2024, 2, 1),
                end_month=dt.date(2025, 7, 1),
            ),
            SnapshotExperience(
                experience_id=IDS.brightfold,
                employer="Brightfold Systems",
                title="Associate Software Engineer",
                kind="professional",
                start_month=dt.date(2022, 7, 1),
                end_month=dt.date(2023, 12, 1),
            ),
        ),
        projects=(SnapshotProject(project_id=IDS.vera, name="VERA-RAG", kind="personal"),),
        facts=(
            _fact(
                IDS.latency,
                "Cut p95 API latency by 30% with Redis caching",
                skills=(_skill("redis", "Redis"),),
                metrics=("30%",),
                experience_id=IDS.nordvale,
            ),
            _fact(
                IDS.screens,
                "Built 15+ React screens for the checkout platform",
                skills=(_skill("react", "React"),),
                metrics=("15+",),
                experience_id=IDS.nordvale,
            ),
            _fact(
                IDS.apps,
                "Shipped 8 full-stack applications with Node.js and PostgreSQL",
                skills=(_skill("nodejs", "Node.js"), _skill("postgresql", "PostgreSQL")),
                metrics=("8",),
                experience_id=IDS.brightfold,
            ),
            _fact(
                IDS.billing,
                "Led three engineers moving the billing service to Python 3.12 in March 2023",
                skills=(_skill("python", "Python"),),
                experience_id=IDS.brightfold,
            ),
            _fact(
                IDS.rag,
                "Built a closed-loop RAG system with hybrid retrieval",
                skills=(_skill("rag", "RAG"),),
                project_id=IDS.vera,
            ),
            _fact(
                IDS.cloud,
                "Designed cloud-native solutions for the checkout platform on AWS",
                skills=(_skill("aws", "AWS"),),
                experience_id=IDS.nordvale,
            ),
        ),
        declared_skills=(_skill("typescript", "TypeScript"), _skill("sql", "SQL")),
        skill_years=(
            SnapshotSkillYears(canonical="python", full_time_months=6, fact_count=1),
            SnapshotSkillYears(canonical="react", full_time_months=18, fact_count=1),
            SnapshotSkillYears(canonical="redis", full_time_months=18, fact_count=1),
        ),
        full_time_months=36,
    )
