"""Stage 5b, the writer (RESUME_ENGINE §8): one bullet per slot, from that slot's facts alone.

The model is given only what the slot needs: the cited facts' text, their metrics exactly as
written, the technologies those facts name (in the taxonomy's spellings), the requirement the
slot serves by the taxonomy's name for it, never the posting's words (a posting is untrusted
text, and nothing from it reaches the writer), and the style rules. It returns a bullet and
what it used, and code decides:

- 6b's written-bullet check: the facts it cites are the ones it was given, every number and
  technology is backed by them, every metric it declares is printed exactly;
- 6c's per-bullet rules: an allowed opening verb, no banned word, no vague phrase;
- the slot's line budget, measured with the page's own glyph widths.

A failing bullet gets the findings back and one rewrite; then, once, another fact that proves
the same requirement from the same role; then the facts' own words, which pass 6b by
construction (MAX_REPAIRS = 2, RESUME_ENGINE §9). Verbs are kept apart before the fact, not
repaired after it: each request names the verbs the page has already used up. If the model
is unavailable (not opted in, over a cap, down), every remaining slot keeps its fact's words.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotFact
from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.compose.fallback import SlotKey, fact_bullet
from aria_resume.compose.line_budget import bullet_lines
from aria_resume.document import DocumentBullet
from aria_resume.evidence import skills_named
from aria_resume.llm import LlmClient, LlmPurpose, LlmRequest, LlmUnavailable
from aria_resume.schemas import (
    Bullet,
    BulletSlot,
    EvidenceMap,
    NormalizedJD,
    RequirementKind,
    ResumePlan,
)
from aria_resume.validate import Finding
from aria_resume.validate.recruiter import check_bullet_style, opening_verb
from aria_resume.validate.style import Style
from aria_resume.validate.truth import check_written

__all__ = ["INSTRUCTIONS", "MAX_REPAIRS", "RESPONSE_SCHEMA", "WRITER_VERSION", "WriterStats", "write_page"]

WRITER_VERSION = "2026.10.1"
MAX_REPAIRS = 2

#: Fixed text. Facts arrive as task inputs, never formatted into this.
INSTRUCTIONS = (
    "Write one resume bullet from the facts in fact_texts, and nothing else. Return JSON matching the "
    "response_schema.\n"
    "Say only what the facts say. Every number must be copied exactly from metrics, and listed in "
    "metrics_used. Name only technologies from allowed_tech, listed in tech_used. Cite in fact_ids the ids "
    "of the facts you used, from fact_texts.\n"
    "Open with one verb from style_rules.allowed_verbs, and not with any verb in "
    "style_rules.avoid_opening_verbs. Never use a word from style_rules.banned_words or a phrase from "
    "style_rules.vague_phrases. Keep it to style_rules.max_printed_lines printed lines, about 110 "
    "characters a line.\n"
    "Lead with what the facts show about target_requirement, if they show it.\n"
    "If style_rules.fix_these is not empty, it lists what was wrong with your last attempt: fix each one."
)


class WrittenBullet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=400)
    fact_ids: list[str] = Field(min_length=1, max_length=4)
    metrics_used: list[str] = Field(max_length=8)
    tech_used: list[str] = Field(max_length=12)


RESPONSE_SCHEMA = WrittenBullet.model_json_schema()


@dataclass
class WriterStats:
    """How each bullet came to be, so the eval can show whether effort "low" holds."""

    first_try: int = 0
    rewritten: int = 0
    swapped: int = 0
    fallback: int = 0
    requests: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "first_try": self.first_try,
            "rewritten": self.rewritten,
            "swapped": self.swapped,
            "fallback": self.fallback,
            "requests": self.requests,
        }


def _requirement_names(slot: BulletSlot, normalized: NormalizedJD, taxonomy: SkillTaxonomy) -> list[str]:
    """What the slot serves, by the taxonomy's name for it, never the posting's text."""
    by_id = {requirement.id: requirement for requirement in normalized.requirements}
    names: list[str] = []
    for requirement_id in slot.requirement_ids:
        requirement = by_id.get(requirement_id)
        if requirement is None:
            continue
        if requirement.kind in (RequirementKind.SKILL, RequirementKind.DOMAIN, RequirementKind.SOFT):
            names.append(taxonomy.display_form(requirement.canonical))
        else:
            names.append(requirement.kind.value)
    return names


def _request(
    facts: Sequence[SnapshotFact],
    requirements: list[str],
    style: Style,
    taxonomy: SkillTaxonomy,
    *,
    avoid: set[str],
    max_lines: int,
    feedback: list[str],
) -> LlmRequest:
    allowed = sorted(taxonomy.display_form(canonical) for canonical in skills_named(facts, taxonomy))
    style_rules = {
        "allowed_verbs": list(style.allowed_verbs),
        "avoid_opening_verbs": sorted(avoid),
        "banned_words": list(style.banned),
        "vague_phrases": list(style.vague_phrases),
        "max_printed_lines": max_lines,
        "fix_these": feedback,
    }
    return LlmRequest(
        purpose=LlmPurpose.WRITE_BULLET,
        trusted_instructions=INSTRUCTIONS,
        inputs={
            "fact_texts": json.dumps([{"id": str(fact.fact_id), "text": fact.text} for fact in facts]),
            "metrics": json.dumps([metric.exact_value for fact in facts for metric in fact.metrics]),
            "allowed_tech": json.dumps(allowed),
            "target_requirement": json.dumps(requirements),
            "style_rules": json.dumps(style_rules, sort_keys=True),
        },
        max_output_tokens=1024,
    )


def _judge(
    reply_text: str,
    slot: BulletSlot,
    provided: tuple[uuid.UUID, ...],
    snapshot: CandidateSnapshot,
    taxonomy: SkillTaxonomy,
    style: Style,
    avoid: set[str],
) -> tuple[DocumentBullet | None, list[Finding]]:
    try:
        written = WrittenBullet.model_validate_json(reply_text)
        fact_ids = tuple(uuid.UUID(fact_id) for fact_id in written.fact_ids)
    except (ValidationError, ValueError):
        return None, [
            Finding("not_the_schema", "the reply did not match the schema: return only the JSON asked for")
        ]
    bullet = Bullet(
        text=" ".join(written.text.split()),
        fact_ids=fact_ids,
        metrics_used=tuple(written.metrics_used),
        tech_used=tuple(written.tech_used),
        requirement_ids=slot.requirement_ids,
    )
    findings = check_written(bullet, snapshot, taxonomy, provided=provided)
    cited = [snapshot.fact(fact_id).text for fact_id in fact_ids if fact_id in provided]
    findings += check_bullet_style(bullet.text, cited, style)
    printed = bullet_lines(bullet.text)
    if printed > slot.lines:
        findings.append(
            Finding("too_long", f"{bullet.text!r} prints {printed} lines; its slot holds {slot.lines}")
        )
    verb = opening_verb(bullet.text, style)
    if verb is not None and verb in avoid:
        findings.append(
            Finding("repeated_verb", f"{bullet.text!r} opens with {verb!r}, which the page has used up")
        )
    if findings:
        return None, findings
    return DocumentBullet(text=bullet.text, fact_ids=bullet.fact_ids), []


def _alternative(
    slot: BulletSlot, evidence: EvidenceMap, snapshot: CandidateSnapshot, on_page: set[uuid.UUID]
) -> uuid.UUID | None:
    """Another fact proving the slot's requirements, from the same role or project, not on the page."""
    first = snapshot.fact(slot.fact_ids[0])
    source = (first.experience_id, first.project_id)
    ranked = sorted(
        (
            scored
            for requirement_id in slot.requirement_ids
            for scored in evidence.per_requirement.get(requirement_id, ())
        ),
        key=lambda scored: scored.score,
        reverse=True,
    )
    for scored in ranked:
        fact = snapshot.fact(scored.fact_id)
        if scored.fact_id not in on_page and (fact.experience_id, fact.project_id) == source:
            return scored.fact_id
    return None


