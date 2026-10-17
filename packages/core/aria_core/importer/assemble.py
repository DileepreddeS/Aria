"""From classified lines to entries: a PDF's layout read into an import plan (RESUME_ENGINE §5.4).

A section header opens a section. Inside EXPERIENCE, PROJECTS and EDUCATION an entry
header opens an entry; the plain lines under it (a location, a "Client:" line,
coursework) join its head; bullets attach to the entry above them **in the
reconstructed reading order** — never in content-stream order — with each bullet's
wrapped lines joined back onto it. Dates come only from the date the classifier
found in the header's right-hand band, which reaches the entry parser as a head line
of its own.

The interpretation of an entry — employer and title, client, location, metrics and
skills in a bullet — is the importer's existing work and is reused as it is. What
changes is how lines are grouped, which is where the 2026-10-11 import went wrong.

Line numbers are 1-based positions in the layout's reading order. The text stored on
the upload is the same lines in the same order, so a fact's source_line quotes the
right line either way. A line that belongs nowhere — text after an entry's bullets,
lines before a section's first entry, an unrecognised section — is reported as
unparsed with the reason, never attached by guess.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from aria_core.documents import LayoutDocument
from aria_core.graph.status import AchievementKind, GraphSource
from aria_core.importer.classify import ClassifiedLine, LineClass, classify, leading_segments, leading_text
from aria_core.importer.entries import Entry, parse_education, parse_experience, parse_project
from aria_core.importer.header import parse_header
from aria_core.importer.lines import SourceLine
from aria_core.importer.plan import (
    ImportPlan,
    ImportQuestion,
    ProposedAchievement,
    ProposedEducation,
    ProposedExperience,
    ProposedFact,
    ProposedProject,
    QuestionKind,
    UnparsedLines,
)
from aria_core.importer.resume import _MIN_SUMMARY_CHARACTERS, _achievement_kind, _fact
from aria_core.importer.sections import SectionKind
from aria_core.importer.skills import SkillFindings, parse_skill_list_line
from aria_core.schemas.base import utc_now
from aria_core.taxonomy.model import SkillTaxonomy

__all__ = ["parse_resume_layout"]

#: Where an education header divides school from degree: a dash or a bar with spaces.
_HEAD_SPLIT = re.compile(r"\s+[\u2013\u2014|]\s+")

_ENTRY_SECTIONS = {
    SectionKind.EXPERIENCE,
    SectionKind.LEADERSHIP,
    SectionKind.EDUCATION,
    SectionKind.PROJECTS,
}


def _source(line: ClassifiedLine, text: str | None = None) -> SourceLine:
    body = line.line.text if text is None else text
    return SourceLine(number=line.line.index + 1, text=body, raw=line.line.text, bullet=False)


@dataclass(slots=True)
class _Section:
    heading: ClassifiedLine | None
    kind: SectionKind
    lines: list[ClassifiedLine] = field(default_factory=list)


def _split_sections(classified: Sequence[ClassifiedLine]) -> tuple[list[ClassifiedLine], list[_Section]]:
    header: list[ClassifiedLine] = []
    sections: list[_Section] = []
    for line in classified:
        if line.kind is LineClass.SECTION_HEADER:
            sections.append(_Section(line, line.section or SectionKind.UNKNOWN))
        elif sections:
            sections[-1].lines.append(line)
        else:
            header.append(line)
    return header, sections


def _entries(section: _Section) -> tuple[list[Entry], list[ClassifiedLine]]:
    """The section's entries, and the lines that belong to none of them."""
    entries: list[Entry] = []
    stray: list[ClassifiedLine] = []
    head: list[SourceLine] = []
    bullets: list[SourceLine] = []
    open_entry = False

    def close() -> None:
        if open_entry:
            entries.append(Entry(tuple(head), tuple(bullets)))

    for line in section.lines:
        if line.kind is LineClass.ENTRY_HEADER:
            close()
            open_entry = True
            # The header's phrase: the bold employer and the title after it, which a
            # change of weight splits into touching segments.
            first = leading_text(line.line)
            if line.date_text and first.endswith(line.date_text):
                first = first[: -len(line.date_text)].rstrip()
            if section.kind is SectionKind.EDUCATION:
                # "Cascadia State University \u2014 M.S. Computer Science": the school
                # and the degree are read from lines of their own.
                head = [_source(line, part) for part in _HEAD_SPLIT.split(first) if part.strip()]
            else:
                head = [_source(line, first)]
            if line.date_text:
                head.append(_source(line, line.date_text))
            else:
                # The rest of the header (a project's stack, say) is head text, but a
                # date anywhere but the right-hand band is not read as one.
                rest = " ".join(segment.text for segment in line.line.segments[leading_segments(line.line) :])
                if rest:
                    head.append(_source(line, rest))
            bullets = []
        elif not open_entry:
            stray.append(line)
        elif line.kind is LineClass.BULLET:
            bullets.append(SourceLine(line.line.index + 1, line.line.text, line.line.text, bullet=True))
        elif line.kind is LineClass.CONTINUATION and bullets:
            last = bullets[-1]
            joined = f"{last.text} {line.line.text}"
            bullets[-1] = SourceLine(last.number, joined, joined, bullet=True)
        elif not bullets:
            head.append(_source(line))
        else:
            stray.append(line)
    close()
    return entries, stray


