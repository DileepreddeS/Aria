"""The finished content of one resume page, as the renderer receives it (Stage 7).

Everything here has already been decided: which facts, in what words, in what order. The
renderer only lays it out, so it can be tested on its own with a document built by hand,
and every later stage is measured through it (the plan approved this order: renderer
second). Each bullet still carries the facts it came from, so the PDF's text can be traced
back to approved facts after rendering.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from aria_core.schemas.base import AriaMessage
from aria_core.sensitivity import Sensitivity

__all__ = [
    "ContactItem",
    "DocumentBullet",
    "DocumentEntry",
    "DocumentSection",
    "ResumeDocument",
    "SectionKind",
]

#: What separates contact items on the line. Plain text, so an extractor reads the line whole.
CONTACT_SEPARATOR = " | "


class SectionKind(StrEnum):
    SUMMARY = "summary"
    EXPERIENCE = "experience"
    PROJECTS = "projects"
    EDUCATION = "education"
    SKILLS = "skills"
    ACHIEVEMENTS = "achievements"


#: The standard headings (RESUME_ENGINE §9 6a), in capitals as they are printed.
HEADINGS: dict[SectionKind, str] = {
    SectionKind.SUMMARY: "SUMMARY",
    SectionKind.EXPERIENCE: "EXPERIENCE",
    SectionKind.PROJECTS: "PROJECTS",
    SectionKind.EDUCATION: "EDUCATION",
    SectionKind.SKILLS: "SKILLS",
    SectionKind.ACHIEVEMENTS: "ACHIEVEMENTS",
}


class ContactItem(AriaMessage):
    text: Annotated[str, Sensitivity.S1] = Field(min_length=1, max_length=200)
    #: Where it links, added to the PDF as an annotation over the text, never as markup.
    url: Annotated[str | None, Sensitivity.S1] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _the_separator_stays_a_separator(self) -> Self:
        if CONTACT_SEPARATOR.strip() in self.text:
            raise ValueError("a contact item cannot contain the separator")
        if self.url is not None and not self.url.startswith(("https://", "mailto:", "tel:")):
            raise ValueError("a contact link is https, mailto or tel")
        return self


class DocumentBullet(AriaMessage):
    text: Annotated[str, Sensitivity.S1] = Field(min_length=1, max_length=400)
    #: The approved facts this bullet says. Empty only for a line that cites none.
    fact_ids: Annotated[tuple[uuid.UUID, ...], Sensitivity.S0] = ()


class DocumentEntry(AriaMessage):
    """One role, project or degree: a header line, an optional second line, its bullets."""

    #: The bold part: employer, project or school.
    title: Annotated[str, Sensitivity.S1] = Field(min_length=1, max_length=200)
    #: After the dash: job title, project tagline, degree.
    detail: Annotated[str | None, Sensitivity.S1] = Field(default=None, max_length=200)
    #: In the right-hand band, "Feb 2024 - Jul 2025". Empty for an undated project.
    dates: Annotated[str, Sensitivity.S1] = Field(default="", max_length=60)
    #: A second plain line: location, client, coursework, a project's stack.
    subline: Annotated[str | None, Sensitivity.S1] = Field(default=None, max_length=300)
    bullets: tuple[DocumentBullet, ...] = ()
    #: The approved role, project or degree this entry prints. The truth check (6b) requires
    #: the header to be exactly what the snapshot says of it, and every bullet's facts to
    #: hang off it. None only for a hand-built page that is not checked against a snapshot.
    source_id: Annotated[uuid.UUID | None, Sensitivity.S0] = None


class DocumentSection(AriaMessage):
    kind: Annotated[SectionKind, Sensitivity.S0]
    #: Plain lines: the summary's sentences, the skills lines.
    lines: Annotated[tuple[str, ...], Sensitivity.S1] = ()
    entries: tuple[DocumentEntry, ...] = ()

    @property
    def heading(self) -> str:
        return HEADINGS[self.kind]


class ResumeDocument(AriaMessage):
    name: Annotated[str, Sensitivity.S1] = Field(min_length=1, max_length=120)
    contact: tuple[ContactItem, ...] = ()
    sections: tuple[DocumentSection, ...] = ()

    @property
    def contact_line(self) -> str:
        return CONTACT_SEPARATOR.join(item.text for item in self.contact)