async def write_page(
    plan: ResumePlan,
    snapshot: CandidateSnapshot,
    normalized: NormalizedJD,
    evidence: EvidenceMap,
    taxonomy: SkillTaxonomy,
    style: Style,
    llm: LlmClient,
) -> tuple[dict[SlotKey, DocumentBullet], WriterStats]:
    """Every slot written, rewritten, swapped or left in its fact's words; in reading order."""
    stats = WriterStats(notes=[f"writer: instructions {WRITER_VERSION}"])
    written: dict[SlotKey, DocumentBullet] = {}
    on_page = {
        fact_id
        for section in plan.sections
        for entry in section.entries
        for slot in entry.bullets
        for fact_id in slot.fact_ids
    }
    counts: Counter[str] = Counter()
    previous: str | None = None
    available = True

    for s, section in enumerate(plan.sections):
        for e, entry in enumerate(section.entries):
            for b, slot in enumerate(entry.bullets):
                avoid = {verb for verb, count in counts.items() if count >= style.max_bullets_per_verb}
                if previous is not None:
                    avoid.add(previous)
                provided = slot.fact_ids
                feedback: list[str] = []
                swapped = False
                result: DocumentBullet | None = None
                attempt = 0
                while available and attempt <= MAX_REPAIRS and result is None:
                    if attempt == MAX_REPAIRS:
                        alternative = _alternative(slot, evidence, snapshot, on_page)
                        if alternative is not None:
                            provided, feedback, swapped = (alternative,), [], True
                    facts = [snapshot.fact(fact_id) for fact_id in provided]
                    request = _request(
                        facts,
                        _requirement_names(slot, normalized, taxonomy),
                        style,
                        taxonomy,
                        avoid=avoid,
                        max_lines=slot.lines,
                        feedback=feedback,
                    )
                    try:
                        stats.requests += 1
                        reply = await llm.complete(request)
                    except LlmUnavailable as error:
                        available = False
                        stats.notes.append(f"writer unavailable, facts' own words from here on ({error})")
                        break
                    result, findings = _judge(reply.text, slot, provided, snapshot, taxonomy, style, avoid)
                    feedback = [finding.detail for finding in findings]
                    attempt += 1
                if result is None:
                    result = fact_bullet(slot, snapshot)
                    stats.fallback += 1
                elif swapped:
                    stats.swapped += 1
                    on_page.add(provided[0])
                elif attempt == 1:
                    stats.first_try += 1
                else:
                    stats.rewritten += 1
                written[(s, e, b)] = result
                previous = opening_verb(result.text, style)
                if previous is not None:
                    counts[previous] += 1
    return written, stats
