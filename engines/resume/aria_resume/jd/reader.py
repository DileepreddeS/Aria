"""Stage 1 by rules: a posting's text to a :class:`ParsedJD`, with no model involved.

What a model must read (title, company, requirements written as prose outside a known
heading) is left unset or empty here, never guessed; the model step fills it, through the
quarantined-model pattern (SECURITY.md §5).
"""

from __future__ import annotations

from aria_core.importer.skills import tag_skills
from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.jd import extract
from aria_resume.jd.clean import MAX_CHARACTERS, clean
from aria_resume.jd.sections import split_items
from aria_resume.jd.suspicious import screen
from aria_resume.schemas import JDSection, ParsedJD, RawRequirement

__all__ = ["read_jd"]

#: The responsibilities a ParsedJD keeps (RESUME_ENGINE §3: "max 12").
MAX_RESPONSIBILITIES = 12


def read_jd(raw: str, taxonomy: SkillTaxonomy) -> ParsedJD:
    cleaned = clean(raw)
    items = split_items(cleaned.text)

    signals = [f"{kind}: {item.text}" for kind, item in screen(items)]
    flagged = {id(item) for _, item in screen(items)}
    if cleaned.invisible:
        signals.append(
            f"hidden_text: the posting contained {cleaned.invisible} invisible characters, removed"
        )
    if cleaned.truncated:
        signals.append(f"oversized: the posting ran past {MAX_CHARACTERS} characters and was cut")
    # A flagged line is quoted above and is nothing else: not a requirement, not a duty.
    read = [item for item in items if id(item) not in flagged]

    keywords: list[str] = []
    for item in read:
        if item.section in (JDSection.ABOUT, JDSection.BENEFITS):
            continue
        for skill in tag_skills(item.text, taxonomy).skills:
            if skill.surface_form not in keywords:
                keywords.append(skill.surface_form)

    def raw_requirements(section: JDSection) -> tuple[RawRequirement, ...]:
        return tuple(
            RawRequirement(text=item.text, section=section) for item in read if item.section is section
        )

    return ParsedJD(
        seniority=extract.seniority(cleaned.text),
        min_years=extract.general_years(read),
        education=extract.degree_lines(read),
        location=extract.location(cleaned.text),
        work_mode=extract.work_mode(cleaned.text),
        responsibilities=tuple(item.text for item in read if item.section is JDSection.RESPONSIBILITIES)[
            :MAX_RESPONSIBILITIES
        ],
        required=raw_requirements(JDSection.REQUIRED),
        preferred=raw_requirements(JDSection.PREFERRED),
        keywords=tuple(keywords),
        sponsorship_language=extract.sponsorship_sentences(read),
        clearance_or_citizenship=extract.citizenship_sentences(read),
        suspicious_instructions=tuple(signals),
    )
