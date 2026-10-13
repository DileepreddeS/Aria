"""Finding a resume's sections, and admitting when a heading is unrecognised.

Headings are matched against a vocabulary rather than guessed at from formatting,
because formatting is the first thing PDF extraction loses: bold, size and colour are
all gone by the time the importer sees the text, and indentation is unreliable.

An unrecognised heading becomes :attr:`SectionKind.UNKNOWN` and its lines are reported
as unparsed rather than dropped or guessed into the nearest section. A resume with a
"RELEVANT COURSEWORK" heading should produce "I did not know what to do with lines
34-39" and not silently file them as work experience — the importer's output is a set
of claims about a person, and quietly misfiling one is worse than not importing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aria_core.importer.lines import ResumeText, SourceLine

__all__ = ["Section", "SectionKind", "merged_headings", "split_sections"]


class SectionKind(StrEnum):
    """The parts of a resume the importer knows how to read."""

    HEADER = "header"
    """Everything before the first heading: name, contact details, links."""
    SUMMARY = "summary"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    SKILLS = "skills"
    PROJECTS = "projects"
    ACHIEVEMENTS = "achievements"
    """Awards, publications, certifications, patents, talks."""
    LEADERSHIP = "leadership"
    """Activities and volunteering. Separate because the time counts differently."""
    UNKNOWN = "unknown"
    """A heading-shaped line the vocabulary does not cover. Reported, never merged."""


#: Heading text to section, after folding to lowercase and dropping punctuation.
#: Phrases rather than keywords: "work authorization" must not match "work".
_HEADINGS: dict[str, SectionKind] = {
    "summary": SectionKind.SUMMARY,
    "professional summary": SectionKind.SUMMARY,
    "profile": SectionKind.SUMMARY,
    "objective": SectionKind.SUMMARY,
    "career objective": SectionKind.SUMMARY,
    "about": SectionKind.SUMMARY,
    "about me": SectionKind.SUMMARY,
    "experience": SectionKind.EXPERIENCE,
    "work experience": SectionKind.EXPERIENCE,
    "professional experience": SectionKind.EXPERIENCE,
    "relevant experience": SectionKind.EXPERIENCE,
    "employment": SectionKind.EXPERIENCE,
    "employment history": SectionKind.EXPERIENCE,
    "work history": SectionKind.EXPERIENCE,
    "industry experience": SectionKind.EXPERIENCE,
    "internships": SectionKind.EXPERIENCE,
    "internship experience": SectionKind.EXPERIENCE,
    "education": SectionKind.EDUCATION,
    "academics": SectionKind.EDUCATION,
    "academic background": SectionKind.EDUCATION,
    "skills": SectionKind.SKILLS,
    "technical skills": SectionKind.SKILLS,
    "core skills": SectionKind.SKILLS,
    "skills and tools": SectionKind.SKILLS,
    "technologies": SectionKind.SKILLS,
    "technical proficiencies": SectionKind.SKILLS,
    "tools": SectionKind.SKILLS,
    "tech stack": SectionKind.SKILLS,
    "projects": SectionKind.PROJECTS,
    "personal projects": SectionKind.PROJECTS,
    "academic projects": SectionKind.PROJECTS,
    "selected projects": SectionKind.PROJECTS,
    "side projects": SectionKind.PROJECTS,
    "open source": SectionKind.PROJECTS,
    "research": SectionKind.PROJECTS,
    "awards": SectionKind.ACHIEVEMENTS,
    "honors": SectionKind.ACHIEVEMENTS,
    "honours": SectionKind.ACHIEVEMENTS,
    "awards and honors": SectionKind.ACHIEVEMENTS,
    "achievements": SectionKind.ACHIEVEMENTS,
    "certifications": SectionKind.ACHIEVEMENTS,
    "certificates": SectionKind.ACHIEVEMENTS,
    "publications": SectionKind.ACHIEVEMENTS,
    "patents": SectionKind.ACHIEVEMENTS,
    "talks": SectionKind.ACHIEVEMENTS,
    "leadership": SectionKind.LEADERSHIP,
    "activities": SectionKind.LEADERSHIP,
    "leadership and activities": SectionKind.LEADERSHIP,
    "extracurricular activities": SectionKind.LEADERSHIP,
    "volunteering": SectionKind.LEADERSHIP,
    "volunteer experience": SectionKind.LEADERSHIP,
}

#: Characters a heading may carry and still be the same heading.
_HEADING_NOISE = ":;.,|_=~"

#: A heading is short. Anything longer is a sentence that happens to start with a
#: section word, and reading it as a heading would bury the rest of the line.
_MAX_HEADING_WORDS = 4


#: Longest known heading, in words. Nothing longer needs to be considered when
#: looking for two headings printed side by side.
_MAX_HEADING_PHRASE = max(len(key.split()) for key in _HEADINGS)


def merged_headings(line: SourceLine) -> tuple[str, ...]:
    """Known section headings printed side by side on one line.

    **The fallback, not the detector.** Columns are found properly in
    :mod:`aria_document_parser.reading_order`, from the gutter in the page's geometry,
    and a PDF arrives here already in reading order. This text check exists for the
    case with no geometry to read: a ``.txt`` upload or pasted text whose columns were
    flattened before ARIA ever saw it.

    It catches only the obvious shape -- two known headings on one line -- and misses
    the common one, a sidebar of skills beside a column of bullets where every merged
    row reads plausibly. That is exactly why the real detection is geometric.
    """
    words = line.text.split()
    if len(words) < 2:
        return ()

    found: list[str] = []
    index = 0
    while index < len(words):
        for size in range(min(_MAX_HEADING_PHRASE, len(words) - index), 0, -1):
            phrase = " ".join(words[index : index + size]).strip(_HEADING_NOISE)
            key = " ".join(phrase.lower().replace("&", "and").split())
            if key in _HEADINGS:
                found.append(phrase)
                index += size
                break
        else:
            index += 1

    return tuple(found) if len(found) > 1 else ()


def section_for_heading(text: str) -> SectionKind | None:
    """The section a heading's words name, or None when the vocabulary does not know them.

    For the geometric classifier, where layout decides that a line is a heading and
    this only says which one: the vocabulary raises confidence, never decides alone.
    """
    folded = text.strip().strip(_HEADING_NOISE).strip().lower().replace("&", "and")
    return _HEADINGS.get(" ".join(folded.strip(_HEADING_NOISE).split()))


def _heading_key(line: SourceLine) -> str:
    """The lookup key for a line, or "" if it cannot be a heading at all."""
    text = line.text.strip().strip(_HEADING_NOISE).strip()
    if not text or line.bullet:
        return ""
    if len(text.split()) > _MAX_HEADING_WORDS:
        return ""

    folded = text.lower().replace("&", "and")
    return " ".join(folded.strip(_HEADING_NOISE).split())


def _looks_like_a_heading(line: SourceLine, key: str) -> bool:
    """True for a heading-shaped line the vocabulary does not know.

    Deliberately narrow: all-caps, or title case ending in a colon. Those are the two
    things that survive extraction. A line that is merely short is not a heading —
    an employer name on its own line is short too.
    """
    if not key or any(character.isdigit() for character in line.text):
        return False
    letters = [character for character in line.text if character.isalpha()]
    if not letters:
        return False
    if all(character.isupper() for character in letters) and len(letters) > 2:
        return True
    return line.text.rstrip().endswith(":") and line.text[0].isupper()


@dataclass(frozen=True, slots=True)
class Section:
    """One run of lines under one heading."""

    kind: SectionKind
    #: The heading line, or ``None`` for the header block, which has no heading.
    heading: SourceLine | None
    lines: tuple[SourceLine, ...]

    @property
    def content(self) -> tuple[SourceLine, ...]:
        return tuple(line for line in self.lines if not line.blank)

    @property
    def heading_text(self) -> str:
        return self.heading.text if self.heading else "(header)"


def split_sections(document: ResumeText) -> tuple[Section, ...]:
    """Split a resume into sections, in document order.

    The first section is always the header, even when empty, so callers do not have
    to special-case a resume that opens with a heading.
    """
    sections: list[Section] = []
    current_kind = SectionKind.HEADER
    current_heading: SourceLine | None = None
    current: list[SourceLine] = []

    def flush() -> None:
        sections.append(Section(kind=current_kind, heading=current_heading, lines=tuple(current)))

    for line in document.lines:
        key = _heading_key(line)
        kind = _HEADINGS.get(key)
        if kind is None and _looks_like_a_heading(line, key):
            kind = SectionKind.UNKNOWN

        if kind is None:
            current.append(line)
            continue

        flush()
        current_kind, current_heading, current = kind, line, []

    flush()
    return tuple(sections)
