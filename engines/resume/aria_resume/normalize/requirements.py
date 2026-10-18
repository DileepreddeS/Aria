"""Raw requirement lines to canonical, weighted requirements (RESUME_ENGINE §4).

Weights follow the spec: a must-have is 1.0, a preferred skill 0.5, a skill a
responsibility implies 0.7. What a line says about itself counts too:

- **alternatives.** "Python or Go", "Zustand, MobX or Redux": each skill joins one
  ``any_of`` group, and any one of them meets it. Examples after "such as", "like" or
  "e.g." are a group of their own, beside the concept they illustrate ("a vector
  database such as Pinecone or pgvector": the vector database stands; the products are
  alternatives). A skill that stands alone anywhere in the posting is in no group.
- **softening.** "preferred", "not required", "a plus", "bonus" make a required line
  preferred; "ideally" and "preferably" make what follows them preferred.
- **years.** "3+ years with React and TypeScript" puts 3 on both. "5+ years of experience
  building web applications" is the general floor, a ``years`` requirement of its own,
  and puts no years on a skill.

Each skill keeps the posting's spellings, required lines first, then preferred, then
responsibilities, so the requirement's own spelling leads; they are printed verbatim for
ATS matching. A required or preferred line that names no skill the taxonomy knows, no
degree and no years is returned as unplaced, never dropped.

Everything here is a pure function of the ParsedJD and the taxonomy: the same posting
gives the same requirements, ids included.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aria_core.importer.skills import tag_skills
from aria_core.taxonomy.model import SkillCategory, SkillTaxonomy
from aria_resume.jd.extract import years_mentions
from aria_resume.schemas import (
    JDSection,
    NormalizedJD,
    ParsedJD,
    RawRequirement,
    Requirement,
    RequirementKind,
)

__all__ = ["MUST_HAVE", "PREFERRED", "RESPONSIBILITY_IMPLIED", "normalize"]

MUST_HAVE = 1.0
PREFERRED = 0.5
RESPONSIBILITY_IMPLIED = 0.7

#: Practices that are ways of working rather than technologies.
_SOFT = frozenset({"collaboration", "leadership", "mentoring", "product_thinking", "agile", "code_review"})

_SOFTENS_LINE = re.compile(
    r"\b(?:preferred|not required|a plus|is a plus|nice to have|bonus|desirable)\b", re.IGNORECASE
)
_SOFTENS_REST = re.compile(r"\b(?:ideally|preferably|bonus if|a plus if)\b", re.IGNORECASE)
_EXAMPLES = re.compile(r"\b(?:such as|like|e\.g\.|for example|including)\b", re.IGNORECASE)
_ALTERNATIVE = re.compile(r"\bor\b|/", re.IGNORECASE)

_DEGREE_LEVELS = (
    ("associate", re.compile(r"\bassociate'?s\b", re.IGNORECASE)),
    (
        "bachelor",
        re.compile(r"\b(?:B\.?S\.?|B\.?A\.?|BSc|BEng|bachelor'?s|undergraduate|four-year)\b", re.IGNORECASE),
    ),
    ("master", re.compile(r"\b(?:M\.?S\.?|MSc|MEng|master'?s|graduate degree)\b", re.IGNORECASE)),
    ("phd", re.compile(r"\b(?:Ph\.?D\.?|doctorate)\b", re.IGNORECASE)),
)


@dataclass
class _Draft:
    kind: RequirementKind
    canonical: str
    surface_forms: list[str] = field(default_factory=list)
    weight: float = 0.0
    must_have: bool = False
    min_years: int | None = None
    groups: list[str | None] = field(default_factory=list)


def _kind(canonical: str, taxonomy: SkillTaxonomy) -> RequirementKind:
    if canonical in _SOFT:
        return RequirementKind.SOFT
    if taxonomy.get(canonical).category is SkillCategory.DOMAIN:
        return RequirementKind.DOMAIN
    return RequirementKind.SKILL


def _slug(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.lower())[:8])


class _Collector:
    def __init__(self, taxonomy: SkillTaxonomy) -> None:
        self.taxonomy = taxonomy
        self.drafts: dict[str, _Draft] = {}
        self.unplaced: list[RawRequirement] = []
        self.groups = 0

    def add(
        self,
        kind: RequirementKind,
        canonical: str,
        surface: str,
        *,
        weight: float,
        must_have: bool,
        min_years: int | None = None,
        group: str | None = None,
        grouping: bool = True,
    ) -> None:
        key = f"{kind.value}:{canonical}"
        draft = self.drafts.setdefault(key, _Draft(kind, canonical))
        if surface not in draft.surface_forms:
            draft.surface_forms.append(surface)
        draft.weight = max(draft.weight, weight)
        draft.must_have = draft.must_have or must_have
        if min_years is not None:
            draft.min_years = max(draft.min_years or 0, min_years)
        if grouping:
            draft.groups.append(group)

    def new_group(self) -> str:
        self.groups += 1
        return f"g{self.groups}"

    def line(self, item: RawRequirement, *, weight: float, must_have: bool) -> None:
        text = item.text
        if must_have and _SOFTENS_LINE.search(text):
            weight, must_have = PREFERRED, False
        placed = False

        mentions = years_mentions(text)
        general = [mention for mention in mentions if mention.general]
        if general and item.section is not JDSection.RESPONSIBILITIES:
            placed = True
            self.add(
                RequirementKind.YEARS,
                "experience",
                text,
                weight=weight,
                must_have=must_have,
                min_years=max(mention.low for mention in general),
            )
        skill_years = max((mention.low for mention in mentions if not mention.general), default=None)

        if item.section is not JDSection.RESPONSIBILITIES:
            levels = [
                level for level, pattern in _DEGREE_LEVELS if pattern.search(text.replace("\u2019", "'"))
            ]
            if levels:
                placed = True
                self.add(RequirementKind.EDUCATION, levels[0], text, weight=weight, must_have=must_have)

        softened = _SOFTENS_REST.search(text)
        examples = _EXAMPLES.search(text)
        skills = list(tag_skills(text, self.taxonomy).skills)
        before_examples = [s for s in skills if not examples or text.find(s.surface_form) < examples.start()]
        after_examples = [s for s in skills if s not in before_examples]
        # Alternatives among the skills named before any "such as"; the examples after it
        # are alternatives to each other, the concept before it stands.
        main_group = (
            self.new_group()
            if len(before_examples) > 1
            and _ALTERNATIVE.search(text[: examples.start() if examples else len(text)])
            else None
        )
        example_group = self.new_group() if len(after_examples) > 1 else None
        for skill in skills:
            placed = True
            position = text.find(skill.surface_form)
            soft_here = softened is not None and position > softened.start()
            self.add(
                _kind(skill.canonical, self.taxonomy),
                skill.canonical,
                skill.surface_form,
                weight=PREFERRED if soft_here and must_have else weight,
                must_have=must_have and not soft_here,
                # A general floor is not this skill's; only years the line puts on skills.
                min_years=skill_years,
                group=example_group if skill in after_examples else main_group,
                # A responsibility implies a skill; it does not say the skill stands alone.
                grouping=item.section is not JDSection.RESPONSIBILITIES,
            )
        if not placed and item.section is not JDSection.RESPONSIBILITIES:
            self.unplaced.append(item)

    def result(self) -> tuple[Requirement, ...]:
        requirements = []
        for key, draft in self.drafts.items():
            # Standing alone anywhere means it is wanted for itself, not as one option.
            group = None if not draft.groups or None in draft.groups else draft.groups[0]
            requirements.append(
                Requirement(
                    id=key,
                    kind=draft.kind,
                    canonical=draft.canonical,
                    surface_forms=tuple(draft.surface_forms),
                    weight=draft.weight,
                    must_have=draft.must_have,
                    min_years=draft.min_years,
                    any_of=group,
                )
            )
        return tuple(requirements)


def normalize(parsed: ParsedJD, taxonomy: SkillTaxonomy) -> NormalizedJD:
    collector = _Collector(taxonomy)
    for item in parsed.required:
        collector.line(item, weight=MUST_HAVE, must_have=True)
    for item in parsed.preferred:
        collector.line(item, weight=PREFERRED, must_have=False)
    for text in parsed.responsibilities:
        item = RawRequirement(text=text, section=JDSection.RESPONSIBILITIES)
        collector.line(item, weight=RESPONSIBILITY_IMPLIED, must_have=False)
        collector.add(
            RequirementKind.RESPONSIBILITY,
            _slug(text),
            text,
            weight=RESPONSIBILITY_IMPLIED,
            must_have=False,
            grouping=False,
        )
    return NormalizedJD(
        requirements=collector.result(),
        unplaced=tuple(collector.unplaced),
        taxonomy_version=taxonomy.version,
    )
