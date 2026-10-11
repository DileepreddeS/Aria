"""Importing a plain-text notes file — the skills list carried over from ARIA v1.

A notes file is not a resume and must not be read as one. It has no sections, no
dates, no employers; it is a list of things the candidate knows, sometimes grouped
under their own headings, sometimes one per line, sometimes comma-separated. Running
the resume parser over it would find "experience" in a heading and start inventing
roles out of a word list.

So this reads every line as a list of skills and nothing else. Two consequences worth
being explicit about:

* **It produces no facts.** A skills note says "I know Python". It does not say what
  was built with it, and a declared skill is not evidence — the Evidence Matcher
  treats the two differently on purpose (RESUME_ENGINE §4), and manufacturing a fact
  from a word list would create a claim with nothing behind it.
* **Unresolved terms are reported, not dropped.** A notes file is where the unusual
  skills live, which makes it the best source of taxonomy gaps.
"""

from __future__ import annotations

import re

from aria_core.graph.status import GraphSource
from aria_core.importer.lines import read_lines
from aria_core.importer.plan import ImportPlan, ImportQuestion, QuestionKind, UnparsedLines
from aria_core.importer.skills import SkillFindings, parse_skill_list_line
from aria_core.schemas.untrusted import UntrustedContent
from aria_core.taxonomy.model import SkillTaxonomy

__all__ = ["parse_skills_notes", "parse_skills_notes_text"]

#: A line longer than this is prose, not a list, and is reported rather than chopped
#: into words. "I spent two years doing Python" should not declare two skills.
_MAX_LIST_LINE = 200

#: A line with this many words and no separator is a sentence.
_MAX_WORDS_WITHOUT_SEPARATOR = 6

_SEPARATOR_PRESENT = re.compile(r"[,;|\u2022\u00b7/]")


def parse_skills_notes_text(
    text: str,
    taxonomy: SkillTaxonomy,
    *,
    source: GraphSource = GraphSource.SKILLS_NOTES_IMPORT,
) -> ImportPlan:
    """Read a notes file into declared skills."""
    document = read_lines(text)
    findings = SkillFindings()
    unparsed: list[UnparsedLines] = []

    for line in document.lines:
        if line.blank:
            continue

        separated = bool(_SEPARATOR_PRESENT.search(line.text))
        if len(line.text) > _MAX_LIST_LINE or (
            len(line.words) > _MAX_WORDS_WITHOUT_SEPARATOR and not separated
        ):
            unparsed.append(
                UnparsedLines(
                    first_line=line.number,
                    last_line=line.number,
                    reason="this reads as a sentence rather than a list of skills",
                    sample=line.text[:120],
                )
            )
            continue

        found = parse_skill_list_line(line, taxonomy)
        if not separated and not found.skills:
            # One run of words, nothing recognised: this is one of the candidate's
            # own group headings, or a note to themselves. Reporting it as a missing
            # skill would pad the list of real taxonomy gaps with "Frontend" and
            # "Things I want to learn", and a padded list is one nobody reads.
            unparsed.append(
                UnparsedLines(
                    first_line=line.number,
                    last_line=line.number,
                    reason="this reads as a heading rather than a skill",
                    sample=line.text[:120],
                )
            )
            continue

        findings = findings.merge(found)

    questions: list[ImportQuestion] = []
    if findings.unknown:
        questions.append(
            ImportQuestion(
                kind=QuestionKind.UNKNOWN_SKILLS,
                prompt=f"{len(findings.unknown)} term(s) from your notes are not in the taxonomy: "
                + ", ".join(findings.unknown),
                detail=(
                    "They are kept as you wrote them. Adding them to the taxonomy is what "
                    "lets ARIA match them against a job's requirements."
                ),
            )
        )

    return ImportPlan(
        source=source,
        declared_skills=findings.skills,
        questions=tuple(questions),
        unknown_skills=findings.unknown,
        unparsed=tuple(unparsed),
        line_count=len(document),
    )


def parse_skills_notes(
    content: UntrustedContent,
    taxonomy: SkillTaxonomy,
    *,
    source: GraphSource = GraphSource.SKILLS_NOTES_IMPORT,
) -> ImportPlan:
    """Read an uploaded notes file into declared skills.

    Takes :class:`UntrustedContent` for the same reason the resume parser does: the
    text came from a file somebody uploaded, and the call site should say so.
    """
    return parse_skills_notes_text(content.text, taxonomy, source=source)
