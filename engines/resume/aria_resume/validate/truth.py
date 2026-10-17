"""6b Truth check (RESUME_ENGINE §9), the deterministic part: a line says what its facts say.

A bullet is judged against the facts it cites, and only those. Everything it claims must
be found in them:

- **numbers** exactly as written: "30%" is not "30 percent", "31%", "30.0%" or "~30%",
  and "15+" is not "15". A qualifier in front ("over 30%") is part of the claim. Counts
  written as words ("five engineers", "doubled") are numbers too;
- **months and years**: a year is a number, a month a name the fact must use;
- **technologies** the taxonomy tags, by id: a skill the cited facts name, in any
  spelling of it ("Postgres" for a PostgreSQL fact). Never a sibling (a Redis fact does
  not back "Memcached") and never an implied skill (a PostgreSQL fact does not put
  "SQL" in a bullet), which reads as a claim about what was built;
- **names** of the candidate's other employers, clients, titles, projects, schools and
  awards. The cited facts' own role, project or degree may be named;
- **scope**: leading, owning or mentoring, a company-wide or global reach, doing it
  alone. Each needs a word of the same kind in the cited facts.

The facts' own words therefore always pass, which the repair loop relies on: its last
resort is the fact text itself (§9). The summary cites no single fact, so it is judged
against all of them, and its years are recomputed from the snapshot: full-time months,
truncated (aria_core.graph.months), per skill when the clause names one; it names no
implied skill either. The skills lines may list skills the candidate declared or a fact
names, and an implication of those only where implied_skills.yaml allows it: one step,
and a fact about the technology rather than an inference about the candidate
(DECISIONS 2026-10-17).

A fact's stored tags are read again through their spelling with the taxonomy in hand,
so a tag stored before taxonomy 2026.10.3 filed Memcached under Redis backs Memcached,
and one whose spelling no longer names a skill ("checkout") backs nothing.

Entailment beyond these (a bullet that rearranges true words into a false sentence) is
the NLI model's job (§9), a later step. So are entry headers, which the planner produces.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence

from aria_core.graph.months import months_to_years
from aria_core.importer.skills import resolve_terms, split_skill_terms, tag_skills
from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotFact
from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.document import ResumeDocument, SectionKind
from aria_resume.schemas import Bullet
from aria_resume.validate import Finding
from aria_resume.validate.implied import ImpliedSkills, load_implied_skills
from aria_resume.validate.text import (
    WORD_VALUES,
    month_mentions,
    number_mentions,
    number_words,
    phrase_spans,
)

__all__ = ["check_bullet_text", "check_truth", "check_written"]

#: Claims of scope, by kind. A word of a kind needs some word of the same kind in the
#: cited facts: "led" is backed by "leading", not by "built".
_SCOPE: dict[str, tuple[str, ...]] = {
    "leadership": (
        "led",
        "lead",
        "leads",
        "leading",
        "managed",
        "manage",
        "manages",
        "managing",
        "mentored",
        "mentor",
        "mentors",
        "mentoring",
        "supervised",
        "supervising",
        "directed",
        "headed",
        "owned",
        "ownership",
        "oversaw",
        "overseeing",
    ),
    "breadth": (
        "company-wide",
        "companywide",
        "organization-wide",
        "org-wide",
        "enterprise-wide",
        "firm-wide",
        "global",
        "globally",
        "worldwide",
        "nationwide",
        "entire",
        "across the company",
        "across the organization",
        "all teams",
        "every team",
    ),
    "alone": ("sole", "solely", "single-handedly", "singlehandedly", "alone", "independently", "by myself"),
}

#: A years-of-experience claim: "3 years", "2+ years", "three years", "1.5 yrs".
_YEARS = re.compile(
    r"(?<![\w.])(?P<n>\d+(?:\.\d+)?|" + "|".join(WORD_VALUES) + r")(?P<plus>\+)?"
    r"(?:\s+|-)(?:years?|yrs?)(?![\w])",
    re.IGNORECASE,
)


def _cited(
    fact_ids: Iterable[uuid.UUID], snapshot: CandidateSnapshot
) -> tuple[list[SnapshotFact], list[uuid.UUID]]:
    found: list[SnapshotFact] = []
    missing: list[uuid.UUID] = []
    for fact_id in fact_ids:
        try:
            found.append(snapshot.fact(fact_id))
        except KeyError:
            missing.append(fact_id)
    return found, missing


def _held(facts: Sequence[SnapshotFact], taxonomy: SkillTaxonomy) -> set[str]:
    """The skills ``facts`` name, as taxonomy ids: their tags, read again through the
    candidate's spelling, and what the taxonomy finds in their text."""
    held: set[str] = set()
    for fact in facts:
        for skill in fact.skills:
            canonical = taxonomy.resolve(skill.surface_form)
            if canonical is not None:
                held.add(canonical)
        held |= {skill.canonical for skill in tag_skills(fact.text, taxonomy).skills}
    return held


