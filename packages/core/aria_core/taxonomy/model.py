"""The skill taxonomy (RESUME_ENGINE §4).

Three jobs, and it is worth being clear which is which, because conflating them is
how a resume ends up claiming something the candidate never said:

* **Resolution.** "React.js", "ReactJS" and "react" are the same skill. One
  canonical id, so a fact tagged from a resume and a requirement parsed from a JD
  can be compared at all.
* **Surface forms.** The JD's own spelling is kept, because ATS keyword matching is
  literal: if the posting says "PostgreSQL", the resume says "PostgreSQL", not
  "Postgres".
* **Implication.** Next.js implies React implies JavaScript. Useful for *matching* a
  requirement to evidence. Never for a claim: years of React do not become years of
  JavaScript, and :mod:`aria_core.graph.derive` keeps the two apart.

Unknown terms are returned as unresolved and reported, never silently dropped — a
skill the taxonomy has not heard of is a gap in the taxonomy, and pretending it does
not exist loses a real requirement.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Skill", "SkillCategory", "SkillTaxonomy", "TaxonomyError"]


class TaxonomyError(Exception):
    """The taxonomy file is not usable. Raised at load time, never at use time."""


class SkillCategory(StrEnum):
    """Used to balance a skills line and to pick a role template later."""

    LANGUAGE = "language"
    FRONTEND = "frontend"
    BACKEND = "backend"
    DATA = "data"
    ML = "ml"
    INFRA = "infra"
    TOOLING = "tooling"
    PRACTICE = "practice"
    """Testing, accessibility, code review \u2014 things done rather than used."""
    DOMAIN = "domain"
    """Distributed systems, computer vision, payments."""


@dataclass(frozen=True, slots=True)
class Skill:
    """One canonical skill."""

    canonical: str
    display: str
    """The spelling ARIA uses when the JD does not provide one ("PostgreSQL")."""
    category: SkillCategory
    aliases: frozenset[str] = frozenset()
    implies: tuple[str, ...] = ()
    """Canonical ids this skill is evidence for. Directed, acyclic."""

    def __post_init__(self) -> None:
        if not self.canonical:
            raise TaxonomyError("a skill needs a canonical id")
        if self.canonical != self.canonical.lower():
            raise TaxonomyError(f"canonical ids are lowercase: {self.canonical!r}")


def normalise(term: str) -> str:
    """Fold a surface form to a lookup key.

    Deliberately conservative: case, surrounding punctuation and internal
    whitespace/separators only. It does not stem or strip digits, because "python 3"
    and "python" are the same skill while "c" and "c++" are not, and "vue 2" matters.
    """
    folded = term.strip().lower()
    folded = folded.replace("&", " and ")
    for separator in ("_", "-", "/", ".", ",", "(", ")", "[", "]"):
        folded = folded.replace(separator, " ")

    return " ".join(folded.split())


@dataclass(frozen=True)
class SkillTaxonomy:
    """A loaded, validated taxonomy.

    ``version`` travels into every resume artifact, so a resume built last month can
    be explained with the taxonomy that built it (RESUME_ENGINE §9).
    """

    version: str
    skills: Mapping[str, Skill]
    _by_alias: Mapping[str, str] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ lookups
    def resolve(self, term: str) -> str | None:
        """The canonical id for a surface form, or ``None`` if unknown."""
        return self._by_alias.get(normalise(term))

    def resolve_all(self, terms: Iterable[str]) -> tuple[dict[str, str], list[str]]:
        """``({surface form: canonical}, [unknown surface forms])``.

        Both halves are returned because the unknown half is the useful one: it is
        what gets reviewed and added to the taxonomy.
        """
        resolved: dict[str, str] = {}
        unknown: list[str] = []
        for term in terms:
            canonical = self.resolve(term)
            if canonical is None:
                unknown.append(term)
            else:
                resolved[term] = canonical
        return resolved, unknown

    def get(self, canonical: str) -> Skill:
        try:
            return self.skills[canonical]
        except KeyError as error:
            raise TaxonomyError(f"no skill with canonical id {canonical!r}") from error

    def __contains__(self, canonical: object) -> bool:
        return canonical in self.skills

    def __iter__(self) -> Iterator[Skill]:
        return iter(self.skills.values())

    def __len__(self) -> int:
        return len(self.skills)

    # -------------------------------------------------------------- implications
    def implications(self, canonical: str) -> frozenset[str]:
        """Everything this skill is evidence for, transitively, excluding itself.

        ``implications("nextjs")`` is ``{react, javascript, frontend_web}``. Used by
        the Evidence Matcher to score an implied match lower than a direct one —
        never to add years or to put a skill on a resume the candidate did not list.
        """
        seen: set[str] = set()
        frontier = list(self.get(canonical).implies)
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(self.get(current).implies)
        return frozenset(seen)

    def display_form(self, canonical: str, *, preferred: str | None = None) -> str:
        """The spelling to print. A JD's own surface form wins when there is one."""
        if preferred and self.resolve(preferred) == canonical:
            return preferred.strip()
        return self.get(canonical).display
