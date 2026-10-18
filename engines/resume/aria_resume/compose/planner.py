"""Stage 5a, the Planner (RESUME_ENGINE §8): which approved facts go where, before a word is written.

Deterministic, no model. In order:

1. **Entries.** Roles reverse-chronologically; projects by the evidence they carry, the
   template's number at most and none that proves nothing; every approved degree.
2. **Value.** A fact is worth the requirement weight it carries: weight x score, summed over
   the requirements it is among the best three for (Stage 4).
3. **Must-haves first.** The best fact for every must-have with evidence is a mandatory
   bullet (one per group of alternatives), removed only after every other bullet.
4. **Fill.** Each entry up to its template minimum (the most recent role 3, older roles 2,
   projects 2), then by value up to its maximum; no fact twice.
5. **The page.** Heights are estimated (:mod:`.line_budget`). Over the page, the lowest-value
   bullet goes first; a role never keeps fewer than two, and one that cannot hold two
   keeps its header alone, employer, title and months, so the page shows no gap in
   employment (DECISIONS 2026-10-18). Projects go whole; degrees never.
6. **Skills lines.** Every verified skill (declared, or named by an approved fact) and the
   allowlisted implications of those (implied_skills.yaml), by the template's categories,
   the posting's own skills first, in the posting's spelling when it names the same skill
   (never a sibling's).

The output, a :class:`ResumePlan`, is what the writer (5b) fills and what the fallback
(:mod:`.fallback`) turns into a page with the facts' own words.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotFact
from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.compose import line_budget
from aria_resume.compose.headers import degree_header, project_header, role_header
from aria_resume.compose.line_budget import PAGE, Page
from aria_resume.compose.templates import RoleTemplate, load_template
from aria_resume.document import SectionKind
from aria_resume.evidence import skills_named
from aria_resume.schemas import (
    BulletSlot,
    EvidenceMap,
    NormalizedJD,
    PlannedEntry,
    PlannedSection,
    RequirementKind,
    ResumePlan,
)
from aria_resume.validate.implied import ImpliedSkills, load_implied_skills

__all__ = ["PlanError", "plan"]

#: A mandatory bullet's value is raised by this, so it outlasts every other bullet.
_MANDATORY = 1000.0


class PlanError(ValueError):
    """The snapshot lacks what a page needs (an approved name)."""


@dataclass
class _Entry:
    kind: SectionKind
    source_id: uuid.UUID
    facts: list[SnapshotFact]
    minimum: int
    maximum: int
    #: The header prints a second line (client, place): one more line of height.
    subline: bool = False
    chosen: list[BulletSlot] = field(default_factory=list)
    header_only: bool = False
    dropped_whole: bool = False


def _fact_values(
    evidence: EvidenceMap, normalized: NormalizedJD
) -> tuple[dict[uuid.UUID, float], dict[uuid.UUID, list[str]]]:
    weights = {requirement.id: requirement.weight for requirement in normalized.requirements}
    values: dict[uuid.UUID, float] = defaultdict(float)
    serves: dict[uuid.UUID, list[str]] = defaultdict(list)
    for requirement_id, scored in evidence.per_requirement.items():
        for item in scored:
            values[item.fact_id] += weights.get(requirement_id, 0.0) * item.score
            serves[item.fact_id].append(requirement_id)
    return values, serves


def _mandatory(evidence: EvidenceMap, normalized: NormalizedJD, placeable: set[uuid.UUID]) -> set[uuid.UUID]:
    """The best placeable fact for each must-have with evidence; one per group of alternatives."""
    gaps = {gap.requirement_id for gap in evidence.missing}
    best_by_unit: dict[str, tuple[float, uuid.UUID]] = {}
    for requirement in normalized.requirements:
        if not requirement.must_have or requirement.kind in (
            RequirementKind.RESPONSIBILITY,
            RequirementKind.YEARS,
            RequirementKind.EDUCATION,
        ):
            continue
        if requirement.id in gaps:
            continue
        for item in evidence.per_requirement.get(requirement.id, ()):
            if item.fact_id in placeable:
                unit = requirement.any_of or requirement.id
                if unit not in best_by_unit or item.score > best_by_unit[unit][0]:
                    best_by_unit[unit] = (item.score, item.fact_id)
                break
    return {fact_id for _, fact_id in best_by_unit.values()}


def _slot(fact: SnapshotFact, value: float, serves: list[str], mandatory: bool) -> BulletSlot:
    return BulletSlot(
        fact_ids=(fact.fact_id,),
        requirement_ids=tuple(dict.fromkeys(serves)),
        value=round(value, 4),
        mandatory=mandatory,
        lines=line_budget.bullet_lines(fact.text),
    )


def _skills_lines(
    snapshot: CandidateSnapshot,
    normalized: NormalizedJD,
    taxonomy: SkillTaxonomy,
    template: RoleTemplate,
    implied: ImpliedSkills,
) -> tuple[list[str], dict[str, str]]:
    spellings: dict[str, str] = {}
    for skill in snapshot.declared_skills:
        canonical = taxonomy.resolve(skill.surface_form)
        if canonical:
            spellings.setdefault(canonical, skill.surface_form)
    for fact in snapshot.facts:
        for skill in fact.skills:
            canonical = taxonomy.resolve(skill.surface_form)
            if canonical:
                spellings.setdefault(canonical, skill.surface_form)
    held = set(spellings) | skills_named(snapshot.facts, taxonomy)
    listable = implied.listable(held)

    asked: dict[str, tuple[float, str]] = {}
    for requirement in normalized.requirements:
        if requirement.kind is RequirementKind.SKILL and requirement.canonical in listable:
            # The posting's spelling names the same skill (an alias resolves to this id).
            asked[requirement.canonical] = (requirement.weight, requirement.surface_forms[0])
    surface: dict[str, str] = {}
    lines: list[str] = []
    labels = {
        "language": "Languages",
        "frontend": "Frontend",
        "backend": "Backend",
        "data": "Data",
        "ml": "Machine Learning",
        "infra": "Cloud & DevOps",
        "tooling": "Tools",
        "practice": "Practices",
        "domain": "Domains",
    }
    for category in template.skills.categories:
        members = [c for c in listable if c in taxonomy and taxonomy.get(c).category is category]
        members.sort(
            key=lambda c: (c not in asked, -asked.get(c, (0.0, ""))[0], taxonomy.get(c).display.lower())
        )
        terms = []
        for canonical in members[: template.skills.max_terms_per_line]:
            spelling = (
                asked[canonical][1]
                if canonical in asked
                else spellings.get(canonical) or taxonomy.get(canonical).display
            )
            surface[canonical] = spelling
            terms.append(spelling)
        if terms:
            lines.append(f"{labels[category.value]}: {', '.join(terms)}")
    return lines, surface


def _height(
    entries: dict[SectionKind, list[_Entry]], skills: list[str], page: Page, order: tuple[SectionKind, ...]
) -> float:
    height = page.name + page.line + page.contact_gap
    for kind in order:
        if kind is SectionKind.SKILLS:
            if skills:
                height += (
                    page.section_gap
                    + page.heading
                    + sum(line_budget.plain_lines(line) * page.line for line in skills)
                )
            continue
        kept = [entry for entry in entries.get(kind, []) if not entry.dropped_whole]
        if not kept:
            continue
        height += page.section_gap + page.heading
        for entry in kept:
            height += page.entry_gap + page.line + (page.line if entry.subline else 0.0)
            height += sum(slot.lines for slot in entry.chosen) * page.line
    return height


def plan(
    normalized: NormalizedJD,
    evidence: EvidenceMap,
    snapshot: CandidateSnapshot,
    taxonomy: SkillTaxonomy,
    *,
    template: RoleTemplate | None = None,
    implied: ImpliedSkills | None = None,
    page: Page = PAGE,
) -> ResumePlan:
    if not snapshot.contact.full_name:
        raise PlanError("the snapshot has no approved name: a page cannot be planned without one")
    template = template or load_template(evidence.role_type)
    implied = implied or load_implied_skills(taxonomy)
    values, serves = _fact_values(evidence, normalized)

    by_anchor: dict[uuid.UUID, list[SnapshotFact]] = defaultdict(list)
    for fact in snapshot.facts:
        anchor = fact.experience_id or fact.project_id or fact.education_id
        if anchor is not None:
            by_anchor[anchor].append(fact)

    # Current roles first, then by the month each ended, then by the month it began.
    roles = sorted(
        snapshot.experiences,
        key=lambda role: (
            role.end_month is not None,
            -(role.end_month or role.start_month).toordinal(),
            -role.start_month.toordinal(),
        ),
    )
    entries: dict[SectionKind, list[_Entry]] = defaultdict(list)
    for index, role in enumerate(roles):
        low, high = template.recent_role_bullets if index == 0 else template.older_role_bullets
        entries[SectionKind.EXPERIENCE].append(
            _Entry(
                SectionKind.EXPERIENCE,
                role.experience_id,
                by_anchor[role.experience_id],
                low,
                high,
                subline=role_header(role).subline is not None,
            )
        )
    projects = sorted(
        (
            project
            for project in snapshot.projects
            if any(values.get(f.fact_id, 0.0) > 0 for f in by_anchor[project.project_id])
        ),
        key=lambda project: (
            -sum(values.get(f.fact_id, 0.0) for f in by_anchor[project.project_id]),
            project.name,
        ),
    )[: template.max_projects]
    for project in projects:
        low, high = template.project_bullets
        entries[SectionKind.PROJECTS].append(
            _Entry(
                SectionKind.PROJECTS,
                project.project_id,
                by_anchor[project.project_id],
                low,
                high,
                subline=project_header(project).subline is not None,
            )
        )
    degrees = sorted(
        snapshot.education,
        key=lambda degree: -(degree.end_month or degree.start_month or dt.date(1900, 1, 1)).toordinal(),
    )
    for degree in degrees:
        entries[SectionKind.EDUCATION].append(
            _Entry(
                SectionKind.EDUCATION,
                degree.education_id,
                [],
                0,
                0,
                subline=degree_header(degree).subline is not None,
            )
        )

    placeable = {
        fact.fact_id
        for kind in (SectionKind.EXPERIENCE, SectionKind.PROJECTS)
        for entry in entries[kind]
        for fact in entry.facts
    }
    mandatory = _mandatory(evidence, normalized, placeable)

    def worth(fact: SnapshotFact) -> float:
        return values.get(fact.fact_id, 0.0) + (_MANDATORY if fact.fact_id in mandatory else 0.0)

    for kind in (SectionKind.EXPERIENCE, SectionKind.PROJECTS):
        for entry in entries[kind]:
            ranked = sorted(
                entry.facts, key=lambda fact: (-worth(fact), -len(fact.metrics), str(fact.fact_id))
            )
            for fact in ranked:
                below_minimum = len(entry.chosen) < entry.minimum
                if len(entry.chosen) >= entry.maximum:
                    break
                if below_minimum or worth(fact) > 0:
                    entry.chosen.append(
                        _slot(
                            fact,
                            values.get(fact.fact_id, 0.0),
                            serves.get(fact.fact_id, []),
                            fact.fact_id in mandatory,
                        )
                    )

    skills, surface = _skills_lines(snapshot, normalized, taxonomy, template, implied)
    dropped: list[uuid.UUID] = []

    def slot_worth(slot: BulletSlot) -> float:
        return slot.value + (_MANDATORY if slot.mandatory else 0.0)

    while _height(entries, skills, page, template.section_order) > page.usable_height:
        options: list[tuple[float, int, _Entry, BulletSlot | None]] = []
        for kind in (SectionKind.EXPERIENCE, SectionKind.PROJECTS):
            for entry in entries[kind]:
                if entry.dropped_whole or not entry.chosen:
                    continue
                if len(entry.chosen) > 2:
                    weakest = min(entry.chosen, key=slot_worth)
                    options.append((slot_worth(weakest), weakest.lines, entry, weakest))
                else:
                    # Two or fewer: the bullets go together, never down to one.
                    options.append((sum(slot_worth(slot) for slot in entry.chosen), 0, entry, None))
        if not options:
            break
        _, _, entry, slot = min(options, key=lambda option: (option[0], -option[1]))
        if slot is not None:
            entry.chosen.remove(slot)
            dropped.extend(slot.fact_ids)
        else:
            dropped.extend(fact_id for item in entry.chosen for fact_id in item.fact_ids)
            entry.chosen = []
            if entry.kind is SectionKind.PROJECTS:
                entry.dropped_whole = True
            else:
                entry.header_only = True

    sections: list[PlannedSection] = []
    for kind in template.section_order:
        if kind is SectionKind.SKILLS:
            if skills:
                sections.append(PlannedSection(kind=kind, lines=tuple(skills)))
            continue
        kept = [entry for entry in entries.get(kind, []) if not entry.dropped_whole]
        if kept:
            sections.append(
                PlannedSection(
                    kind=kind,
                    entries=tuple(
                        PlannedEntry(
                            kind=kind,
                            source_id=entry.source_id,
                            bullets=tuple(entry.chosen),
                            header_only=entry.header_only,
                        )
                        for entry in kept
                    ),
                )
            )
    return ResumePlan(
        role_type=evidence.role_type,
        template_version=template.version,
        sections=tuple(sections),
        surface_forms=surface,
        estimated_height=round(_height(entries, skills, page, template.section_order), 2),
        usable_height=page.usable_height,
        dropped=tuple(dropped),
    )
