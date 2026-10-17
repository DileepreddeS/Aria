"""Loading and validating the taxonomy seed.

Everything that can be wrong with the file is checked here, at load time, so a
malformed taxonomy is a start-up failure rather than a resume with a missing skill:
duplicate canonical ids, an alias claimed by two skills, an ``implies`` edge pointing
at nothing, cycles, and a sibling group naming a skill that does not exist or two skills
of which one is a kind of the other.

Cycles matter more than they look. :meth:`SkillTaxonomy.implications` walks the graph
transitively; a cycle would still terminate because visited ids are tracked, but it
would mean React implies JavaScript implies React, and an implied match would be
indistinguishable from a direct one. Refusing to load is the honest response.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from aria_core.taxonomy.model import Skill, SkillCategory, SkillTaxonomy, TaxonomyError, normalise

__all__ = ["DEFAULT_TAXONOMY_PATH", "load_default_taxonomy", "load_taxonomy"]

#: The seed ships with the repository: it is public, non-personal (S0) data.
DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[4] / "data" / "seed" / "taxonomy" / "skills.yaml"


def load_taxonomy(path: Path) -> SkillTaxonomy:
    """Read, validate and index a taxonomy file."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise TaxonomyError(f"no taxonomy at {path}") from error
    except yaml.YAMLError as error:
        raise TaxonomyError(f"{path} is not valid YAML: {error}") from error

    if not isinstance(raw, dict) or "version" not in raw or "skills" not in raw:
        raise TaxonomyError(f"{path} must be a mapping with 'version' and 'skills'")

    version = str(raw["version"])
    entries = raw["skills"]
    if not isinstance(entries, list) or not entries:
        raise TaxonomyError(f"{path}: 'skills' must be a non-empty list")

    skills: dict[str, Skill] = {}
    by_alias: dict[str, str] = {}

    for entry in entries:
        skill = _build_skill(entry, path)
        if skill.canonical in skills:
            raise TaxonomyError(f"{path}: duplicate canonical id {skill.canonical!r}")
        skills[skill.canonical] = skill

        # The canonical id and the display form are both lookup keys, so a JD saying
        # either finds the skill without the file having to repeat itself.
        for form in {skill.canonical, skill.display, *skill.aliases}:
            key = normalise(form)
            owner = by_alias.get(key)
            if owner is not None and owner != skill.canonical:
                raise TaxonomyError(
                    f"{path}: {form!r} is claimed by both {owner!r} and {skill.canonical!r}; "
                    "an ambiguous alias would resolve a requirement to the wrong skill"
                )
            by_alias[key] = skill.canonical

    _validate_edges(skills, path)
    siblings = _sibling_groups(raw.get("sibling_groups") or [], skills, path)
    return SkillTaxonomy(version=version, skills=skills, _by_alias=by_alias, _siblings=siblings)


def _sibling_groups(groups: Any, skills: dict[str, Skill], path: Path) -> dict[str, frozenset[str]]:
    """Each group's members are alternatives to each other, pairwise and symmetric.

    A member that implies another is not its sibling but a kind of it (Hive is not an
    alternative to Hadoop, it runs on it), so that pair is refused.
    """
    if not isinstance(groups, list):
        raise TaxonomyError(f"{path}: 'sibling_groups' must be a list of lists")
    found: dict[str, set[str]] = {}
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            raise TaxonomyError(f"{path}: a sibling group needs two or more ids, got {group!r}")
        members = [str(member) for member in group]
        if len(set(members)) != len(members):
            raise TaxonomyError(f"{path}: sibling group {members} names a skill twice")
        for member in members:
            if member not in skills:
                raise TaxonomyError(f"{path}: sibling group {members} names {member!r}, which is not a skill")
        for member in members:
            for other in members:
                if member != other and other in _closure(skills, member):
                    raise TaxonomyError(
                        f"{path}: {member!r} implies {other!r}, so it is a kind of it, not a sibling"
                    )
            found.setdefault(member, set()).update(other for other in members if other != member)
    return {member: frozenset(others) for member, others in found.items()}


def _closure(skills: dict[str, Skill], canonical: str) -> set[str]:
    seen: set[str] = set()
    frontier = list(skills[canonical].implies)
    while frontier:
        current = frontier.pop()
        if current not in seen:
            seen.add(current)
            frontier.extend(skills[current].implies)
    return seen


def _build_skill(entry: Any, path: Path) -> Skill:
    if not isinstance(entry, dict) or "id" not in entry:
        raise TaxonomyError(f"{path}: each skill needs an 'id', got {entry!r}")

    canonical = str(entry["id"])
    category_name = str(entry.get("category", "")).strip()
    try:
        category = SkillCategory(category_name)
    except ValueError as error:
        raise TaxonomyError(
            f"{path}: skill {canonical!r} has category {category_name!r}; "
            f"expected one of {sorted(member.value for member in SkillCategory)}"
        ) from error

    aliases = entry.get("aliases") or []
    if not isinstance(aliases, list):
        raise TaxonomyError(f"{path}: skill {canonical!r} has a non-list 'aliases'")
    implies = entry.get("implies") or []
    if not isinstance(implies, list):
        raise TaxonomyError(f"{path}: skill {canonical!r} has a non-list 'implies'")
    common_word = entry.get("common_word", False)
    if not isinstance(common_word, bool):
        raise TaxonomyError(f"{path}: skill {canonical!r} has a non-boolean 'common_word'")

    return Skill(
        canonical=canonical,
        display=str(entry.get("display", canonical)),
        category=category,
        aliases=frozenset(str(alias) for alias in aliases),
        implies=tuple(str(target) for target in implies),
        common_word=common_word,
    )


def _validate_edges(skills: dict[str, Skill], path: Path) -> None:
    """Every edge points at a real skill, and the graph is acyclic."""
    for skill in skills.values():
        for target in skill.implies:
            if target not in skills:
                raise TaxonomyError(f"{path}: {skill.canonical!r} implies {target!r}, which is not a skill")
            if target == skill.canonical:
                raise TaxonomyError(f"{path}: {skill.canonical!r} implies itself")

    # Iterative depth-first search with an explicit stack: a deep chain should not
    # depend on Python's recursion limit.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(skills, WHITE)

    for start in skills:
        if colour[start] != WHITE:
            continue
        stack: list[tuple[str, list[str]]] = [(start, list(skills[start].implies))]
        colour[start] = GREY
        trail = [start]
        while stack:
            node, remaining = stack[-1]
            if not remaining:
                colour[node] = BLACK
                stack.pop()
                trail.pop()
                continue
            target = remaining.pop()
            if colour[target] == GREY:
                cycle = " -> ".join([*trail, target])
                raise TaxonomyError(
                    f"{path}: implication cycle {cycle}. A cycle makes an implied match "
                    "indistinguishable from a direct one."
                )
            if colour[target] == WHITE:
                colour[target] = GREY
                trail.append(target)
                stack.append((target, list(skills[target].implies)))


@lru_cache(maxsize=1)
def load_default_taxonomy() -> SkillTaxonomy:
    """The seed taxonomy, read once per process."""
    return load_taxonomy(DEFAULT_TAXONOMY_PATH)
