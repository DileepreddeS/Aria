"""Re-tag the graph's skill edges with the taxonomy in hand (DECISIONS 2026-10-17).

A fact's skills are tagged when it is created, with the taxonomy of that day. When the
taxonomy changes (2026.10.3 split sibling products out of each other's aliases), the
stored edges keep their old ids: RabbitMQ is still filed as "message queues", and the
word "checkout" still as payments. Matching and skill-years read those ids, so they
drift from what the facts say. This brings the edges back in line, at the candidate's
request, after they have seen every change.

**Edges only.** A fact's text, revisions, version and approval state are never touched,
nor a declared skill's approval state: re-tagging decides which skill a word names,
never whether a statement is true. For each fact:

- a stored edge is read again through its spelling, the candidate's own word: kept if
  it still names the same skill, re-pointed if it now names another ("RabbitMQ" from
  message queues to RabbitMQ), and dropped only if it names nothing ("checkout");
- what the taxonomy now finds in the fact's current text is added.

A stored edge the text does not spell out (an enrichment answer about Kafka that never
repeats the word) is kept as long as its spelling names a skill. A declared skill is
re-pointed the same way; one whose spelling names nothing, or would collide with another
declared skill, is left as it is and reported, because removing it would undo a decision
the candidate made.

**Exactly what was shown.** The preview carries a fingerprint of every change. Applying
plans again inside the transaction and refuses if the fingerprint differs, so a change
the candidate did not see is never written.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.audit.log import append_event
from aria_core.db.models import DeclaredSkill, FactSkill
from aria_core.graph.facts import facts_for_deduplication
from aria_core.importer.skills import tag_skills
from aria_core.schemas.audit import ActorKind, AuditAction, AuditEventDraft, AuditOutcome, SubjectKind
from aria_core.schemas.identity import TenantId
from aria_core.taxonomy.model import SkillTaxonomy

__all__ = ["DeclaredRetag", "FactRetag", "Retag", "StaleRetag", "apply_retag", "plan_retag"]

Edge = tuple[str, str]
"""``(canonical, surface_form)``."""


class StaleRetag(Exception):
    """The graph or the taxonomy changed after the preview; nothing was written."""


@dataclass(frozen=True, slots=True)
class FactRetag:
    fact_id: uuid.UUID
    status: str
    text: str
    removed: tuple[Edge, ...]
    added: tuple[Edge, ...]


@dataclass(frozen=True, slots=True)
class DeclaredRetag:
    declared_id: uuid.UUID
    surface_form: str
    old: str
    #: The id its spelling names now, or None when it names nothing.
    new: str | None
    #: Why it is left as it is, when it is.
    left_because: str | None = None


@dataclass(frozen=True, slots=True)
class Retag:
    taxonomy_version: str
    facts: tuple[FactRetag, ...]
    declared: tuple[DeclaredRetag, ...]

    @property
    def changes(self) -> int:
        return sum(len(item.removed) + len(item.added) for item in self.facts) + sum(
            1 for item in self.declared if item.left_because is None
        )

    @property
    def fingerprint(self) -> str:
        """Every change, in a stable order: what the candidate is asked to confirm."""
        body = {
            "taxonomy": self.taxonomy_version,
            "facts": [[str(item.fact_id), sorted(item.removed), sorted(item.added)] for item in self.facts],
            "declared": [
                [str(item.declared_id), item.old, item.new, item.left_because] for item in self.declared
            ],
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


async def plan_retag(session: AsyncSession, *, candidate_id: uuid.UUID, taxonomy: SkillTaxonomy) -> Retag:
    """What re-tagging would change for one candidate. Changes nothing."""
    # Every fact, whatever its state, through the repository's read path: a draft's
    # edges drift as much as an approved fact's, and nothing here reaches a resume.
    facts = [
        (fact_id, status.value, text)
        for fact_id, text, status in await facts_for_deduplication(session, candidate_id)
    ]
    stored: dict[uuid.UUID, set[Edge]] = {}
    if facts:
        rows = (
            await session.execute(
                select(FactSkill.fact_id, FactSkill.skill_canonical, FactSkill.surface_form).where(
                    FactSkill.fact_id.in_([row[0] for row in facts])
                )
            )
        ).all()
        for fact_id, canonical, surface in rows:
            stored.setdefault(fact_id, set()).add((canonical, surface))

    changed: list[FactRetag] = []
    for fact_id, status, text in facts:
        before = stored.get(fact_id, set())
        after: dict[str, str] = {}
        for _, surface in sorted(before):
            now = taxonomy.resolve(surface)
            if now is not None:
                after.setdefault(now, surface)
        for skill in tag_skills(text, taxonomy).skills:
            after.setdefault(skill.canonical, skill.surface_form)
        before_ids = {canonical: surface for canonical, surface in before}
        removed = tuple(sorted((c, s) for c, s in before_ids.items() if c not in after))
        added = tuple(sorted((c, s) for c, s in after.items() if c not in before_ids))
        if removed or added:
            changed.append(FactRetag(fact_id, status, text, removed, added))

    declared_rows = (
        await session.execute(
            select(DeclaredSkill.id, DeclaredSkill.skill_canonical, DeclaredSkill.surface_form)
            .where(DeclaredSkill.candidate_id == candidate_id)
            .order_by(DeclaredSkill.surface_form, DeclaredSkill.id)
        )
    ).all()
    held = {canonical for _, canonical, _ in declared_rows}
    declared: list[DeclaredRetag] = []
    for declared_id, canonical, surface in declared_rows:
        now = taxonomy.resolve(surface)
        if now == canonical:
            continue
        if now is None:
            declared.append(
                DeclaredRetag(declared_id, surface, canonical, None, "its spelling names no skill now")
            )
        elif now in held:
            declared.append(DeclaredRetag(declared_id, surface, canonical, now, f"{now} is already declared"))
        else:
            held.add(now)
            declared.append(DeclaredRetag(declared_id, surface, canonical, now))
    return Retag(taxonomy.version, tuple(changed), tuple(declared))


async def apply_retag(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    actor_id: str,
    taxonomy: SkillTaxonomy,
    fingerprint: str,
) -> Retag:
    """Write the previewed re-tag, if it is still exactly what was previewed."""
    plan = await plan_retag(session, candidate_id=candidate_id, taxonomy=taxonomy)
    if plan.fingerprint != fingerprint:
        raise StaleRetag("the graph or the taxonomy changed since the preview")
    for item in plan.facts:
        for canonical, _ in item.removed:
            await session.execute(
                delete(FactSkill).where(
                    FactSkill.fact_id == item.fact_id, FactSkill.skill_canonical == canonical
                )
            )
        for canonical, surface in item.added:
            await session.execute(
                insert(FactSkill).values(
                    fact_id=item.fact_id, skill_canonical=canonical, tenant_id=tenant_id, surface_form=surface
                )
            )
    for entry in plan.declared:
        if entry.left_because is None and entry.new is not None:
            await session.execute(
                update(DeclaredSkill)
                .where(DeclaredSkill.id == entry.declared_id, DeclaredSkill.skill_canonical == entry.old)
                .values(skill_canonical=entry.new)
            )
    await append_event(
        session,
        AuditEventDraft(
            action=AuditAction.GRAPH_RETAGGED,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            subject_kind=SubjectKind.CANDIDATE,
            subject_id=str(candidate_id),
            outcome=AuditOutcome.OK,
            reason="skill edges re-tagged with the current taxonomy at the candidate's request",
            # Ids, counts and taxonomy ids only: S0 metadata.
            payload={
                "taxonomy_version": plan.taxonomy_version,
                "fingerprint": plan.fingerprint,
                "facts_changed": len(plan.facts),
                "edges_removed": sum(len(item.removed) for item in plan.facts),
                "edges_added": sum(len(item.added) for item in plan.facts),
                "declared_repointed": sum(1 for item in plan.declared if item.left_because is None),
                "declared_left": sum(1 for item in plan.declared if item.left_because is not None),
            },
        ),
        tenant_id=tenant_id,
    )
    return plan
