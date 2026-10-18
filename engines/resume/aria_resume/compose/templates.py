"""Role templates (RESUME_ENGINE §8 5a), loaded from templates/roles/*.yaml and checked.

A template is configuration a person reviews: section order, how many bullets each kind of
entry gets, how many projects, which skill categories the skills lines show and in what
order. A template that contradicts itself (an unknown section, a minimum above its maximum,
an unknown skill category) is refused when loaded, not discovered on a page.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aria_core.taxonomy.model import SkillCategory
from aria_resume.document import SectionKind
from aria_resume.schemas import RoleType

__all__ = ["ROLES", "RoleTemplate", "TemplateError", "load_template"]

ROLES = Path(__file__).with_name("templates") / "roles"


class TemplateError(ValueError):
    """A role template is malformed or contradicts itself."""


class SkillsLayout(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    categories: tuple[SkillCategory, ...] = Field(min_length=1)
    max_terms_per_line: int = Field(ge=1, le=30)


class RoleTemplate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    role: RoleType
    summary: str
    section_order: tuple[SectionKind, ...]
    recent_role_bullets: tuple[int, int]
    older_role_bullets: tuple[int, int]
    project_bullets: tuple[int, int]
    max_projects: int = Field(ge=0, le=6)
    skills: SkillsLayout

    @model_validator(mode="after")
    def _consistent(self) -> RoleTemplate:
        if self.summary != "model_only":
            raise ValueError("summary must be model_only: no template sentence stands in for one")
        if len(set(self.section_order)) != len(self.section_order):
            raise ValueError("a section is listed twice")
        if SectionKind.SUMMARY in self.section_order:
            raise ValueError("the summary is placed by the writer, not the template's section order")
        for required in (SectionKind.EXPERIENCE, SectionKind.SKILLS, SectionKind.EDUCATION):
            if required not in self.section_order:
                raise ValueError(f"section_order must include {required.value}")
        for name in ("recent_role_bullets", "older_role_bullets", "project_bullets"):
            low, high = getattr(self, name)
            if not 0 <= low <= high <= 8:
                raise ValueError(f"{name} must be [minimum, maximum] with 0 <= minimum <= maximum <= 8")
        return self


def load_template(role: RoleType, directory: Path = ROLES) -> RoleTemplate:
    return _load((directory / f"{role.value}.yaml").resolve())


@cache
def _load(path: Path) -> RoleTemplate:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise TemplateError(f"no role template at {path.name}") from error
    try:
        template = RoleTemplate.model_validate(raw)
    except ValidationError as error:
        raise TemplateError(f"{path.name}: {error}") from error
    if template.role.value != path.stem:
        raise TemplateError(f"{path.name} declares role {template.role.value}")
    return template
