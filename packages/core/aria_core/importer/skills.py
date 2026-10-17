"""Finding skills in a resume: in a skills list, and in the prose of a bullet.

Two different jobs with two different risk profiles, which is why they are two
functions rather than one.

**A skills list** is the candidate saying "I know these". Everything on it resolves
through the taxonomy, and whatever does not resolve is reported as an unknown term
rather than dropped — an unrecognised skill is a gap in the taxonomy, and silently
losing it loses a real thing about the person (RESUME_ENGINE §4).

**Prose** is riskier, because a skill tag on a bullet is evidence, and evidence turns
into months: tag "Swift" onto a two-year role and the graph will happily report two
years of Swift. So a short single word is only tagged from prose when the resume wrote
it as a name — capitalised, upper case, or carrying a symbol or digit. "Go to market"
does not make the candidate a Go programmer; "Go" does. Multi-word terms ("machine
learning") are exempt: they are long enough to be unambiguous.

**Splitting a skills line** happens only where the resume separates skills: commas,
semicolons, bars, bullets, wide gaps, "and", "or", and never inside parentheses.
"AWS (EC2, S3, Lambda)" is a skill and the services named under it, each resolved in
its own right. A slash is not a separator: "CI/CD" and "TCP/IP" are one skill each.
A term that does not resolve whole is taken as parts only when known skills cover
every word of it ("GitHub Actions CI/CD", "HTML/CSS", "Java Spring Boot"); otherwise
the whole term is reported as unknown, never a fragment of it.

Implication edges are never followed here. Next.js implies React, and that is useful
for *matching* a requirement later; writing React onto the candidate's graph because
they wrote Next.js would be putting a word in their mouth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aria_core.importer.lines import SourceLine
from aria_core.taxonomy.model import SkillTaxonomy, normalise

__all__ = [
    "ResolvedSkill",
    "SkillFindings",
    "parse_skill_list_line",
    "resolve_terms",
    "split_skill_terms",
    "tag_skills",
]

#: Separators a resume uses between skills on one line. Not "/": "CI/CD" is one skill,
#: and "HTML/CSS" is two only because each half is known (see _resolve).
_SPLIT = re.compile(r"[,;|\u2022\u00b7\u2219]|\s{2,}|\bor\b|\band\b", re.IGNORECASE)

#: A parenthesised group, innermost first. Separators inside one do not split the line.
_GROUP = re.compile(r"\([^()]*\)")
_HELD = re.compile(r"\x00(\d+)\x00")

#: A skills line often starts with its own category: "Languages: Python, Go".
#: The label is dropped — ARIA's categories come from the taxonomy, and a resume's
#: idea of "Tools" is not something to import as data.
_LABEL = re.compile(r"^(?P<label>[A-Za-z][A-Za-z &/+#.-]{0,40}?)\s*[:\u2013-]\s+(?P<rest>.+)$")

#: Longest multi-word skill name worth scanning for. The taxonomy's longest entries
#: ("natural language processing") are three words; four leaves room to grow.
_MAX_TERM_WORDS = 4

#: Below this length, a single-word term must be written as a name to be tagged from
#: prose. Covers the ids that collide with English words: c, r, go, sql, css, etl.
_PROSE_NAME_LENGTH = 5


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    """A skill the taxonomy recognised, with the spelling the resume used."""

    canonical: str
    #: The candidate's own spelling. Kept because ATS keyword matching is literal.
    surface_form: str
    source_line: int | None = None

    def __str__(self) -> str:
        return f"{self.surface_form} ({self.canonical})"


@dataclass(frozen=True, slots=True)
class SkillFindings:
    """What one line or section yielded."""

    skills: tuple[ResolvedSkill, ...] = ()
    #: Terms the taxonomy does not know, in the resume's spelling. Reported so they
    #: can be added, never silently discarded.
    unknown: tuple[str, ...] = ()

    def merge(self, other: SkillFindings) -> SkillFindings:
        """Combine two findings, keeping the first spelling of each canonical id."""
        seen = {skill.canonical for skill in self.skills}
        extra = tuple(skill for skill in other.skills if skill.canonical not in seen)
        unknown_seen = {term.lower() for term in self.unknown}
        extra_unknown = tuple(term for term in other.unknown if term.lower() not in unknown_seen)
        return SkillFindings(self.skills + extra, self.unknown + extra_unknown)


def resolve_terms(
    taxonomy: SkillTaxonomy, terms: list[str], *, source_line: int | None = None
) -> SkillFindings:
    """Resolve explicit terms — a skills list, or a notes file.

    No capitalisation rule here: the candidate wrote these down as skills, so "sql"
    means SQL. The rule exists for prose, where the same word may be ordinary English.
    """
    skills: list[ResolvedSkill] = []
    unknown: list[str] = []
    seen: set[str] = set()

    for term in terms:
        cleaned = term.strip().strip(".,;:()[]\u2022").strip()
        if not cleaned or len(cleaned) > 60:
            continue
        resolved = _resolve(taxonomy, cleaned)
        if not resolved:
            if cleaned.lower() not in {item.lower() for item in unknown}:
                unknown.append(cleaned)
            continue
        for canonical, surface in resolved:
            if canonical in seen:
                continue
            seen.add(canonical)
            skills.append(ResolvedSkill(canonical, surface, source_line))

    return SkillFindings(tuple(skills), tuple(unknown))


def _resolve(taxonomy: SkillTaxonomy, term: str) -> list[tuple[str, str]]:
    """``(canonical, surface)`` for a term: the whole of it, or parts covering every word.

    Parts are tried longest-first, so "GitHub Actions CI/CD" is GitHub Actions and CI/CD
    rather than nothing. If any word is left uncovered the answer is empty, and the
    caller reports the whole term: a fragment such as "CD" is never a finding.
    """
    whole = taxonomy.resolve(term)
    if whole is not None:
        return [(whole, term)]
    # Word spans, so a part keeps the resume's own spelling ("CI/CD", not "CI CD").
    spans = [match.span() for match in re.finditer(r"[^\s/]+", term)]
    found: list[tuple[str, str]] = []
    index = 0
    while index < len(spans):
        for size in range(min(_MAX_TERM_WORDS, len(spans) - index), 0, -1):
            surface = term[spans[index][0] : spans[index + size - 1][1]]
            canonical = taxonomy.resolve(surface)
            if canonical is not None:
                found.append((canonical, surface))
                index += size
                break
        else:
            return []
    return found if len(found) > 1 else []


def split_skill_terms(text: str) -> list[str]:
    """A skills line's terms. A parenthesised group stays inside its term."""
    held: list[str] = []

    def hold(match: re.Match[str]) -> str:
        held.append(match.group(0))
        return f"\x00{len(held) - 1}\x00"

    masked = text
    while _GROUP.search(masked):
        masked = _GROUP.sub(hold, masked)

    def restore(part: str) -> str:
        while _HELD.search(part):
            part = _HELD.sub(lambda match: held[int(match.group(1))], part)
        return part

    return [restore(part) for part in _SPLIT.split(masked) if part and part.strip()]


