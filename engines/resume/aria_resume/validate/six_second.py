"""6c's 6-second test: does the top third of the page show the job's top requirements?

RESUME_ENGINE \u00a71 and \u00a79 6c: the top third (the summary, the first three bullets, the first
skills line) must show the job's three most important requirements; fewer than two shown is a
finding naming the missing ones. Only requirements the candidate has strong evidence for are
weighed: one the graph cannot prove is already a gap, reported as one, and no page could show
it truthfully (DECISIONS 2026-11-09). A model reads the page, as a recruiter would, through the
gateway; code decides what counts (DECISIONS 2026-11-08):

* the requirements are the taxonomy's names for them, never the posting's words: the posting is
  untrusted, the names are ours;
* they are the three most important among those the evidence map holds as strong, the same
  "evidenced" the ATS check's keywords are;
* the top third is built from the document, in reading order, which the ATS check (6a) holds
  the printed page to;
* a requirement counts as shown only when the model quotes where, and the quote is found in
  the top third word for word. A "yes" without a true quote is a no.

With no model, or one that cannot be reached, the check is not run and says so in a note: it
is a judgement only a reader can make, and CI's pages are printed without one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.document import ResumeDocument, SectionKind
from aria_resume.llm import LlmClient, LlmPurpose, LlmRequest, LlmUnavailable
from aria_resume.schemas import EvidenceMap, NormalizedJD, RequirementKind
from aria_resume.validate import Finding

__all__ = [
    "INSTRUCTIONS",
    "RESPONSE_SCHEMA",
    "SIX_SECOND_VERSION",
    "SixSecondResult",
    "ranked_requirements",
    "six_second_check",
    "top_requirements",
    "top_third",
]

SIX_SECOND_VERSION = "2026.11.2"
_TOP = 3
_REQUIRED = 2
_NAMED_KINDS = (RequirementKind.SKILL, RequirementKind.DOMAIN, RequirementKind.SOFT)

#: Fixed text. The page and the requirement names arrive as task inputs.
INSTRUCTIONS = (
    "You are a recruiter with six seconds. For each name in top_requirements, say whether "
    "rendered_top_third shows that the candidate has it. Return JSON matching the response_schema, one "
    "entry per requirement, in the order given.\n"
    "When it is shown, copy into quote the shortest passage of rendered_top_third that shows it, exactly as "
    "written. When it is not shown, set shown to false and quote to an empty string. Judge only what the "
    "text says; never assume."
)


class _Seen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    requirement: str = Field(min_length=1, max_length=120)
    shown: bool
    quote: str = Field(max_length=300)


class _Reading(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    requirements: list[_Seen] = Field(max_length=_TOP)


RESPONSE_SCHEMA = _Reading.model_json_schema()


@dataclass
class SixSecondResult:
    findings: list[Finding] = field(default_factory=list)
    #: Requirement names the top third was shown to prove, with the quote that proves each.
    shown: dict[str, str] = field(default_factory=dict)
    asked: tuple[str, ...] = ()
    ran: bool = False
    notes: list[str] = field(default_factory=list)


def ranked_requirements(
    normalized: NormalizedJD, evidence: EvidenceMap, taxonomy: SkillTaxonomy
) -> tuple[tuple[str, frozenset[str]], ...]:
    """The three most important nameable requirements the candidate can prove, each with the
    requirement ids its name stands for: must-haves first, then by weight. The planner leads
    the page with them, and this test asks for them."""
    proven = set(evidence.strong)
    ranked = sorted(
        (
            requirement
            for requirement in normalized.requirements
            if requirement.kind in _NAMED_KINDS and requirement.id in proven
        ),
        key=lambda requirement: (not requirement.must_have, -requirement.weight),
    )
    ids: dict[str, set[str]] = {}
    for requirement in ranked:
        name = taxonomy.display_form(requirement.canonical)
        if name in ids or len(ids) < _TOP:
            ids.setdefault(name, set()).add(requirement.id)
    return tuple((name, frozenset(found)) for name, found in ids.items())


def top_requirements(
    normalized: NormalizedJD, evidence: EvidenceMap, taxonomy: SkillTaxonomy
) -> tuple[str, ...]:
    """The names of :func:`ranked_requirements`, most important first."""
    return tuple(name for name, _ in ranked_requirements(normalized, evidence, taxonomy))


def top_third(document: ResumeDocument) -> str:
    """The summary, the first three bullets and the first skills line, in reading order."""
    parts: list[str] = []
    for section in document.sections:
        if section.kind is SectionKind.SUMMARY:
            parts.extend(section.lines)
    bullets = [
        bullet.text for section in document.sections for entry in section.entries for bullet in entry.bullets
    ]
    parts.extend(bullets[:3])
    skills = [
        line for section in document.sections if section.kind is SectionKind.SKILLS for line in section.lines
    ]
    parts.extend(skills[:1])
    return "\n".join(parts)


def _collapsed(text: str) -> str:
    return " ".join(text.split()).casefold()


async def six_second_check(
    document: ResumeDocument,
    normalized: NormalizedJD,
    evidence: EvidenceMap,
    taxonomy: SkillTaxonomy,
    llm: LlmClient,
) -> SixSecondResult:
    asked = top_requirements(normalized, evidence, taxonomy)
    result = SixSecondResult(asked=asked, notes=[f"six-second: instructions {SIX_SECOND_VERSION}"])
    if not asked:
        result.notes.append(
            "six-second: not run, the candidate has strong evidence for no requirement the taxonomy names"
        )
        return result
    page = top_third(document)
    request = LlmRequest(
        purpose=LlmPurpose.SIX_SECOND_CHECK,
        trusted_instructions=INSTRUCTIONS,
        inputs={
            "top_requirements": json.dumps(list(asked)),
            "rendered_top_third": page,
            "response_schema": json.dumps(RESPONSE_SCHEMA, sort_keys=True),
        },
        max_output_tokens=512,
    )
    try:
        reply = await llm.complete(request)
    except LlmUnavailable as error:
        result.notes.append(f"six-second: not run, the model was unavailable ({error})")
        return result
    result.ran = True
    try:
        reading = _Reading.model_validate_json(reply.text)
    except ValidationError:
        result.notes.append("six-second: the reply did not match the schema; nothing counted as shown")
        reading = _Reading(requirements=[])
    text = _collapsed(page)
    for seen in reading.requirements:
        quote = _collapsed(seen.quote)
        if seen.requirement in asked and seen.shown and quote and quote in text:
            result.shown.setdefault(seen.requirement, seen.quote)
        elif seen.requirement in asked and seen.shown:
            result.notes.append(
                f"six-second: {seen.requirement!r} claimed shown with a quote the page does not hold"
            )
    required = min(_REQUIRED, len(asked))
    if len(result.shown) < required:
        missing = [name for name in asked if name not in result.shown]
        result.findings.append(
            Finding(
                "six_second",
                f"the top third shows {len(result.shown)} of the top {len(asked)} requirements; "
                f"missing {missing}",
            )
        )
    return result
