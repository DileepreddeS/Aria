"""Approving the rest of the graph: roles, degrees, projects, skills, motivations.

:mod:`aria_core.graph.facts` owns facts, and for good reasons — a fact's text is
immutable, revisions matter, and the architecture tests enforce that nothing else
reads the table. The other seven graph tables are simpler: a row is proposed, and the
candidate accepts it, turns it down, or reconsiders. They share a status column and
nothing else, so they share this module.

They do **not** share the facts lifecycle by coincidence. It is the same one, named
once here and checked the same way, because "approved" has to mean the same thing
everywhere: a person said this is true about them, and their name is on it.

The gap this closed was embarrassing and worth recording. The completeness report
requires approved roles, education, skills and contact details, and until this existed
there was no way to approve any of them — so the Phase 1 gate, "founder's graph
complete", was unreachable. A report that cannot be satisfied is worse than no report:
it teaches you to ignore it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.audit.log import append_event
from aria_core.db.models import (
    Achievement,
    DeclaredSkill,
    Education,
    Experience,
    Motivation,
    ProfileField,
    Project,
)
from aria_core.graph.facts import ALLOWED_ACTIONS
from aria_core.graph.status import ApprovalStatus
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    SubjectKind,
)
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId, UserId

__all__ = [
    "EntityKind",
    "EntityNotFound",
    "EntityView",
    "IllegalEntityTransition",
    "approve_entity",
    "entities",
    "get_entity",
    "reconsider_entity",
    "reject_entity",
]


class EntityKind(StrEnum):
    """The graph rows that are approved rather than written.

    Facts are deliberately absent: they have their own module, their own immutable
    revisions, and an architecture test that fails if anything else touches them.
    """

    EXPERIENCE = "experience"
    PROJECT = "project"
    EDUCATION = "education"
    PROFILE_FIELD = "profile_field"
    DECLARED_SKILL = "declared_skill"
    ACHIEVEMENT = "achievement"
    MOTIVATION = "motivation"


class EntityNotFound(Exception):
    """No such row in this tenant."""


class IllegalEntityTransition(Exception):
    """The lifecycle does not allow this change."""

    def __init__(self, current: ApprovalStatus, action: str) -> None:
        super().__init__(f"cannot {action} a row that is {current.value}")
        self.current = current
        self.action = action


#: The model behind each kind, and how to describe one in a line. The description is
#: what the approval view shows: a row id with no text is not something anybody can
#: decide about.
_MODELS: Final[dict[EntityKind, Any]] = {
    EntityKind.EXPERIENCE: Experience,
    EntityKind.PROJECT: Project,
    EntityKind.EDUCATION: Education,
    EntityKind.PROFILE_FIELD: ProfileField,
    EntityKind.DECLARED_SKILL: DeclaredSkill,
    EntityKind.ACHIEVEMENT: Achievement,
    EntityKind.MOTIVATION: Motivation,
}


@dataclass(frozen=True, slots=True)
class EntityView:
    """One row, with enough of it to decide on."""

    entity_id: uuid.UUID
    kind: EntityKind
    status: ApprovalStatus
    #: What this row says, in one line, for the approval view.
    summary: str
    source: str
    source_line: int | None = None
    source_upload_id: uuid.UUID | None = None
    approved_at: dt.datetime | None = None
    approved_by: uuid.UUID | None = None


def _summarise(kind: EntityKind, row: Any) -> str:
    """One line describing a row, in the candidate's own words where there are any."""
    if kind is EntityKind.EXPERIENCE:
        when = f"{row.start_month:%b %Y} - " + (f"{row.end_month:%b %Y}" if row.end_month else "present")
        client = f" (client: {row.client})" if row.client else ""
        return f"{row.title} at {row.employer}{client}, {when} [{row.kind}]"
    if kind is EntityKind.PROJECT:
        return f"{row.name} [{row.kind}]"
    if kind is EntityKind.EDUCATION:
        extra = f", GPA {row.gpa}" if row.gpa else ""
        return f"{row.degree} at {row.school}{extra}"
    if kind is EntityKind.PROFILE_FIELD:
        return f"{row.kind.replace('_', ' ')}: {row.value}"
    if kind is EntityKind.DECLARED_SKILL:
        return f"{row.surface_form} ({row.skill_canonical})"
    if kind is EntityKind.ACHIEVEMENT:
        return f"{row.title} [{row.kind}]"
    return f"{row.kind.replace('_', ' ')}: {row.text}"


def _query(kind: EntityKind, candidate_id: uuid.UUID) -> Select[*tuple[Any, ...]]:
    model = _MODELS[kind]
    return select(model).where(model.candidate_id == candidate_id).order_by(model.created_at)


