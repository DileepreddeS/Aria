"""The voice rules the recruiter check (6c) applies, loaded from style.yaml and checked.

A list that contradicts itself is refused when loaded, not discovered on a resume: a verb
both allowed and banned, an allowed verb that is also a weak opening, a duplicate, a
threshold that could never pass.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = ["STYLE_PATH", "Style", "StyleError", "load_style"]

STYLE_PATH = Path(__file__).with_name("style.yaml")


class StyleError(ValueError):
    """style.yaml is malformed or contradicts itself."""


class Style(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    allowed_verbs: tuple[str, ...] = Field(min_length=1)
    max_bullets_per_verb: int = Field(ge=1)
    min_quantified_share: float = Field(ge=0.0, le=1.0)
    banned: tuple[str, ...]
    weak_openings: tuple[str, ...]
    vague_phrases: tuple[str, ...]


def _problems(style: Style) -> list[str]:
    problems: list[str] = []
    for name in ("allowed_verbs", "banned", "weak_openings", "vague_phrases"):
        values: tuple[str, ...] = getattr(style, name)
        for value in values:
            if value != value.lower() or value != " ".join(value.split()) or not value:
                problems.append(f"{name}: {value!r} must be lowercase, single-spaced and non-empty")
        repeated = sorted({value for value in values if values.count(value) > 1})
        if repeated:
            problems.append(f"{name}: {repeated} listed twice")
    verbs = set(style.allowed_verbs)
    if verbs & set(style.banned):
        problems.append(f"allowed_verbs: {sorted(verbs & set(style.banned))} also banned")
    if verbs & set(style.weak_openings):
        problems.append(f"weak_openings: {sorted(verbs & set(style.weak_openings))} is an allowed verb")
    return problems


def load_style(path: Path = STYLE_PATH) -> Style:
    """The style at ``path``; the shipped one by default, read once."""
    return _load(path.resolve())


@cache
def _load(path: Path) -> Style:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise StyleError(f"{path.name}: expected a mapping")
    try:
        style = Style.model_validate(raw)
    except ValidationError as error:
        fields = ", ".join(".".join(str(part) for part in item["loc"]) for item in error.errors())
        raise StyleError(f"{path.name}: {fields}: {error}") from error
    problems = _problems(style)
    if problems:
        raise StyleError(f"{path.name}: " + "; ".join(problems))
    return style
