"""A posting split into sections by its headings, and each section into items.

A heading is a short line that names a section: "Requirements:", "QUALIFICATIONS", "What
you'll do", "Bonus points:". The vocabulary below maps the common ones; the longest match
wins, so "Preferred qualifications" is preferred, not required. A heading the vocabulary
does not know opens an ``other`` section, and so does the text before the first heading.

Items are bullets, or the sentences of a paragraph. A section that opened with bullets
ends at the first plain paragraph after them: postings put their legal and closing lines
there, and a citizenship sentence below the last qualification is not a qualification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aria_resume.schemas import JDSection

__all__ = ["Item", "heading_section", "split_items"]

#: Heading phrases, normalised to lowercase words. Longest match first.
_VOCABULARY: dict[str, JDSection] = {
    "preferred qualifications": JDSection.PREFERRED,
    "preferred skills": JDSection.PREFERRED,
    "preferred experience": JDSection.PREFERRED,
    "nice to have": JDSection.PREFERRED,
    "nice to haves": JDSection.PREFERRED,
    "bonus points": JDSection.PREFERRED,
    "bonus": JDSection.PREFERRED,
    "pluses": JDSection.PREFERRED,
    "preferred": JDSection.PREFERRED,
    "it would be great if you have": JDSection.PREFERRED,
    "minimum qualifications": JDSection.REQUIRED,
    "basic qualifications": JDSection.REQUIRED,
    "required qualifications": JDSection.REQUIRED,
    "qualifications": JDSection.REQUIRED,
    "requirements": JDSection.REQUIRED,
    "required skills": JDSection.REQUIRED,
    "what you'll need": JDSection.REQUIRED,
    "what you need": JDSection.REQUIRED,
    "what we're looking for": JDSection.REQUIRED,
    "what we are looking for": JDSection.REQUIRED,
    "who you are": JDSection.REQUIRED,
    "you have": JDSection.REQUIRED,
    "about you": JDSection.REQUIRED,
    "must have": JDSection.REQUIRED,
    "must haves": JDSection.REQUIRED,
    "skills": JDSection.REQUIRED,
    "responsibilities": JDSection.RESPONSIBILITIES,
    "key responsibilities": JDSection.RESPONSIBILITIES,
    "what you'll do": JDSection.RESPONSIBILITIES,
    "what you will do": JDSection.RESPONSIBILITIES,
    "in this role you will": JDSection.RESPONSIBILITIES,
    "in this role": JDSection.RESPONSIBILITIES,
    "the role": JDSection.RESPONSIBILITIES,
    "your role": JDSection.RESPONSIBILITIES,
    "duties": JDSection.RESPONSIBILITIES,
    "about us": JDSection.ABOUT,
    "about the company": JDSection.ABOUT,
    "about the team": JDSection.ABOUT,
    "who we are": JDSection.ABOUT,
    "benefits": JDSection.BENEFITS,
    "perks": JDSection.BENEFITS,
    "what we offer": JDSection.BENEFITS,
    "compensation": JDSection.BENEFITS,
}

_BULLET = re.compile(r"^\s*(?:[-*\u2022\u00b7\u25e6\u2023\u2043]|\d{1,2}[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


@dataclass(frozen=True, slots=True)
class Item:
    text: str
    section: JDSection


def _words(line: str) -> str:
    folded = line.strip().strip("#*_ ").rstrip(":").strip().lower().replace("\u2019", "'")
    return " ".join(folded.split())


def heading_section(line: str) -> JDSection | None:
    """The section a heading line opens, or None when the line is not a heading."""
    stripped = line.strip()
    if not stripped or _BULLET.match(stripped) or len(stripped.split()) > 7:
        return None
    words = _words(stripped)
    if words.startswith("about ") and len(words.split()) <= 3:
        return JDSection.ABOUT
    if words in _VOCABULARY:
        return _VOCABULARY[words]
    # An unknown short line ending in a colon is a heading of a section we do not read.
    if stripped.endswith(":") and not stripped[:-1].strip().endswith((".", ",")):
        return JDSection.OTHER
    return None


def _sentences(paragraph: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(paragraph.strip()) if part.strip()]


def split_items(text: str) -> list[Item]:
    """Every item of the posting, in order, with its section."""
    items: list[Item] = []
    section = JDSection.OTHER
    bulleted = False
    #: A prose section has had its paragraph, and a blank line followed it.
    closed = False
    for line in text.split("\n"):
        if not line.strip():
            if items and items[-1].section is section and not bulleted:
                closed = True
            continue
        opened = heading_section(line)
        if opened is not None:
            section, bulleted, closed = opened, False, False
            continue
        bullet = _BULLET.match(line)
        if bullet:
            bulleted = True
            items.append(Item(" ".join(line[bullet.end() :].split()), section))
            continue
        if bulleted or closed:
            # A plain paragraph after a list, or a second paragraph after a blank line,
            # is no longer under the heading: closing and legal lines live there.
            section, bulleted, closed = JDSection.OTHER, False, False
        items.extend(Item(sentence, section) for sentence in _sentences(" ".join(line.split())))
    return items
