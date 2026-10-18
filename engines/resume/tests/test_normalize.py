"""Stage 2, the Requirement Normalizer (RESUME_ENGINE §4), on the synthetic postings."""

from __future__ import annotations

from pathlib import Path

import pytest
from aria_resume.jd.reader import read_jd
from aria_resume.normalize.requirements import normalize
from aria_resume.schemas import (
    JDSection,
    NormalizedJD,
    ParsedJD,
    RawRequirement,
    Requirement,
    RequirementKind,
)

from aria_core.taxonomy.model import SkillTaxonomy

JDS = Path(__file__).parent / "fixtures" / "jds"


def _normalized(name: str, taxonomy: SkillTaxonomy) -> NormalizedJD:
    return normalize(read_jd((JDS / f"{name}.txt").read_text(encoding="utf-8"), taxonomy), taxonomy)


def _by_id(normalized: NormalizedJD) -> dict[str, Requirement]:
    return {requirement.id: requirement for requirement in normalized.requirements}


class TestLarkspur:
    @pytest.fixture
    def reqs(self, taxonomy: SkillTaxonomy) -> dict[str, Requirement]:
        return _by_id(_normalized("larkspur_backend", taxonomy))

    def test_a_must_have_skill(self, reqs: dict[str, Requirement]) -> None:
        docker = reqs["skill:docker"]
        assert (docker.weight, docker.must_have, docker.any_of) == (1.0, True, None)
        assert docker.surface_forms == ("Docker",)

    def test_alternatives_share_a_group_and_stay_must_have(self, reqs: dict[str, Requirement]) -> None:
        python, go = reqs["skill:python"], reqs["skill:golang"]
        assert python.any_of is not None and python.any_of == go.any_of
        assert python.must_have and go.must_have

    def test_a_skill_named_in_a_responsibility_too_keeps_its_strongest_reading(
        self, reqs: dict[str, Requirement]
    ) -> None:
        # Python is in "Build REST APIs in Node and Python" (0.7) and a must-have (1.0).
        assert reqs["skill:python"].weight == 1.0

    def test_years_on_a_skill_and_ideally_softens_what_follows(self, reqs: dict[str, Requirement]) -> None:
        sql, postgres = reqs["skill:sql"], reqs["skill:postgresql"]
        assert (sql.must_have, sql.min_years) == (True, 2)
        # "ideally Postgres": preferred, not required. Postgres also designs the schemas
        # in a responsibility, so 0.7 is its strongest reading.
        assert (postgres.must_have, postgres.weight) == (False, 0.7)
        # Required line first, so the requirement's own spelling leads.
        assert postgres.surface_forms == ("Postgres", "PostgreSQL")

    def test_preferred_skills(self, reqs: dict[str, Requirement]) -> None:
        kafka, rabbit = reqs["skill:kafka"], reqs["skill:rabbitmq"]
        assert (kafka.weight, kafka.must_have) == (0.5, False)
        assert kafka.any_of == rabbit.any_of is not None

    def test_a_sibling_product_is_its_own_requirement(self, reqs: dict[str, Requirement]) -> None:
        assert "skill:mariadb" in reqs
        assert "skill:mysql" not in reqs

    def test_responsibility_implied_skills(self, reqs: dict[str, Requirement]) -> None:
        assert (reqs["skill:nodejs"].weight, reqs["skill:nodejs"].must_have) == (0.7, False)
        assert (reqs["skill:rest_api"].weight, reqs["skill:rest_api"].must_have) == (0.7, False)

    def test_responsibilities_are_requirements_of_their_own(self, reqs: dict[str, Requirement]) -> None:
        responsibilities = [r for r in reqs.values() if r.kind is RequirementKind.RESPONSIBILITY]
        assert len(responsibilities) == 3
        assert all((r.weight, r.must_have) == (0.7, False) for r in responsibilities)

    def test_years_and_degree(self, reqs: dict[str, Requirement]) -> None:
        years = reqs["years:experience"]
        assert (years.min_years, years.must_have) == (1, True)
        degree = reqs["education:bachelor"]
        assert degree.must_have and degree.surface_forms[0].startswith("BS in Computer Science")

    def test_a_line_naming_no_known_skill_is_kept_not_dropped(self, taxonomy: SkillTaxonomy) -> None:
        unplaced = [item.text for item in _normalized("larkspur_backend", taxonomy).unplaced]
        assert unplaced == ["Exposure to Zorblang or other niche DSLs"]


class TestTidewell:
    @pytest.fixture
    def reqs(self, taxonomy: SkillTaxonomy) -> dict[str, Requirement]:
        return _by_id(_normalized("tidewell_frontend", taxonomy))

    def test_years_per_skill_and_overall(self, reqs: dict[str, Requirement]) -> None:
        assert reqs["skill:react"].min_years == 3
        assert reqs["skill:typescript"].min_years == 3
        assert reqs["years:experience"].min_years == 5

    def test_a_three_way_alternative(self, reqs: dict[str, Requirement]) -> None:
        groups = {reqs[f"skill:{name}"].any_of for name in ("zustand", "mobx", "redux")}
        assert len(groups) == 1 and None not in groups

    def test_a_domain(self, reqs: dict[str, Requirement]) -> None:
        assert reqs["domain:hipaa"].kind is RequirementKind.DOMAIN

    def test_nothing_from_the_injected_lines(self, taxonomy: SkillTaxonomy) -> None:
        normalized = _normalized("tidewell_frontend", taxonomy)
        texts = [form for r in normalized.requirements for form in r.surface_forms]
        texts += [item.text for item in normalized.unplaced]
        assert not any("passport" in text or "perfect match" in text for text in texts)


class TestCorvane:
    @pytest.fixture
    def reqs(self, taxonomy: SkillTaxonomy) -> dict[str, Requirement]:
        return _by_id(_normalized("corvane_ai", taxonomy))

    def test_a_preferred_degree_written_inside_the_required_section(
        self, reqs: dict[str, Requirement]
    ) -> None:
        degree = reqs["education:master"]
        assert (degree.must_have, degree.weight) == (False, 0.5)

    def test_examples_after_such_as_are_alternatives_and_the_concept_stands(
        self, reqs: dict[str, Requirement]
    ) -> None:
        examples = {reqs[f"skill:{name}"].any_of for name in ("pinecone", "qdrant", "pgvector")}
        assert len(examples) == 1 and None not in examples
        concept = reqs["skill:vector_database"]
        assert concept.any_of is None and concept.must_have

    def test_general_years_are_not_pinned_to_a_skill(self, reqs: dict[str, Requirement]) -> None:
        assert reqs["years:experience"].min_years == 2
        assert reqs["skill:machine_learning"].min_years is None


def test_the_ids_are_stable(taxonomy: SkillTaxonomy) -> None:
    first = _normalized("larkspur_backend", taxonomy)
    assert first == _normalized("larkspur_backend", taxonomy)
    assert len({r.id for r in first.requirements}) == len(first.requirements)


def test_a_standalone_mention_takes_a_skill_out_of_its_group(taxonomy: SkillTaxonomy) -> None:
    jd = ParsedJD(
        required=(
            RawRequirement(text="Experience with Python or Go", section=JDSection.REQUIRED),
            RawRequirement(text="Strong Python", section=JDSection.REQUIRED),
        )
    )
    reqs = _by_id(normalize(jd, taxonomy))
    assert reqs["skill:python"].any_of is None
    assert reqs["skill:golang"].any_of is not None
