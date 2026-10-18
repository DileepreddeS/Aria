"""Which skills an approved fact names: the one rule the matcher and the truth check share.

A fact's stored tags are read again through their spelling, the candidate's own word,
with the taxonomy in hand: a tag stored when Memcached was filed under Redis names
Memcached, and one whose word names nothing now ("checkout") names nothing. What the
taxonomy finds in the fact's current text is added. Shared so that what the matcher
scores as evidence for a skill is exactly what the truth check would let a bullet claim.
"""

from __future__ import annotations

from collections.abc import Iterable

from aria_core.importer.skills import tag_skills
from aria_core.schemas.snapshot import SnapshotFact
from aria_core.taxonomy.model import SkillTaxonomy

__all__ = ["skills_named"]


def skills_named(facts: Iterable[SnapshotFact], taxonomy: SkillTaxonomy) -> set[str]:
    named: set[str] = set()
    for fact in facts:
        for skill in fact.skills:
            canonical = taxonomy.resolve(skill.surface_form)
            if canonical is not None:
                named.add(canonical)
        named |= {skill.canonical for skill in tag_skills(fact.text, taxonomy).skills}
    return named