def _names(snapshot: CandidateSnapshot, facts: Sequence[SnapshotFact]) -> tuple[set[str], set[str]]:
    """Every name the candidate's graph holds, and those the cited facts may use."""
    every: set[str] = set()
    own: set[str] = set()
    anchors = {
        anchor for fact in facts for anchor in (fact.experience_id, fact.project_id, fact.education_id)
    }
    for experience in snapshot.experiences:
        names = {experience.employer, experience.title} | (
            {experience.client} if experience.client else set()
        )
        every |= names
        if experience.experience_id in anchors:
            own |= names
    for project in snapshot.projects:
        every.add(project.name)
        if project.project_id in anchors:
            own.add(project.name)
    for degree in snapshot.education:
        every |= {degree.school, degree.degree}
        if degree.education_id in anchors:
            own |= {degree.school, degree.degree}
    every |= {achievement.title for achievement in snapshot.achievements}
    return every, own


def _backed(
    text: str,
    facts: Sequence[SnapshotFact],
    snapshot: CandidateSnapshot,
    taxonomy: SkillTaxonomy,
    *,
    label: str,
    skip: Sequence[tuple[int, int]] = (),
) -> list[Finding]:
    """Everything ``text`` claims that ``facts`` do not."""
    source = " \n ".join(fact.text for fact in facts)
    findings: list[Finding] = []
    # A claim checked elsewhere (a summary's years) is blanked, so its number is not
    # judged a second time against the facts' text.
    for start, end in skip:
        text = text[:start] + " " * (end - start) + text[end:]

    bare = {mention.bare for mention in number_mentions(source)}
    qualified = {mention.qualified for mention in number_mentions(source) if mention.qualified}
    for mention in number_mentions(text):
        claim = mention.qualified or mention.bare
        if (mention.qualified and mention.qualified not in qualified) or mention.bare not in bare:
            findings.append(Finding("unbacked_number", f"{label}: {claim!r} is in no cited fact"))
    source_words = set(number_words(source))
    for word in number_words(text):
        if word not in source_words:
            findings.append(Finding("unbacked_number", f"{label}: {word!r} is in no cited fact"))

    source_months = set(month_mentions(source))
    if any(month not in source_months for month in month_mentions(text)):
        findings.append(Finding("unbacked_date", f"{label}: names a month no cited fact names"))

    held = _held(facts, taxonomy)
    for skill in tag_skills(text, taxonomy).skills:
        if skill.canonical not in held:
            findings.append(
                Finding("unbacked_technology", f"{label}: {skill.surface_form!r} is in no cited fact")
            )

    every, own = _names(snapshot, facts)
    own |= {name for name in every if phrase_spans(source, name)}
    covered = [span for name in own for span in phrase_spans(text, name)]
    for name in sorted(every - own):
        for start, end in phrase_spans(text, name):
            if not any(outer_start <= start and end <= outer_end for outer_start, outer_end in covered):
                findings.append(
                    Finding("unbacked_name", f"{label}: names {name!r}, which no cited fact does")
                )
                break

    for kind, terms in _SCOPE.items():
        used = [term for term in terms if phrase_spans(text, term)]
        if used and not any(phrase_spans(source, term) for term in terms):
            findings.append(
                Finding("unbacked_scope", f"{label}: {used[0]!r} claims {kind} no cited fact states")
            )
    return findings


def check_bullet_text(
    text: str, fact_ids: Sequence[uuid.UUID], snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
) -> list[Finding]:
    """One bullet against the facts it cites."""
    label = repr(text)
    if not fact_ids:
        return [Finding("no_citation", f"{label} cites no fact")]
    facts, missing = _cited(fact_ids, snapshot)
    if missing:
        return [
            Finding("unknown_fact", f"{label} cites {fact_id}, which the snapshot does not hold")
            for fact_id in missing
        ]
    return _backed(text, facts, snapshot, taxonomy, label=label)


def _years_value(raw: str) -> float:
    return WORD_VALUES[raw.lower()] if raw.lower() in WORD_VALUES else float(raw)


