"""Stage 4, the Evidence Matcher (RESUME_ENGINE §7): the scoring parts, then whole postings."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest
from aria_resume.jd.reader import read_jd
from aria_resume.match.embeddings import WordHashEmbedder, cosine
from aria_resume.match.matcher import MatchThresholds, match, role_type
from aria_resume.match.scoring import context_fit, metric_strength, recency, skill_match
from aria_resume.normalize.requirements import normalize
from aria_resume.schemas import (
    EvidenceMap,
    FitDecision,
    NormalizedJD,
    Requirement,
    RequirementKind,
    RoleType,
)

from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotMetric
from aria_core.taxonomy.model import SkillTaxonomy

JDS = Path(__file__).parent / "fixtures" / "jds"


def _req(canonical: str, kind: RequirementKind = RequirementKind.SKILL, **values: object) -> Requirement:
    fields: dict[str, object] = {
        "id": f"{kind.value}:{canonical}",
        "kind": kind,
        "canonical": canonical,
        "surface_forms": (canonical,),
        "weight": 1.0,
        "must_have": True,
    }
    fields.update(values)
    return Requirement.model_validate(fields)


class TestSkillMatch:
    @pytest.mark.parametrize(
        ("wanted", "how", "value"),
        [
            ("redis", "exact", 1.0),
            # Redis implies caching in the taxonomy.
            ("caching", "implied", 0.6),
            # Memcached and Redis are siblings: a partial match, for matching only.
            ("memcached", "sibling", 0.3),
            ("kafka", "none", 0.0),
        ],
    )
    def test_how_a_fact_meets_a_skill(
        self, taxonomy: SkillTaxonomy, wanted: str, how: str, value: float
    ) -> None:
        assert skill_match(_req(wanted), {"redis"}, taxonomy) == (value, how)

    def test_implication_runs_one_way(self, taxonomy: SkillTaxonomy) -> None:
        assert skill_match(_req("nextjs"), {"react"}, taxonomy) == (0.0, "none")
        assert skill_match(_req("react"), {"nextjs"}, taxonomy)[1] == "implied"

    def test_a_duty_has_no_skill_to_match(self, taxonomy: SkillTaxonomy) -> None:
        duty = _req("own-the-billing-service", RequirementKind.RESPONSIBILITY)
        assert skill_match(duty, {"redis"}, taxonomy) == (0.0, "none")


class TestTheOtherParts:
    def test_metric_strength(self, snapshot: CandidateSnapshot, ids: SimpleNamespace) -> None:
        assert metric_strength(snapshot.fact(ids.latency)) == 1.0  # 30%
        assert metric_strength(snapshot.fact(ids.screens)) == 0.6  # 15+, a count
        assert metric_strength(snapshot.fact(ids.billing)) == 0.0  # no metric
        twelve = snapshot.fact(ids.latency).model_copy(
            update={"metrics": (SnapshotMetric(exact_value="12%"),)}
        )
        assert metric_strength(twelve) == 0.8

    def test_recency(self, snapshot: CandidateSnapshot, ids: SimpleNamespace) -> None:
        # Nordvale ended Jul 2025; the snapshot is as of Oct 2026: 15 months.
        assert recency(snapshot.fact(ids.latency), snapshot) == pytest.approx(1 - 0.15 * 15 / 12)
        # VERA-RAG carries no dates.
        assert recency(snapshot.fact(ids.rag), snapshot) == 0.5
        current = snapshot.model_copy(
            update={
                "experiences": (
                    snapshot.experiences[0].model_copy(update={"end_month": None}),
                    *snapshot.experiences[1:],
                )
            }
        )
        assert recency(current.fact(ids.latency), current) == 1.0
        long_ago = snapshot.model_copy(update={"as_of": dt.date(2040, 1, 1)})
        assert recency(long_ago.fact(ids.latency), long_ago) == 0.2

    def test_context_fit_and_its_reversal_for_research(
        self, snapshot: CandidateSnapshot, ids: SimpleNamespace
    ) -> None:
        professional, personal = snapshot.fact(ids.latency), snapshot.fact(ids.rag)
        assert context_fit(professional, snapshot, research_role=False) == 1.0
        assert context_fit(personal, snapshot, research_role=False) == 0.4
        assert context_fit(professional, snapshot, research_role=True) == 0.7


class TestTheFakeEmbedder:
    def test_it_is_deterministic_and_unit_length(self) -> None:
        embedder = WordHashEmbedder()
        (first,), (second,) = embedder.embed(["Built REST APIs"]), embedder.embed(["Built REST APIs"])
        assert first == second
        assert sum(value * value for value in first) == pytest.approx(1.0)

    def test_shared_words_are_similar_and_disjoint_ones_are_not(self) -> None:
        left, near, far = WordHashEmbedder().embed(
            ["Built REST APIs in Python", "Python REST APIs for stores", "Painted watercolour landscapes"]
        )
        assert cosine(left, near) > 0.4 > cosine(left, far)

    def test_empty_text_is_a_zero_vector(self) -> None:
        (vector,) = WordHashEmbedder().embed([""])
        assert not any(vector)


def _posting(name: str, taxonomy: SkillTaxonomy) -> NormalizedJD:
    return normalize(read_jd((JDS / f"{name}.txt").read_text(encoding="utf-8"), taxonomy), taxonomy)


class TestRoleType:
    @pytest.mark.parametrize(
        ("posting", "role"),
        [
            ("larkspur_backend", RoleType.BACKEND),
            ("tidewell_frontend", RoleType.FRONTEND),
            ("corvane_ai", RoleType.AI),
        ],
    )
    def test_the_synthetic_postings(self, taxonomy: SkillTaxonomy, posting: str, role: RoleType) -> None:
        assert role_type(_posting(posting, taxonomy).requirements, taxonomy) is role

    def test_frontend_and_backend_together_are_full_stack(self, taxonomy: SkillTaxonomy) -> None:
        requirements = [_req("react"), _req("nextjs"), _req("nodejs"), _req("postgresql")]
        assert role_type(requirements, taxonomy) is RoleType.FULLSTACK

    def test_a_posting_that_names_no_skill_is_general(self, taxonomy: SkillTaxonomy) -> None:
        assert role_type([], taxonomy) is RoleType.SWE


class TestLarkspur:
    @pytest.fixture
    def evidence(self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy) -> EvidenceMap:
        return match(_posting("larkspur_backend", taxonomy), snapshot, taxonomy, WordHashEmbedder())

    def test_an_exact_skill_is_proven(self, evidence: EvidenceMap, ids: SimpleNamespace) -> None:
        assert "skill:python" in evidence.strong
        (top, *_) = evidence.per_requirement["skill:python"]
        assert (top.fact_id, top.match) == (ids.billing, "exact")

    def test_a_met_group_leaves_no_gap_for_its_other_members(self, evidence: EvidenceMap) -> None:
        gaps = {gap.requirement_id for gap in evidence.light_gaps + evidence.missing}
        assert "skill:golang" not in gaps

    def test_an_implied_skill_is_a_light_gap_with_its_closest_fact(
        self, evidence: EvidenceMap, ids: SimpleNamespace
    ) -> None:
        (sql,) = [gap for gap in evidence.light_gaps if gap.requirement_id == "skill:sql"]
        assert sql.closest_fact_id == ids.apps and sql.must_have
        assert evidence.per_requirement["skill:sql"][0].match == "implied"

    def test_a_sibling_counts_for_matching_only_partly(self, evidence: EvidenceMap) -> None:
        assert evidence.per_requirement["skill:mariadb"][0].match == "sibling"
        assert "skill:mariadb" not in evidence.strong

    def test_a_skill_no_fact_names_has_no_evidence_however_similar_the_words(
        self, evidence: EvidenceMap
    ) -> None:
        assert evidence.per_requirement["skill:docker"] == ()
        (docker,) = [gap for gap in evidence.missing if gap.requirement_id == "skill:docker"]
        assert docker.closest_fact_id is None
        assert "add it as a fact" in docker.suggestion

    def test_years_and_degree_are_checked_against_the_profile(self, evidence: EvidenceMap) -> None:
        assert "years:experience" in evidence.strong
        assert evidence.profile_checks["years:experience"] == "3.0 full-time years against 1 asked"
        assert "education:bachelor" in {gap.requirement_id for gap in evidence.missing}
        assert (
            evidence.profile_checks["education:bachelor"] == "no degree on the profile against bachelor asked"
        )

    def test_duties_are_ranked_but_are_never_gaps(self, evidence: EvidenceMap) -> None:
        duties = [key for key in evidence.per_requirement if key.startswith("responsibility:")]
        assert duties and all(evidence.per_requirement[key] for key in duties)
        judged = set(evidence.strong) | {gap.requirement_id for gap in evidence.light_gaps + evidence.missing}
        assert not judged & set(duties)

    def test_coverage_and_fit(self, evidence: EvidenceMap) -> None:
        # Must-haves: years (met), degree, Python-or-Go (met), SQL, Docker, Kubernetes.
        assert evidence.coverage == pytest.approx(2 / 6, abs=1e-4)
        assert evidence.fit_decision is FitDecision.WEAK
        assert evidence.role_type is RoleType.BACKEND

    def test_the_best_three_in_order_and_the_model_recorded(self, evidence: EvidenceMap) -> None:
        for scored in evidence.per_requirement.values():
            assert len(scored) <= 3
            assert [item.score for item in scored] == sorted((item.score for item in scored), reverse=True)
        assert evidence.embedder == "fake:word-hash-256"


class TestFit:
    def test_every_must_have_met_is_a_strong_fit(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        jd = NormalizedJD(requirements=(_req("python"), _req("redis")), taxonomy_version=taxonomy.version)
        evidence = match(jd, snapshot, taxonomy, WordHashEmbedder())
        assert (evidence.coverage, evidence.fit_decision) == (1.0, FitDecision.STRONG)

    def test_a_posting_with_nothing_read_is_no_fit_until_the_model_reads_it(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        evidence = match(
            NormalizedJD(requirements=(), taxonomy_version="t"), snapshot, taxonomy, WordHashEmbedder()
        )
        assert (evidence.coverage, evidence.fit_decision) == (0.0, FitDecision.NO_FIT)

    def test_years_the_profile_does_not_have_are_missing(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        # 36 full-time months against five years asked.
        years = _req("experience", RequirementKind.YEARS, min_years=5)
        evidence = match(
            NormalizedJD(requirements=(years,), taxonomy_version="t"), snapshot, taxonomy, WordHashEmbedder()
        )
        assert [gap.requirement_id for gap in evidence.missing] == ["years:experience"]
        assert evidence.profile_checks["years:experience"] == "3.0 full-time years against 5 asked"

    def test_the_thresholds_are_configuration(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        jd = NormalizedJD(requirements=(_req("python"),), taxonomy_version="t")
        impossible = MatchThresholds(strong=1.0)
        assert match(jd, snapshot, taxonomy, WordHashEmbedder(), thresholds=impossible).coverage == 0.0
