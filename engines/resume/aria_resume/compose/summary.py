"""Stage 5b's summary: two lines from the page's top evidence, or none (RESUME_ENGINE \u00a78).

The spec's pattern: identity, years and the strongest proof, then three or four must-have
skills that have evidence, never by implication. Each part is chosen here, from approved data,
and the model only phrases them (DECISIONS 2026-11-08):

* **identity** is one of the job titles on the page, exactly as approved; no employer;
* **years** is the counted figure's own phrase (``counted_years(...).claim()``, "2+ years"),
  or nothing when it may print none;
* **proof** is the facts of the page's highest-value bullet slots, with their metrics;
* **skills** are the 6-second test's top requirements, then the other must-haves, each with
  strong evidence and named by an approved fact, in the posting's spelling: where fewer than
  three must-haves have evidence, the next most important evidenced skill is the summary's
  to show, since the first role may prove none of them (DECISIONS 2026-11-09).

Each reply is judged as a bullet is: the schema, then the truth check (its years against the
counted figure, every other claim against the facts), the recruiter's banned words and vague
phrases, two printed lines, no first person. A failing reply is sent back with what was
wrong, at most :data:`MAX_REPAIRS` times. After that the page has no summary: a template
sentence never stands in for one (DECISIONS 2026-10-18).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aria_core.graph.counted_years import counted_years
from aria_core.schemas.snapshot import CandidateSnapshot
from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.compose.line_budget import plain_lines
from aria_resume.document import SectionKind
from aria_resume.evidence import skills_named
from aria_resume.llm import LlmClient, LlmPurpose, LlmRequest, LlmUnavailable
from aria_resume.schemas import BulletSlot, EvidenceMap, NormalizedJD, RequirementKind, ResumePlan
from aria_resume.validate import Finding
from aria_resume.validate.recruiter import check_summary_style
from aria_resume.validate.six_second import ranked_requirements
from aria_resume.validate.style import Style
from aria_resume.validate.truth import check_summary

__all__ = [
    "INSTRUCTIONS",
    "MAX_REPAIRS",
    "RESPONSE_SCHEMA",
    "SUMMARY_LINES",
    "SUMMARY_VERSION",
    "SummaryInputs",
    "SummaryResult",
    "summary_inputs",
    "write_summary",
]

SUMMARY_VERSION = "2026.11.1"
#: Printed lines the summary may take, and the planner keeps for it.
SUMMARY_LINES = 2
MAX_REPAIRS = 2
_PROOF_SLOTS = 3
_SKILLS = 4

#: Fixed text. The candidate's data arrives as task inputs, never formatted into this.
INSTRUCTIONS = (
    "Write a resume summary from the inputs, and nothing else. Return JSON matching the response_schema.\n"
    "Open with one identity from identities, copied exactly. If years_claim is not null, follow it with "
    "years_claim, copied exactly; otherwise state no number of years. Then the strongest result in "
    "proof_facts, then three or four skills from skills, in the spellings given.\n"
    "Say only what the facts say. Every number must be copied exactly from proof_facts or years_claim. "
    "Cite in fact_ids the ids of the proof facts you used, put the identity you used in identity, and list "
    "in skills_used the skills you named.\n"
    "No first person and no pronouns. Never use a word from style_rules.banned_words or a phrase from "
    "style_rules.vague_phrases. Keep it to style_rules.max_printed_lines printed lines, about 110 "
    "characters a line.\n"
    "If style_rules.fix_these is not empty, it lists what was wrong with your last attempt: fix each one."
)


class WrittenSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=400)
    identity: str = Field(min_length=1, max_length=120)
    fact_ids: list[str] = Field(min_length=1, max_length=_PROOF_SLOTS)
    skills_used: list[str] = Field(max_length=_SKILLS)


RESPONSE_SCHEMA = WrittenSummary.model_json_schema()

_FIRST_PERSON = re.compile(r"\b(?:I|me|my|mine|we|us|our|ours)\b")


@dataclass(frozen=True, slots=True)
class SummaryInputs:
    """What the summary may say, chosen from approved data before the model sees any of it."""

    identities: tuple[str, ...]
    years_claim: str | None
    #: (fact id, text, metrics) for the page's highest-value slots.
    proof: tuple[tuple[uuid.UUID, str, tuple[str, ...]], ...]
    skills: tuple[str, ...]


@dataclass
class SummaryResult:
    #: The summary line, or None when no reply passed and the page has none.
    text: str | None
    requests: int = 0
    #: Replies refused before one passed (0 when the first passed).
    repairs: int = 0
    notes: list[str] = field(default_factory=list)


def summary_inputs(
    plan: ResumePlan,
    snapshot: CandidateSnapshot,
    normalized: NormalizedJD,
    evidence: EvidenceMap,
    taxonomy: SkillTaxonomy,
) -> SummaryInputs | None:
    """None when the page has no role to name or no evidence to cite: then there is no summary."""
    roles = {experience.experience_id: experience for experience in snapshot.experiences}
    identities: list[str] = []
    slots: list[BulletSlot] = []
    for section in plan.sections:
        for entry in section.entries:
            if section.kind is SectionKind.EXPERIENCE and entry.source_id in roles:
                title = roles[entry.source_id].title.strip()
                if title and title not in identities:
                    identities.append(title)
            slots.extend(entry.bullets)
    proof: list[tuple[uuid.UUID, str, tuple[str, ...]]] = []
    for slot in sorted(slots, key=lambda slot: slot.value, reverse=True):
        fact = snapshot.fact(slot.fact_ids[0])
        if all(fact.fact_id != chosen for chosen, _, _ in proof):
            proof.append((fact.fact_id, fact.text, tuple(metric.exact_value for metric in fact.metrics)))
        if len(proof) == _PROOF_SLOTS:
            break
    if not identities or not proof:
        return None
    named = skills_named(snapshot.facts, taxonomy)
    # The summary is the top of the top third: what the 6-second test asks for leads it.
    top = [ids for _, ids in ranked_requirements(normalized, evidence, taxonomy)]
    ranked: list[tuple[int, str]] = []
    for requirement in normalized.requirements:
        spelt = requirement.surface_forms[0]
        rank = next((rank for rank, ids in enumerate(top) if requirement.id in ids), len(top))
        if (
            (requirement.must_have or rank < len(top))
            and requirement.kind is RequirementKind.SKILL
            and requirement.id in evidence.strong
            and requirement.canonical in named
            and all(spelt != skill for _, skill in ranked)
        ):
            ranked.append((rank, spelt))
    skills = [skill for _, skill in sorted(ranked, key=lambda item: item[0])]
    return SummaryInputs(
        identities=tuple(identities[:3]),
        years_claim=counted_years(snapshot).claim(),
        proof=tuple(proof),
        skills=tuple(skills[:_SKILLS]),
    )


def _request(inputs: SummaryInputs, style: Style, feedback: list[str]) -> LlmRequest:
    style_rules = {
        "banned_words": list(style.banned),
        "vague_phrases": list(style.vague_phrases),
        "max_printed_lines": SUMMARY_LINES,
        "fix_these": feedback,
    }
    return LlmRequest(
        purpose=LlmPurpose.WRITE_SUMMARY,
        trusted_instructions=INSTRUCTIONS,
        inputs={
            "identities": json.dumps(list(inputs.identities)),
            "years_claim": json.dumps(inputs.years_claim),
            "proof_facts": json.dumps(
                [
                    {"id": str(fact_id), "text": text, "metrics": list(metrics)}
                    for fact_id, text, metrics in inputs.proof
                ]
            ),
            "skills": json.dumps(list(inputs.skills)),
            "style_rules": json.dumps(style_rules, sort_keys=True),
            "response_schema": json.dumps(RESPONSE_SCHEMA, sort_keys=True),
        },
        max_output_tokens=512,
    )


def _judge(
    reply_text: str, inputs: SummaryInputs, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, style: Style
) -> tuple[str | None, list[Finding]]:
    try:
        written = WrittenSummary.model_validate_json(reply_text)
        fact_ids = [uuid.UUID(fact_id) for fact_id in written.fact_ids]
    except (ValidationError, ValueError):
        return None, [
            Finding("not_the_schema", "the reply did not match the schema: return only the JSON asked for")
        ]
    text = " ".join(written.text.split())
    findings: list[Finding] = []
    proof = {fact_id: fact_text for fact_id, fact_text, _ in inputs.proof}
    if written.identity not in inputs.identities or written.identity not in text:
        findings.append(
            Finding("identity", f"{text!r} must open with one of {list(inputs.identities)}, exactly")
        )
    elif not text.startswith(written.identity):
        findings.append(Finding("identity", f"{text!r} must open with its identity, {written.identity!r}"))
    for fact_id in fact_ids:
        if fact_id not in proof:
            findings.append(
                Finding("unknown_fact", f"{text!r} cites {fact_id}, which is not among proof_facts")
            )
    for skill in written.skills_used:
        if skill not in inputs.skills:
            findings.append(
                Finding("skill_not_given", f"{text!r} names {skill!r}, which is not among skills")
            )
    if _FIRST_PERSON.search(text):
        findings.append(Finding("first_person", f"{text!r} speaks in the first person"))
    printed = plain_lines(text)
    if printed > SUMMARY_LINES:
        findings.append(
            Finding("too_long", f"{text!r} prints {printed} lines; the summary holds {SUMMARY_LINES}")
        )
    findings += check_summary(text, snapshot, taxonomy)
    findings += check_summary_style(text, [proof[fact_id] for fact_id in fact_ids if fact_id in proof], style)
    return (None, findings) if findings else (text, [])


async def write_summary(
    plan: ResumePlan,
    snapshot: CandidateSnapshot,
    normalized: NormalizedJD,
    evidence: EvidenceMap,
    taxonomy: SkillTaxonomy,
    style: Style,
    llm: LlmClient,
) -> SummaryResult:
    """The summary line, judged and repaired, or none."""
    result = SummaryResult(text=None, notes=[f"summary: instructions {SUMMARY_VERSION}"])
    inputs = summary_inputs(plan, snapshot, normalized, evidence, taxonomy)
    if inputs is None:
        result.notes.append("summary: none, the page has no role to name or no evidence to cite")
        return result
    feedback: list[str] = []
    for attempt in range(MAX_REPAIRS + 1):
        try:
            result.requests += 1
            reply = await llm.complete(_request(inputs, style, feedback))
        except LlmUnavailable as error:
            result.notes.append(f"summary: none, the writer was unavailable ({error})")
            return result
        text, findings = _judge(reply.text, inputs, snapshot, taxonomy, style)
        if text is not None:
            result.text, result.repairs = text, attempt
            return result
        feedback = [finding.detail for finding in findings]
    result.notes.append(f"summary: none, {MAX_REPAIRS + 1} replies failed: {feedback[:3]}")
    return result