def _summary(line: str, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy) -> list[Finding]:
    label = f"summary {line!r}"
    findings: list[Finding] = []
    skip: list[tuple[int, int]] = []
    months = {item.canonical: item.full_time_months for item in snapshot.skill_years}
    for match in _YEARS.finditer(line):
        skip.append((match.start(), match.end()))
        claimed = _years_value(match.group("n"))
        # The years are of whatever skill the rest of the clause names; of the career if none.
        clause = re.split(r"[.,;:]", line[match.end() :], maxsplit=1)[0]
        skills = tag_skills(clause, taxonomy).skills
        subjects = [(skill.surface_form, months.get(skill.canonical, 0)) for skill in skills]
        if not subjects:
            subjects = [("experience", snapshot.full_time_months)]
        for subject, held in subjects:
            if claimed > months_to_years(held):
                findings.append(
                    Finding(
                        "summary_years",
                        f"{label}: claims {match.group(0)!r} of {subject}; the graph holds "
                        f"{months_to_years(held)} full-time years",
                    )
                )
    return findings + _backed(line, snapshot.facts, snapshot, taxonomy, label=label, skip=skip)


def _skills_line(
    line: str, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, implied: ImpliedSkills
) -> list[Finding]:
    listed = line.split(":", 1)[1] if ":" in line else line
    declared = {taxonomy.resolve(skill.surface_form) for skill in snapshot.declared_skills}
    held = {canonical for canonical in declared if canonical} | _held(snapshot.facts, taxonomy)
    verified = implied.listable(held)
    findings = resolve_terms(taxonomy, split_skill_terms(listed))
    problems = [
        Finding(
            "unverified_skill",
            f"the skills line lists {skill.surface_form!r}, which the candidate has not approved",
        )
        for skill in findings.skills
        if skill.canonical not in verified
    ]
    problems += [
        Finding("unverified_skill", f"the skills line lists {term!r}, which the taxonomy does not know")
        for term in findings.unknown
    ]
    return problems


def check_truth(
    document: ResumeDocument,
    snapshot: CandidateSnapshot,
    taxonomy: SkillTaxonomy,
    *,
    implied: ImpliedSkills | None = None,
) -> list[Finding]:
    """Every line of the page that makes a claim, against the snapshot."""
    implied = implied or load_implied_skills(taxonomy)
    findings: list[Finding] = []
    for section in document.sections:
        for line in section.lines:
            if section.kind is SectionKind.SUMMARY:
                findings += _summary(line, snapshot, taxonomy)
            elif section.kind is SectionKind.SKILLS:
                findings += _skills_line(line, snapshot, taxonomy, implied)
        for entry in section.entries:
            for bullet in entry.bullets:
                findings += check_bullet_text(bullet.text, bullet.fact_ids, snapshot, taxonomy)
    return findings


def check_written(
    bullet: Bullet,
    snapshot: CandidateSnapshot,
    taxonomy: SkillTaxonomy,
    *,
    provided: Sequence[uuid.UUID],
) -> list[Finding]:
    """A written bullet's declarations (§8 5b), and then its text."""
    label = repr(bullet.text)
    findings = [
        Finding("not_provided", f"{label} cites {fact_id}, which its slot was not given")
        for fact_id in bullet.fact_ids
        if fact_id not in provided
    ]
    facts, _ = _cited(bullet.fact_ids, snapshot)
    metrics = {metric.exact_value for fact in facts for metric in fact.metrics}
    for metric in bullet.metrics_used:
        if metric not in metrics:
            findings.append(
                Finding("metric_not_in_facts", f"{label} declares {metric!r}, which no cited fact holds")
            )
        if not re.search(rf"(?<![\w.]){re.escape(metric)}(?!\w)", bullet.text):
            findings.append(
                Finding("metric_not_in_text", f"{label} declares {metric!r} but does not print it")
            )
    for mention in number_mentions(bullet.text):
        if mention.bare in metrics and mention.bare not in bullet.metrics_used:
            findings.append(
                Finding("metric_not_declared", f"{label} prints {mention.bare!r} without declaring it")
            )
    held = _held(facts, taxonomy)
    for term in bullet.tech_used:
        if taxonomy.resolve(term) not in held:
            findings.append(
                Finding("tech_not_allowed", f"{label} declares {term!r}, which is not on its allowed list")
            )
    return findings + check_bullet_text(bullet.text, bullet.fact_ids, snapshot, taxonomy)
