"""What an import proposes, before anything is written down.

An :class:`ImportPlan` is the whole output of reading a document: no database, no ids,
nothing approved. That separation is the point. The parser can be tested on strings,
the plan can be shown to the candidate in full, and the only code that writes rows
takes a plan as input — so "what would importing this resume do?" is answerable
without doing it.

Everything in a plan is a **proposal**. Each one becomes a draft row that the
candidate approves, edits or rejects, and nothing here can produce an approved fact:
:func:`aria_core.graph.facts.create_draft_fact` has no parameter for it.

A plan also carries what the importer could *not* do — questions, unparsed lines,
unknown skills, details dropped on purpose. A resume importer that reports only its
successes is lying by omission about a document the candidate is about to approve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from aria_core.graph.months import MonthSpan
from aria_core.graph.status import (
    AchievementKind,
    ExperienceKind,
    GraphSource,
    ProjectKind,
    Theme,
)
from aria_core.importer.dates import DateRange
from aria_core.importer.header import DroppedDetail, ProposedProfileField
from aria_core.importer.metrics import Metric
from aria_core.importer.skills import ResolvedSkill

__all__ = [
    "Anchored",
    "ImportPlan",
    "ImportQuestion",
    "ProposedAchievement",
    "ProposedEducation",
    "ProposedExperience",
    "ProposedFact",
    "ProposedProject",
    "QuestionKind",
    "UnparsedLines",
]


class QuestionKind(StrEnum):
    """Why the importer is asking instead of deciding.

    Each of these is a case where a plausible default exists and would be wrong often
    enough to matter. CLAUDE.md: low confidence creates a question, never a default.
    """

    MISSING_DATES = "missing_dates"
    """The resume gave a season, a year, or nothing. A month cannot be inferred."""
    PAID_OR_UNPAID = "paid_or_unpaid"
    """A research or teaching assistantship: part-time employment, or project time?
    The answer changes what "years of experience" means and only the candidate has
    it (``docs/DECISIONS.md``)."""
    EMPLOYER_OR_TITLE = "employer_or_title"
    """Two halves of a line and nothing says which is the job and which is the firm."""
    COLUMNS_MERGED = "columns_merged"
    """The page had two columns and the extractor flattened each row into one line.
    Nothing from it can be imported, and re-exporting in a single column fixes it."""
    UNKNOWN_SECTION = "unknown_section"
    """A heading the importer does not know. Its lines were not imported."""
    UNKNOWN_SKILLS = "unknown_skills"
    """Terms the taxonomy has not heard of. A gap to fill, not a term to drop."""
    POSSIBLE_DUPLICATE = "possible_duplicate"
    """This looks like something already in the graph. Merge, or keep both?"""

    # Raised by the onboarding interviews rather than by an import, and listed here so
    # one enum covers everything that can appear on the question queue. The templates
    # live in :mod:`aria_core.interview.templates`.
    MISSING_METRIC = "missing_metric"
    """A bullet with no number in it. The answer becomes a new draft fact."""
    MISSING_CONTEXT = "missing_context"
    """A role with no achievements under it, so nothing can be tailored from it."""
    UNEVIDENCED_SKILL = "unevidenced_skill"
    """A listed skill no fact mentions: a keyword until something backs it."""
    MISSING_PROFILE_FIELD = "missing_profile_field"
    """A contact detail an application cannot be submitted without."""
    MOTIVATION = "motivation"
    """One of the six things the Answer Engine needs and must never invent."""


@dataclass(frozen=True, slots=True)
class ImportQuestion:
    """One thing to ask the candidate, with the lines that prompted it."""

    kind: QuestionKind
    prompt: str
    source_lines: tuple[int, ...] = ()
    #: Context for the person answering: what was found, and what the options are.
    detail: str = ""

    def __str__(self) -> str:
        where = f" (line{'s' if len(self.source_lines) != 1 else ''} " + (
            ", ".join(str(number) for number in self.source_lines) + ")"
        )
        return f"{self.prompt}{where if self.source_lines else ''}"


@dataclass(frozen=True, slots=True)
class ProposedFact:
    """One achievement, as a draft fact with its edges."""

    text: str
    source_line: int
    skills: tuple[ResolvedSkill, ...] = ()
    metrics: tuple[Metric, ...] = ()
    themes: tuple[Theme, ...] = ()
    source: GraphSource = GraphSource.RESUME_IMPORT

    @property
    def skill_pairs(self) -> tuple[tuple[str, str], ...]:
        """``(canonical, surface_form)`` pairs, as ``create_draft_fact`` wants them."""
        return tuple((skill.canonical, skill.surface_form) for skill in self.skills)

    @property
    def metric_triples(self) -> tuple[tuple[str, str | None, str | None], ...]:
        return tuple((metric.exact_value, metric.measures, metric.baseline) for metric in self.metrics)


@dataclass(frozen=True, slots=True)
class ProposedExperience:
    """A role, with the bullets found under it.

    ``span`` is ``None`` when the resume did not state the months. Such a role is
    still proposed — it is real and the candidate should see it — but it cannot be
    written as a row, so it travels with a :attr:`QuestionKind.MISSING_DATES`
    question and :func:`aria_core.importer.apply` holds it back.
    """

    employer: str
    title: str
    source_line: int
    kind: ExperienceKind = ExperienceKind.PROFESSIONAL
    client: str | None = None
    location_city: str | None = None
    location_region: str | None = None
    span: MonthSpan | None = None
    #: What the resume wrote, kept even when it could not be parsed into months.
    date_text: str = ""
    dates: DateRange | None = None
    facts: tuple[ProposedFact, ...] = ()
    questions: tuple[ImportQuestion, ...] = ()

    @property
    def datable(self) -> bool:
        return self.span is not None

    def label(self) -> str:
        parts = [self.title, self.employer]
        if self.client:
            parts.append(f"client: {self.client}")
        return " \u2014 ".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class ProposedEducation:
    school: str
    degree: str
    source_line: int
    field_of_study: str | None = None
    gpa: str | None = None
    location_city: str | None = None
    location_region: str | None = None
    span: MonthSpan | None = None
    date_text: str = ""
    coursework: str | None = None
    facts: tuple[ProposedFact, ...] = ()
    questions: tuple[ImportQuestion, ...] = ()

    def label(self) -> str:
        return " \u2014 ".join(part for part in (self.degree, self.school) if part)


@dataclass(frozen=True, slots=True)
class ProposedProject:
    name: str
    source_line: int
    kind: ProjectKind = ProjectKind.PERSONAL
    url: str | None = None
    repository_url: str | None = None
    span: MonthSpan | None = None
    date_text: str = ""
    facts: tuple[ProposedFact, ...] = ()
    questions: tuple[ImportQuestion, ...] = ()

    def label(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class ProposedAchievement:
    title: str
    source_line: int
    kind: AchievementKind = AchievementKind.AWARD
    issuer: str | None = None
    url: str | None = None
    date_text: str = ""


@dataclass(frozen=True, slots=True)
class UnparsedLines:
    """A run of lines the importer did not turn into anything.

    Reported rather than discarded. Everything in a resume is there because the
    candidate put it there, and an importer that drops a block without saying so is
    the reason people do not trust these tools.
    """

    first_line: int
    last_line: int
    reason: str
    sample: str = ""

    @property
    def count(self) -> int:
        return self.last_line - self.first_line + 1


class Anchored(Protocol):
    """What the three anchor kinds have in common: bullets, and open questions.

    Roles, degrees and projects are different enough to be separate types and alike
    in exactly this much, which is all the plan's reporting needs.
    """

    @property
    def facts(self) -> tuple[ProposedFact, ...]: ...

    @property
    def questions(self) -> tuple[ImportQuestion, ...]: ...

    def label(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """Everything one document would add to the graph, and everything it would not."""

    source: GraphSource = GraphSource.RESUME_IMPORT
    profile_fields: tuple[ProposedProfileField, ...] = ()
    experiences: tuple[ProposedExperience, ...] = ()
    educations: tuple[ProposedEducation, ...] = ()
    projects: tuple[ProposedProject, ...] = ()
    achievements: tuple[ProposedAchievement, ...] = ()
    declared_skills: tuple[ResolvedSkill, ...] = ()
    #: Facts with nothing to attach to — a summary section, or a leadership entry
    #: that is a bullet rather than a role.
    loose_facts: tuple[ProposedFact, ...] = ()
    questions: tuple[ImportQuestion, ...] = ()
    unknown_skills: tuple[str, ...] = ()
    dropped: tuple[DroppedDetail, ...] = ()
    unparsed: tuple[UnparsedLines, ...] = ()
    #: Lines of the document, for the approval view to quote. Not persisted here —
    #: the upload row already holds the extracted text.
    line_count: int = 0
    _notes: tuple[str, ...] = field(default=(), repr=False)

    # ------------------------------------------------------------------ reporting
    @property
    def anchors(self) -> tuple[Anchored, ...]:
        """Every role, degree and project, in that order."""
        return (*self.experiences, *self.educations, *self.projects)

    @property
    def all_facts(self) -> tuple[ProposedFact, ...]:
        """Every proposed fact, in document order."""
        facts = list(self.loose_facts)
        for anchor in self.anchors:
            facts.extend(anchor.facts)
        return tuple(sorted(facts, key=lambda fact: fact.source_line))

    @property
    def datable_experiences(self) -> tuple[ProposedExperience, ...]:
        return tuple(experience for experience in self.experiences if experience.datable)

    @property
    def all_questions(self) -> tuple[ImportQuestion, ...]:
        """Plan-level questions and the ones attached to individual entries."""
        questions = list(self.questions)
        for anchor in self.anchors:
            questions.extend(anchor.questions)
        return tuple(questions)

    def counts(self) -> dict[str, int]:
        """Shape of the plan, for an audit payload. Counts only — no content.

        Audit payloads are S0: an event may record that eleven facts were proposed
        and must never record what they say (SECURITY.md §3).
        """
        return {
            "profile_fields": len(self.profile_fields),
            "experiences": len(self.experiences),
            "experiences_without_dates": len(self.experiences) - len(self.datable_experiences),
            "educations": len(self.educations),
            "projects": len(self.projects),
            "achievements": len(self.achievements),
            "declared_skills": len(self.declared_skills),
            "facts": len(self.all_facts),
            "questions": len(self.all_questions),
            "unknown_skills": len(self.unknown_skills),
            "dropped": len(self.dropped),
            "unparsed_blocks": len(self.unparsed),
            "unparsed_lines": sum(block.count for block in self.unparsed),
        }

    def summary_lines(self) -> list[str]:
        """A readable account of the plan, for the CLI and the approval view."""
        lines = [
            f"{len(self.profile_fields)} profile field(s), "
            f"{len(self.experiences)} role(s), "
            f"{len(self.educations)} education entr(y/ies), "
            f"{len(self.projects)} project(s), "
            f"{len(self.all_facts)} fact(s) \u2014 all as drafts for you to approve",
        ]
        for experience in self.experiences:
            when = experience.date_text or "dates unknown"
            lines.append(
                f"  line {experience.source_line}: {experience.label()} "
                f"[{experience.kind.value}] {when} \u2014 {len(experience.facts)} bullet(s)"
            )
        for education in self.educations:
            lines.append(f"  line {education.source_line}: {education.label()} {education.date_text}")
        for project in self.projects:
            lines.append(f"  line {project.source_line}: {project.name} [{project.kind.value}]")
        for achievement in self.achievements:
            lines.append(f"  line {achievement.source_line}: {achievement.title} [{achievement.kind.value}]")
        if self.declared_skills:
            lines.append(f"  {len(self.declared_skills)} skill(s) from the skills list")
        if self.unknown_skills:
            lines.append(f"  not in the taxonomy yet: {', '.join(self.unknown_skills)}")
        for detail in self.dropped:
            lines.append(f"  dropped from line {detail.source_line}: {detail.what} \u2014 {detail.reason}")
        for block in self.unparsed:
            where = (
                f"line {block.first_line}"
                if block.count == 1
                else f"lines {block.first_line}-{block.last_line}"
            )
            lines.append(f"  not imported ({where}): {block.reason}")
        for question in self.all_questions:
            lines.append(f"  ? {question}")
        return lines
