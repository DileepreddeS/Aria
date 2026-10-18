"""Stage 4: rank the approved facts that prove each requirement (RESUME_ENGINE §7).

For every requirement that names a skill, a domain, a soft skill or a duty, every fact
is scored on the five parts in :mod:`.scoring` and the best three are kept with their
breakdowns, so "why is this bullet here?" has an answer. Years and degree requirements
are met by the profile, not by a fact: full-time years under the default claim policy,
and the highest degree held, each explained in words.

Then the posting as a whole:

- **coverage** is the weighted share of must-haves met at the strong threshold. A group
  of alternatives ("Python or Go") counts once, met by any member;
- **strong, light gaps, missing**: every must-have and preferred requirement lands in
  one. A met group leaves no gap behind for its other members. Duties are ranked for the
  planner but are not requirements the candidate can lack, so they are in none;
- **role type** comes from what the posting asks for, weighted (§8 5a chooses a template
  from it); **fit** from coverage. A posting the rules could not read a requirement from
  has no coverage to speak of, and is no fit until the model step reads it.

Every threshold is configuration, set here and tuned on the eval (step 12).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aria_core.graph.months import months_to_years
from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotFact
from aria_core.taxonomy.model import SkillCategory, SkillTaxonomy
from aria_resume.evidence import skills_named
from aria_resume.match.embeddings import Embedder, cosine
from aria_resume.match.scoring import MatchWeights, context_fit, metric_strength, recency, skill_match
from aria_resume.normalize.requirements import DEGREE_ORDER, degree_levels
from aria_resume.schemas import (
    EvidenceMap,
    FitDecision,
    Gap,
    NormalizedJD,
    Requirement,
    RequirementKind,
    RoleType,
    ScoreBreakdown,
    ScoredFact,
)

__all__ = ["MatchThresholds", "match", "role_type"]

TOP = 3


@dataclass(frozen=True, slots=True)
class MatchThresholds:
    #: At or above: the requirement is proven.
    strong: float = 0.55
    #: At or above, below strong: a light gap, with the closest fact named.
    light: float = 0.30
    #: Coverage at or above each: strong fit, ok, weak; below weak: no fit.
    fit_strong: float = 0.75
    fit_ok: float = 0.50
    fit_weak: float = 0.30


_AI = frozenset(
    {
        "llm",
        "rag",
        "langchain",
        "langgraph",
        "prompt_engineering",
        "openai_api",
        "fine_tuning",
        "vector_database",
        "embeddings",
        "semantic_search",
        "vllm",
        "gpt",
        "claude_models",
        "llama",
        "mistral",
        "huggingface",
        "lora",
        "qlora",
        "peft",
        "rlhf",
    }
)
_DATA_ENGINEERING = frozenset(
    {
        "spark",
        "pyspark",
        "airflow",
        "dagster",
        "prefect",
        "dbt",
        "etl",
        "data_engineering",
        "data_warehousing",
        "snowflake",
        "bigquery",
        "redshift",
        "databricks",
        "data_lakes",
        "stream_processing",
        "pandas",
        "data_analysis",
        "tableau",
        "power_bi",
        "looker",
    }
)


def role_type(requirements: Sequence[Requirement], taxonomy: SkillTaxonomy) -> RoleType:
    """The template the posting calls for, from the weighted skills it asks for."""
    weights = {"ai": 0.0, "frontend": 0.0, "backend": 0.0, "data": 0.0, "devops": 0.0, "ml": 0.0}
    for requirement in requirements:
        if requirement.kind is not RequirementKind.SKILL or requirement.canonical not in taxonomy:
            continue
        canonical, weight = requirement.canonical, requirement.weight
        category = taxonomy.get(canonical).category
        if canonical in _AI:
            weights["ai"] += weight
        elif canonical in _DATA_ENGINEERING:
            weights["data"] += weight
        elif category is SkillCategory.FRONTEND:
            weights["frontend"] += weight
        elif category in (SkillCategory.BACKEND, SkillCategory.DATA):
            # Databases and queues are a backend engineer's tools.
            weights["backend"] += weight
        elif category is SkillCategory.INFRA:
            weights["devops"] += weight
        elif category is SkillCategory.ML:
            weights["ml"] += weight
    total = sum(weights.values())
    if weights["ai"] >= 1.0:
        return RoleType.AI
    if not total:
        return RoleType.SWE
    share = {name: value / total for name, value in weights.items()}
    if share["frontend"] >= 0.3 and share["backend"] >= 0.3:
        return RoleType.FULLSTACK
    for name, minimum, role in (
        ("frontend", 0.5, RoleType.FRONTEND),
        ("backend", 0.4, RoleType.BACKEND),
        ("devops", 0.5, RoleType.DEVOPS),
        ("data", 0.4, RoleType.DATA),
        ("ml", 0.4, RoleType.ML_RESEARCH),
    ):
        if share[name] >= minimum:
            return role
    return RoleType.SWE


def _label(requirement: Requirement) -> str:
    return requirement.surface_forms[0]


def _profile_check(requirement: Requirement, snapshot: CandidateSnapshot) -> tuple[bool, str]:
    if requirement.kind is RequirementKind.YEARS:
        held = months_to_years(snapshot.full_time_months)
        wanted = requirement.min_years or 0
        return held >= wanted, f"{held} full-time years against {wanted} asked"
    held_levels = [
        DEGREE_ORDER.index(level)
        for degree in snapshot.education
        for level in degree_levels(f"{degree.degree} {degree.field_of_study or ''}")
    ]
    wanted_index = DEGREE_ORDER.index(requirement.canonical)
    best = max(held_levels, default=-1)
    held_name = DEGREE_ORDER[best] if best >= 0 else "no degree on the profile"
    return best >= wanted_index, f"{held_name} against {requirement.canonical} asked"


def _gap(requirement: Requirement, best: ScoredFact | None, *, missing: bool) -> Gap:
    label = _label(requirement)
    if missing:
        suggestion = (
            f"No approved fact shows {label}. If you have done it, add it as a fact; "
            "if not, be ready to say so."
        )
    else:
        suggestion = (
            f"Your closest evidence for {label} is indirect: be ready to explain how it carries over."
        )
    return Gap(
        requirement_id=requirement.id,
        label=label,
        must_have=requirement.must_have,
        best_score=best.score if best else 0.0,
        closest_fact_id=best.fact_id if best else None,
        suggestion=suggestion,
    )


def match(
    normalized: NormalizedJD,
    snapshot: CandidateSnapshot,
    taxonomy: SkillTaxonomy,
    embedder: Embedder,
    *,
    weights: MatchWeights = MatchWeights(),  # noqa: B008  (frozen, shared safely)
    thresholds: MatchThresholds = MatchThresholds(),  # noqa: B008
) -> EvidenceMap:
    requirements = normalized.requirements
    role = role_type(requirements, taxonomy)
    research = role is RoleType.ML_RESEARCH
    facts: list[SnapshotFact] = list(snapshot.facts)
    named = {fact.fact_id: skills_named([fact], taxonomy) for fact in facts}
    fact_vectors = dict(
        zip((fact.fact_id for fact in facts), embedder.embed([f.text for f in facts]), strict=True)
    )
    scored_kinds = (
        RequirementKind.SKILL,
        RequirementKind.DOMAIN,
        RequirementKind.SOFT,
        RequirementKind.RESPONSIBILITY,
    )
    ranked_requirements = [r for r in requirements if r.kind in scored_kinds]
    requirement_vectors = dict(
        zip(
            (r.id for r in ranked_requirements),
            embedder.embed([" ".join(r.surface_forms) for r in ranked_requirements]),
            strict=True,
        )
    )
    standing = {fact.fact_id: (metric_strength(fact), recency(fact, snapshot)) for fact in facts}

    per_requirement: dict[str, tuple[ScoredFact, ...]] = {}
    best: dict[str, float] = {}
    profile_checks: dict[str, str] = {}
    for requirement in requirements:
        if requirement.kind in (RequirementKind.YEARS, RequirementKind.EDUCATION):
            met, why = _profile_check(requirement, snapshot)
            profile_checks[requirement.id] = why
            per_requirement[requirement.id] = ()
            best[requirement.id] = 1.0 if met else 0.0
            continue
        scored: list[ScoredFact] = []
        for fact in facts:
            skill, how = skill_match(requirement, named[fact.fact_id], taxonomy)
            metric, recent = standing[fact.fact_id]
            breakdown = ScoreBreakdown(
                skill_match=skill,
                semantic_sim=cosine(fact_vectors[fact.fact_id], requirement_vectors[requirement.id]),
                metric_strength=metric,
                recency=recent,
                context_fit=context_fit(fact, snapshot, research_role=research),
            )
            score = (
                weights.skill_match * breakdown.skill_match
                + weights.semantic_sim * breakdown.semantic_sim
                + weights.metric_strength * breakdown.metric_strength
                + weights.recency * breakdown.recency
                + weights.context_fit * breakdown.context_fit
            )
            # A skill requirement is proven only by a fact that names the skill, or one
            # the taxonomy relates to it: similar wording alone proves nothing.
            if requirement.kind is not RequirementKind.RESPONSIBILITY and skill == 0.0:
                continue
            scored.append(
                ScoredFact(
                    fact_id=fact.fact_id, score=round(min(score, 1.0), 4), breakdown=breakdown, match=how
                )
            )
        scored.sort(key=lambda item: (-item.score, str(item.fact_id)))
        per_requirement[requirement.id] = tuple(scored[:TOP])
        best[requirement.id] = scored[0].score if scored else 0.0

    judged = [r for r in requirements if r.kind is not RequirementKind.RESPONSIBILITY]
    met_groups = {r.any_of for r in judged if r.any_of and best[r.id] >= thresholds.strong}
    strong: list[str] = []
    light: list[Gap] = []
    missing: list[Gap] = []
    for requirement in judged:
        top = per_requirement[requirement.id][0] if per_requirement[requirement.id] else None
        if best[requirement.id] >= thresholds.strong:
            strong.append(requirement.id)
        elif requirement.any_of in met_groups:
            continue
        elif best[requirement.id] >= thresholds.light:
            light.append(_gap(requirement, top, missing=False))
        else:
            missing.append(_gap(requirement, top, missing=True))

    units: dict[str, tuple[float, bool]] = {}
    for requirement in judged:
        if not requirement.must_have:
            continue
        key = requirement.any_of or requirement.id
        weight, met = units.get(key, (0.0, False))
        units[key] = (max(weight, requirement.weight), met or best[requirement.id] >= thresholds.strong)
    total = sum(weight for weight, _ in units.values())
    coverage = sum(weight for weight, met in units.values() if met) / total if total else 0.0

    if coverage >= thresholds.fit_strong:
        fit = FitDecision.STRONG
    elif coverage >= thresholds.fit_ok:
        fit = FitDecision.OK
    elif coverage >= thresholds.fit_weak:
        fit = FitDecision.WEAK
    else:
        fit = FitDecision.NO_FIT

    return EvidenceMap(
        per_requirement=per_requirement,
        coverage=round(coverage, 4),
        strong=tuple(strong),
        light_gaps=tuple(light),
        missing=tuple(missing),
        profile_checks=profile_checks,
        role_type=role,
        fit_decision=fit,
        embedder=embedder.model_id,
    )