async def entities(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    kind: EntityKind,
    *,
    status: ApprovalStatus | None = None,
) -> list[EntityView]:
    """Rows of one kind, oldest first, optionally filtered by status."""
    statement = _query(kind, candidate_id)
    if status is not None:
        statement = statement.where(_MODELS[kind].status == status.value)
    rows = (await session.execute(statement)).scalars().all()
    return [
        EntityView(
            entity_id=row.id,
            kind=kind,
            status=ApprovalStatus(row.status),
            summary=_summarise(kind, row),
            source=row.source,
            source_line=getattr(row, "source_line", None),
            source_upload_id=getattr(row, "source_upload_id", None),
            approved_at=row.approved_at,
            approved_by=row.approved_by,
        )
        for row in rows
    ]


async def get_entity(session: AsyncSession, kind: EntityKind, entity_id: uuid.UUID) -> EntityView:
    model = _MODELS[kind]
    row = (await session.execute(select(model).where(model.id == entity_id))).scalar_one_or_none()
    if row is None:
        raise EntityNotFound(f"{kind.value} {entity_id}")
    return EntityView(
        entity_id=row.id,
        kind=kind,
        status=ApprovalStatus(row.status),
        summary=_summarise(kind, row),
        source=row.source,
        source_line=getattr(row, "source_line", None),
        source_upload_id=getattr(row, "source_upload_id", None),
        approved_at=row.approved_at,
        approved_by=row.approved_by,
    )


async def _require(
    session: AsyncSession, kind: EntityKind, entity_id: uuid.UUID, action: str
) -> ApprovalStatus:
    """Lock the row, check the action is allowed, return its status.

    The permitted transitions are :data:`aria_core.graph.facts.ALLOWED_ACTIONS` —
    the same table, not a copy of it. "Approved" meaning something different here
    than it does for a fact is exactly the kind of drift that makes a lifecycle
    decorative.
    """
    model = _MODELS[kind]
    row = (await session.execute(select(model.status).where(model.id == entity_id).with_for_update())).first()
    if row is None:
        raise EntityNotFound(f"{kind.value} {entity_id}")
    status = ApprovalStatus(row[0])
    if action not in ALLOWED_ACTIONS[status]:
        raise IllegalEntityTransition(status, action)
    return status


async def _audit(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    kind: EntityKind,
    entity_id: uuid.UUID,
    action: AuditAction,
    actor_id: str,
    payload: dict[str, object],
    reason: str = "",
) -> None:
    await append_event(
        session,
        AuditEventDraft(
            action=action,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            # There is no SubjectKind per graph table, and inventing seven would make
            # the audit vocabulary mirror the schema. The kind is in the payload.
            subject_kind=SubjectKind.CANDIDATE,
            subject_id=str(entity_id),
            outcome=AuditOutcome.OK,
            reason=reason,
            payload={"entity_kind": kind.value, **payload},
        ),
        tenant_id=tenant_id,
    )


async def approve_entity(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    kind: EntityKind,
    entity_id: uuid.UUID,
    approved_by: UserId,
) -> EntityView:
    """The candidate says this row is right. Their name goes on it."""
    status = await _require(session, kind, entity_id, "approve")
    model = _MODELS[kind]
    await session.execute(
        update(model)
        .where(model.id == entity_id)
        .values(
            status=ApprovalStatus.APPROVED.value,
            approved_at=utc_now(),
            approved_by=approved_by,
        )
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        kind=kind,
        entity_id=entity_id,
        action=AuditAction.GRAPH_ENTITY_APPROVED,
        actor_id=str(approved_by),
        payload={"from": status.value},
    )
    return await get_entity(session, kind, entity_id)


async def reject_entity(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    kind: EntityKind,
    entity_id: uuid.UUID,
    rejected_by: UserId,
    reason: str = "",
) -> EntityView:
    """Not right. Kept, like a rejected fact, so an import does not re-propose it."""
    status = await _require(session, kind, entity_id, "reject")
    model = _MODELS[kind]
    await session.execute(
        update(model)
        .where(model.id == entity_id)
        .values(status=ApprovalStatus.REJECTED.value, approved_at=None, approved_by=None)
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        kind=kind,
        entity_id=entity_id,
        action=AuditAction.GRAPH_ENTITY_REJECTED,
        actor_id=str(rejected_by),
        payload={"from": status.value, "reason_length": len(reason)},
        reason=reason[:500],
    )
    return await get_entity(session, kind, entity_id)


async def reconsider_entity(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    kind: EntityKind,
    entity_id: uuid.UUID,
    actor: UserId,
) -> EntityView:
    """Move a rejected row back to draft."""
    status = await _require(session, kind, entity_id, "reconsider")
    model = _MODELS[kind]
    await session.execute(
        update(model).where(model.id == entity_id).values(status=ApprovalStatus.DRAFT.value)
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        kind=kind,
        entity_id=entity_id,
        action=AuditAction.GRAPH_ENTITY_RECONSIDERED,
        actor_id=str(actor),
        payload={"from": status.value},
    )
    return await get_entity(session, kind, entity_id)
