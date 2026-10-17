"""The implied skills a skills line may list: implied_skills.yaml, checked against the taxonomy.

A pair is refused when loaded unless it is a direct ``implies`` edge, one step, between
two skills the taxonomy holds: the list exists to narrow the taxonomy's implications,
never to add one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aria_core.taxonomy.model import SkillTaxonomy

__all__ = ["IMPLIED_PATH", "ImpliedSkills", "ImpliedSkillsError", "load_implied_skills"]

IMPLIED_PATH = Path(__file__).with_name("implied_skills.yaml")


class ImpliedSkillsError(ValueError):
    """implied_skills.yaml is malformed or names an implication the taxonomy lacks."""


@dataclass(frozen=True, slots=True)
class ImpliedSkills:
    version: str
    pairs: Mapping[str, frozenset[str]]

    def listable(self, held: set[str]) -> set[str]:
        """What a skills line may list for a candidate who holds ``held``: those, and
        each allowed one-step implication of them."""
        return held | {target for skill in held for target in self.pairs.get(skill, frozenset())}


def load_implied_skills(taxonomy: SkillTaxonomy, path: Path = IMPLIED_PATH) -> ImpliedSkills:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"version", "pairs"}:
        raise ImpliedSkillsError(f"{path.name}: expected exactly 'version' and 'pairs'")
    version = str(raw["version"] or "")
    if not version:
        raise ImpliedSkillsError(f"{path.name}: a version is required")
    if not isinstance(raw["pairs"], dict):
        raise ImpliedSkillsError(f"{path.name}: 'pairs' maps a skill to the skills it may imply")
    problems: list[str] = []
    pairs: dict[str, frozenset[str]] = {}
    for source, targets in raw["pairs"].items():
        if not isinstance(targets, list) or not targets:
            problems.append(f"{source}: expected a non-empty list")
            continue
        if source not in taxonomy:
            problems.append(f"{source}: not a skill in taxonomy {taxonomy.version}")
            continue
        if len(set(targets)) != len(targets):
            problems.append(f"{source}: a target listed twice")
        direct = set(taxonomy.get(source).implies)
        for target in targets:
            if target not in direct:
                problems.append(
                    f"{source} -> {target}: not a direct implication in taxonomy {taxonomy.version}"
                )
        pairs[str(source)] = frozenset(str(target) for target in targets)
    if problems:
        raise ImpliedSkillsError(f"{path.name}: " + "; ".join(problems))
    return ImpliedSkills(version=version, pairs=pairs)