def _expand(term: str) -> list[str]:
    """ "AWS (EC2, S3, Lambda)" is AWS and the three things named under it."""
    head, bracket, rest = term.partition("(")
    if not bracket:
        return [term]
    if not rest.rstrip().endswith(")"):
        # A group a line break cut off ("AWS (EC2" then "S3)"): the head and the first
        # thing named under it, never one term spanning the bracket.
        return [part for part in (head.strip(), rest.strip()) if part]
    inner = rest.rstrip()[:-1]
    return [head.strip(), *(item for part in split_skill_terms(inner) for item in _expand(part))]


def parse_skill_list_line(line: SourceLine, taxonomy: SkillTaxonomy) -> SkillFindings:
    """Resolve one line of a skills section."""
    text = line.text
    label = _LABEL.match(text)
    if label:
        text = label.group("rest")
    terms = [item for term in split_skill_terms(text) for item in _expand(term)]
    return resolve_terms(taxonomy, terms, source_line=line.number)


def _written_as_a_name(surface: str) -> bool:
    """True when the resume wrote this term as a name rather than as a word.

    Capitalised ("Go"), upper case ("SQL"), or carrying a symbol or digit ("C++",
    "Node.js", "ES6"). A lowercase short word in the middle of a sentence is prose.
    """
    if any(not character.isalpha() for character in surface):
        return True
    return surface[:1].isupper()


def tag_skills(text: str, taxonomy: SkillTaxonomy, *, source_line: int | None = None) -> SkillFindings:
    """Skills named in prose. Used to attach evidence to a fact.

    Scans word n-grams longest-first so "machine learning" resolves as one skill
    rather than as "learning". Unknown terms are not reported: prose is full of words
    that are not skills, and a list of them would be noise rather than a taxonomy gap.
    """
    words = [word for word in re.split(r"[\s,;:()\[\]/|]+", text) if word]
    found: list[ResolvedSkill] = []
    seen: set[str] = set()
    index = 0

    while index < len(words):
        for size in range(min(_MAX_TERM_WORDS, len(words) - index), 0, -1):
            chunk = words[index : index + size]
            surface = " ".join(chunk).strip(".,;:")
            if not normalise(surface):
                continue

            canonical = taxonomy.resolve(surface)
            if canonical is None:
                continue
            if size == 1 and (
                len(surface.strip(".,;:")) <= _PROSE_NAME_LENGTH or taxonomy.get(canonical).common_word
            ):
                # "Spring" the framework, not "the spring semester".
                if not _written_as_a_name(surface.strip(".,;:")):
                    continue
            if canonical not in seen:
                seen.add(canonical)
                found.append(ResolvedSkill(canonical, surface, source_line))
            index += size
            break
        else:
            index += 1

    return SkillFindings(tuple(found))
