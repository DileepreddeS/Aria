"""Reading and changing facts (RESUME_ENGINE §5).

**The only read path for anything that builds output.** :func:`approved_facts` filters
on status; :func:`draft_facts` is for the approval flow and says so in its name. An
architecture test fails if any other module queries the ``facts`` table, so "only
verified facts can appear on a resume" is a property of the code's shape rather than
something each new feature has to remember.

Changing a fact goes through the lifecycle below. Nothing in it verifies itself: the
transition to ``APPROVED`` needs a user id, because approving a fact is the candidate
asserting it is true about them, and no import or model may do that on their behalf.

::

    DRAFT ──approve──► APPROVED ──edit──► (new revision, APPROVED)
      │                   │
      │                   └──reject──► REJECTED
      ├──edit──► (new revision, DRAFT)
      └──reject──► REJECTED ──reconsider──► DRAFT

An edit appends a revision and moves ``current_version``; it never rewrites text. The
database enforces that too — ``aria_app`` has no UPDATE on ``fact_revisions``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Select, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.audit.log import append_event
from aria_core.db.models import Fact, FactMetric, FactRevision, FactSkill, FactTheme
from aria_core.graph.status import ApprovalStatus, GraphSource
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    SubjectKind,
)
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId, UserId, new_id

__all__ = [
    "FactNotFound",
    "FactView",
    "IllegalFactTransition",
    "approve_fact",
    "approved_facts",
    "approved_skill_usage",
    "create_draft_fact",
    "draft_facts",
    "edit_fact",
    "fact_skill_canonicals",
    "facts_for_deduplication",
    "merge_facts",
    "reconsider_fact",
    "reject_fact",
]


class FactNotFound(Exception):
    """No such fact in this tenant."""


class IllegalFactTransition(Exception):
    """The lifecycle does not allow this change."""

    def __init__(self, current: ApprovalStatus, action: str) -> None:
        super().__init__(f"cannot {action} a fact that is {current.value}")
        self.current = current
        self.action = action


#: What may happen to a fact in each state. A table rather than scattered checks, for
#: the same reason the application lifecycle is one (PRODUCT_SPEC §3.7).
ALLOWED_ACTIONS: Final[dict[ApprovalStatus, frozenset[str]]] = {
    ApprovalStatus.DRAFT: frozenset({"approve", "edit", "reject"}),
    ApprovalStatus.APPROVED: frozenset({"edit", "reject"}),
    ApprovalStatus.REJECTED: frozenset({"reconsider"}),
    # A superseded revision is history. The fact itself is never in this state; it is
    # here so the table covers the enum and a new state cannot be forgotten.
    ApprovalStatus.SUPERSEDED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class FactView:
    """A fact and its current text, as the approval flow and the engines see it.

    ``source_line`` and ``source_upload_id`` travel with it so the approval view can
    show the candidate the line of their own resume this came from.
    """

    fact_id: uuid.UUID
    text: str
    version: int
    status: ApprovalStatus
    source: GraphSource
    source_upload_id: uuid.UUID | None
    source_line: int | None
    experience_id: uuid.UUID | None
    project_id: uuid.UUID | None
    education_id: uuid.UUID | None
    approved_at: dt.datetime | None

    @property
    def anchor_id(self) -> uuid.UUID | None:
        """The one thing this fact is attached to, if any."""
        return self.experience_id or self.project_id or self.education_id


def _current_text_query(candidate_id: uuid.UUID, status: ApprovalStatus) -> Select[*tuple[Any, ...]]:
    """Facts in one status, joined to the revision ``current_version`` points at."""
    return (
        select(
            Fact.id,
            FactRevision.text,
            FactRevision.version,
            Fact.status,
            Fact.source,
            Fact.source_upload_id,
            Fact.source_line,
            Fact.experience_id,
            Fact.project_id,
            Fact.education_id,
            Fact.approved_at,
        )
        .join(
            FactRevision,
            (FactRevision.fact_id == Fact.id) & (FactRevision.version == Fact.current_version),
        )
        .where(Fact.candidate_id == candidate_id, Fact.status == status.value)
        .order_by(Fact.created_at, Fact.id)
    )


def _to_views(rows: Sequence[tuple[Any, ...]]) -> list[FactView]:
    return [
        FactView(
            fact_id=row[0],
            text=row[1],
            version=row[2],
            status=ApprovalStatus(row[3]),
            source=GraphSource(row[4]),
            source_upload_id=row[5],
            source_line=row[6],
            experience_id=row[7],
            project_id=row[8],
            education_id=row[9],
            approved_at=row[10],
        )
        for row in rows
    ]


async def approved_facts(session: AsyncSession, candidate_id: uuid.UUID) -> list[FactView]:
    """Every fact the candidate has approved. **The only read path for the engines.**

    A resume or an answer built from anything else would be making a claim the
    candidate never agreed to, which is the one thing this product must not do.
    """
    rows = (await session.execute(_current_text_query(candidate_id, ApprovalStatus.APPROVED))).all()
    return _to_views(rows)


async def draft_facts(session: AsyncSession, candidate_id: uuid.UUID) -> list[FactView]:
    """Facts waiting for a decision. For the approval flow only."""
    rows = (await session.execute(_current_text_query(candidate_id, ApprovalStatus.DRAFT))).all()
    return _to_views(rows)


async def rejected_facts(session: AsyncSession, candidate_id: uuid.UUID) -> list[FactView]:
    """Facts the candidate turned down. Kept so an import does not re-propose them."""
    rows = (await session.execute(_current_text_query(candidate_id, ApprovalStatus.REJECTED))).all()
    return _to_views(rows)


async def facts_for_deduplication(
    session: AsyncSession, candidate_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, ApprovalStatus]]:
    """Every fact's current text **with its status**, for near-duplicate detection.

    An importer has to see all four states to behave correctly, and each for a
    different reason: an approved or draft match is a duplicate to skip or merge, and
    a *rejected* match is a wording the candidate has already turned down, which must
    never be proposed again.

    The status comes back with every row rather than being filtered here, because the
    caller's job is to treat the states differently. It is a read path, so it is named
    in ``test_architecture_verified_only.py`` — nothing may use it to put text on a
    resume, which is what :func:`approved_facts` is for.
    """
    rows = (
        await session.execute(
            select(Fact.id, FactRevision.text, Fact.status)
            .join(
                FactRevision,
                (FactRevision.fact_id == Fact.id) & (FactRevision.version == Fact.current_version),
            )
            .where(Fact.candidate_id == candidate_id)
            .order_by(Fact.created_at, Fact.id)
        )
    ).all()
    return [(row[0], row[1], ApprovalStatus(row[2])) for row in rows]


async def fact_skill_canonicals(session: AsyncSession, candidate_id: uuid.UUID) -> set[str]:
    """Skills mentioned by any fact that has not been rejected.

    Draft facts count. The question this answers is "is there anything behind this
    listed skill?", and a draft bullet about Kafka is something behind it -- asking
    again because it has not been approved yet would be asking about a gap the
    candidate has already filled.

    A read path, so it is named in ``test_architecture_verified_only.py``. It returns
    skill ids and no text, which is what keeps it useless for building a resume.
    """
    rows = (
        await session.execute(
            select(FactSkill.skill_canonical)
            .join(Fact, Fact.id == FactSkill.fact_id)
            .where(
                Fact.candidate_id == candidate_id,
                Fact.status != ApprovalStatus.REJECTED.value,
            )
        )
    ).all()
    return {row[0] for row in rows}


async def get_fact(session: AsyncSession, fact_id: uuid.UUID) -> FactView:
    row = (
        await session.execute(
            select(
                Fact.id,
                FactRevision.text,
                FactRevision.version,
                Fact.status,
                Fact.source,
                Fact.source_upload_id,
                Fact.source_line,
                Fact.experience_id,
                Fact.project_id,
                Fact.education_id,
                Fact.approved_at,
            )
            .join(
                FactRevision,
                (FactRevision.fact_id == Fact.id) & (FactRevision.version == Fact.current_version),
            )
            .where(Fact.id == fact_id)
        )
    ).first()
    if row is None:
        raise FactNotFound(str(fact_id))
    return _to_views([tuple(row)])[0]


# --------------------------------------------------------------------------- writes


async def create_draft_fact(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    text: str,
    source: GraphSource,
    experience_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    education_id: uuid.UUID | None = None,
    source_upload_id: uuid.UUID | None = None,
    source_line: int | None = None,
    skills: Sequence[tuple[str, str]] = (),
    themes: Sequence[str] = (),
    metrics: Sequence[tuple[str, str | None, str | None]] = (),
) -> uuid.UUID:
    """Create a fact as a draft, with its edges. Returns the fact id.

    ``skills`` is ``(canonical, surface_form)``; ``metrics`` is
    ``(exact_value, measures, baseline)``. There is no parameter for creating an
    approved fact: approval is a separate, audited act by a person.
    """
    fact_id = new_id()
    await session.execute(
        insert(Fact).values(
            id=fact_id,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            experience_id=experience_id,
            project_id=project_id,
            education_id=education_id,
            status=ApprovalStatus.DRAFT.value,
            current_version=1,
            source=source.value,
            source_upload_id=source_upload_id,
            source_line=source_line,
        )
    )
    await session.execute(
        insert(FactRevision).values(
            fact_id=fact_id,
            version=1,
            tenant_id=tenant_id,
            text=text.strip(),
            source=source.value,
        )
    )
    for canonical, surface_form in skills:
        await session.execute(
            insert(FactSkill).values(
                fact_id=fact_id,
                skill_canonical=canonical,
                tenant_id=tenant_id,
                surface_form=surface_form,
            )
        )
    for theme in themes:
        await session.execute(insert(FactTheme).values(fact_id=fact_id, theme=theme, tenant_id=tenant_id))
    for exact_value, measures, baseline in metrics:
        await session.execute(
            insert(FactMetric).values(
                id=new_id(),
                tenant_id=tenant_id,
                fact_id=fact_id,
                exact_value=exact_value,
                measures=measures,
                baseline=baseline,
                source_line=source_line,
            )
        )
    return fact_id


async def _require(session: AsyncSession, fact_id: uuid.UUID, action: str) -> tuple[ApprovalStatus, int]:
    """Lock the fact, check the action is allowed, return its status and version."""
    row = (
        await session.execute(
            select(Fact.status, Fact.current_version).where(Fact.id == fact_id).with_for_update()
        )
    ).first()
    if row is None:
        raise FactNotFound(str(fact_id))
    status = ApprovalStatus(row[0])
    if action not in ALLOWED_ACTIONS[status]:
        raise IllegalFactTransition(status, action)
    return status, row[1]


async def _audit(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    fact_id: uuid.UUID,
    action: AuditAction,
    actor_id: str,
    payload: dict[str, object],
) -> None:
    await append_event(
        session,
        AuditEventDraft(
            action=action,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            subject_kind=SubjectKind.FACT,
            subject_id=str(fact_id),
            outcome=AuditOutcome.OK,
            payload=payload,
        ),
        tenant_id=tenant_id,
    )


async def approve_fact(
    session: AsyncSession, *, tenant_id: TenantId, fact_id: uuid.UUID, approved_by: UserId
) -> None:
    """The candidate says this is true about them.

    ``approved_by`` is required and recorded. Approval is the moment a draft becomes
    something ARIA will put in front of an employer, so it has a name attached.
    """
    status, version = await _require(session, fact_id, "approve")
    await session.execute(
        update(Fact)
        .where(Fact.id == fact_id)
        .values(
            status=ApprovalStatus.APPROVED.value,
            approved_at=utc_now(),
            approved_by=approved_by,
        )
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        fact_id=fact_id,
        action=AuditAction.FACT_APPROVED,
        actor_id=str(approved_by),
        payload={"from": status.value, "version": version},
    )


async def edit_fact(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    fact_id: uuid.UUID,
    text: str,
    edited_by: UserId,
    approve: bool = True,
) -> int:
    """Replace a fact's text with a new revision. Returns the new version number.

    The previous revision stays, so a resume built from it still explains. The new
    text is approved by default, because editing it is the candidate writing the
    sentence themselves; pass ``approve=False`` to leave it a draft.
    """
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("a fact's text cannot be blank; reject it instead")

    status, version = await _require(session, fact_id, "edit")
    next_version = version + 1

    await session.execute(
        insert(FactRevision).values(
            fact_id=fact_id,
            version=next_version,
            tenant_id=tenant_id,
            text=cleaned,
            source=GraphSource.USER_EDIT.value,
            created_by=edited_by,
        )
    )
    new_status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DRAFT
    await session.execute(
        update(Fact)
        .where(Fact.id == fact_id)
        .values(
            current_version=next_version,
            status=new_status.value,
            source=GraphSource.USER_EDIT.value,
            approved_at=utc_now() if new_status is ApprovalStatus.APPROVED else None,
            approved_by=edited_by if new_status is ApprovalStatus.APPROVED else None,
        )
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        fact_id=fact_id,
        action=AuditAction.FACT_EDITED,
        actor_id=str(edited_by),
        payload={
            "from": status.value,
            "to": new_status.value,
            "from_version": version,
            "to_version": next_version,
        },
    )
    return next_version


async def reject_fact(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    fact_id: uuid.UUID,
    rejected_by: UserId,
    reason: str = "",
) -> None:
    """The candidate says this is not right. Kept, so an import does not re-propose it."""
    status, version = await _require(session, fact_id, "reject")
    await session.execute(
        update(Fact)
        .where(Fact.id == fact_id)
        .values(status=ApprovalStatus.REJECTED.value, approved_at=None, approved_by=None)
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        fact_id=fact_id,
        action=AuditAction.FACT_REJECTED,
        actor_id=str(rejected_by),
        payload={"from": status.value, "version": version, "reason": reason},
    )


async def reconsider_fact(
    session: AsyncSession, *, tenant_id: TenantId, fact_id: uuid.UUID, actor: UserId
) -> None:
    """Move a rejected fact back to draft, so it can be edited and approved."""
    status, version = await _require(session, fact_id, "reconsider")
    await session.execute(update(Fact).where(Fact.id == fact_id).values(status=ApprovalStatus.DRAFT.value))
    await _audit(
        session,
        tenant_id=tenant_id,
        fact_id=fact_id,
        action=AuditAction.FACT_RECONSIDERED,
        actor_id=str(actor),
        payload={"from": status.value, "version": version},
    )


async def merge_facts(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    keep_fact_id: uuid.UUID,
    merge_fact_id: uuid.UUID,
    actor: UserId,
) -> None:
    """Fold one fact into another: the same achievement, told twice.

    The merged fact is **rejected and left in place**, pointing at the survivor
    through ``merged_into_fact_id``. Nothing is deleted, for two reasons: a resume
    built from it earlier still has to explain itself (RESUME_ENGINE §1.10), and an
    import must not re-propose a wording the candidate has already set aside.

    Both facts must be the same candidate's, which is checked rather than assumed —
    a merge is the one operation here that names two rows, and crossing candidates
    would move an achievement onto the wrong person.
    """
    if keep_fact_id == merge_fact_id:
        raise ValueError("a fact cannot be merged into itself")

    rows = (
        await session.execute(
            select(Fact.id, Fact.candidate_id).where(Fact.id.in_([keep_fact_id, merge_fact_id]))
        )
    ).all()
    owners = {row[0]: row[1] for row in rows}
    if len(owners) != 2:
        missing = {keep_fact_id, merge_fact_id} - set(owners)
        raise FactNotFound(str(next(iter(missing))))
    if owners[keep_fact_id] != owners[merge_fact_id]:
        raise ValueError("facts belonging to different candidates cannot be merged")

    status, version = await _require(session, merge_fact_id, "reject")
    await session.execute(
        update(Fact)
        .where(Fact.id == merge_fact_id)
        .values(
            status=ApprovalStatus.REJECTED.value,
            approved_at=None,
            approved_by=None,
            merged_into_fact_id=keep_fact_id,
        )
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        fact_id=merge_fact_id,
        action=AuditAction.FACT_MERGED,
        actor_id=str(actor),
        payload={"from": status.value, "version": version, "merged_into": str(keep_fact_id)},
    )


async def approved_skill_usage(
    session: AsyncSession, candidate_id: uuid.UUID
) -> list[tuple[str, uuid.UUID | None, uuid.UUID | None]]:
    """``(skill_canonical, experience_id, project_id)`` for every approved fact's skills.

    Lives here rather than in :mod:`aria_core.graph.derive` so the rule that only this
    module queries the fact tables holds with no exceptions — a rule with one carve-out
    is a rule the next reader will add a second one to. Derived values need the anchor
    ids in bulk, which is why this returns tuples rather than FactView objects.
    """
    rows = (
        await session.execute(
            select(FactSkill.skill_canonical, Fact.experience_id, Fact.project_id)
            .join(Fact, Fact.id == FactSkill.fact_id)
            .where(
                Fact.candidate_id == candidate_id,
                Fact.status == ApprovalStatus.APPROVED.value,
            )
        )
    ).all()
    return [(row[0], row[1], row[2]) for row in rows]


async def revision_history(session: AsyncSession, fact_id: uuid.UUID) -> list[tuple[int, str]]:
    """Every version of a fact's text, oldest first. Nothing here is ever rewritten."""
    rows = (
        await session.execute(
            select(FactRevision.version, FactRevision.text)
            .where(FactRevision.fact_id == fact_id)
            .order_by(FactRevision.version)
        )
    ).all()
    return [(row[0], row[1]) for row in rows]