def _unparsed(lines: Sequence[ClassifiedLine], reason: str) -> list[UnparsedLines]:
    return [
        UnparsedLines(first_line=line.line.index + 1, last_line=line.line.index + 1, reason=reason)
        for line in lines
    ]


def parse_resume_layout(
    document: LayoutDocument,
    taxonomy: SkillTaxonomy,
    *,
    today: dt.date | None = None,
    source: GraphSource = GraphSource.RESUME_IMPORT,
) -> ImportPlan:
    """A PDF's layout read into an import plan. The §5.5 gates are applied separately."""
    now = today or utc_now().date()
    classified = classify(document)
    header_lines, sections = _split_sections(classified)

    header = parse_header(tuple(_source(line) for line in header_lines), document.links)
    experiences: list[ProposedExperience] = []
    educations: list[ProposedEducation] = []
    projects: list[ProposedProject] = []
    achievements: list[ProposedAchievement] = []
    loose_facts: list[ProposedFact] = []
    questions: list[ImportQuestion] = []
    unparsed: list[UnparsedLines] = []
    skills = SkillFindings()

    for section in sections:
        content = section.lines
        if section.kind in _ENTRY_SECTIONS:
            entries, stray = _entries(section)
            unparsed.extend(_unparsed(stray, "not part of any entry in its section"))
            for entry in entries:
                if section.kind is SectionKind.EDUCATION:
                    education = parse_education(entry, taxonomy, today=now)
                    if education.school or education.degree:
                        educations.append(education)
                    else:
                        unparsed.extend(
                            UnparsedLines(
                                line.number, line.number, "under EDUCATION, but names no school or degree"
                            )
                            for line in entry.head
                        )
                elif section.kind is SectionKind.PROJECTS:
                    heading = section.heading.line.text if section.heading else ""
                    projects.append(
                        parse_project(entry, taxonomy, today=now, section=section.kind, heading=heading)
                    )
                else:
                    experiences.append(parse_experience(entry, taxonomy, today=now))
            continue
        if section.kind is SectionKind.SKILLS:
            for line in content:
                skills = skills.merge(parse_skill_list_line(_source(line), taxonomy))
            continue
        if section.kind is SectionKind.SUMMARY:
            for line in content:
                if len(line.line.text) >= _MIN_SUMMARY_CHARACTERS:
                    loose_facts.append(_fact(_source(line), taxonomy, today=now, source=source))
            continue
        if section.kind is SectionKind.ACHIEVEMENTS:
            heading_text = section.heading.line.text if section.heading else ""
            kind: AchievementKind = _achievement_kind(heading_text)
            for line in content:
                achievements.append(
                    ProposedAchievement(
                        title=line.line.text[:300], source_line=line.line.index + 1, kind=kind
                    )
                )
            continue
        # A heading the vocabulary does not know: its lines are reported, not filed
        # under the nearest section, and the candidate is asked what it is.
        heading_text = section.heading.line.text if section.heading else ""
        unparsed.extend(_unparsed(content, f"heading {heading_text!r} is not one I know"))
        questions.append(
            ImportQuestion(
                kind=QuestionKind.UNKNOWN_SECTION,
                prompt=f"What is the {heading_text!r} section?",
                source_lines=(section.heading.line.index + 1,) if section.heading else (),
                detail="I did not import its lines. Tell me what they are and I will.",
            )
        )

    if skills.unknown:
        questions.append(
            ImportQuestion(
                kind=QuestionKind.UNKNOWN_SKILLS,
                prompt=f"{len(skills.unknown)} skill(s) are not in the taxonomy yet: "
                + ", ".join(skills.unknown),
                detail=(
                    "They are not in your graph: nothing is recorded for a skill ARIA cannot "
                    "name. Adding them to the taxonomy lets ARIA match them to job requirements."
                ),
            )
        )

    return ImportPlan(
        source=source,
        profile_fields=header.fields,
        experiences=tuple(experiences),
        educations=tuple(educations),
        projects=tuple(projects),
        achievements=tuple(achievements),
        declared_skills=skills.skills,
        loose_facts=tuple(loose_facts),
        questions=tuple(questions),
        unknown_skills=skills.unknown,
        dropped=header.dropped,
        unparsed=tuple(unparsed),
        line_count=len(document.lines),
    )
