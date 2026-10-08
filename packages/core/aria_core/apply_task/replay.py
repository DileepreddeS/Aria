"""Single use enforcement for an ApplyTask (SECURITY.md §8).

A valid signature and an unexpired window are not enough. Inside its few minutes, a
task that leaked — from a log, a crash dump, a compromised device — could be
presented twice, and the second time would submit the application again. So a task
is consumed exactly once.

The mechanism is a primary key, not a check-then-insert: ``consume_task`` inserts
the ``jti`` and lets the database refuse a duplicate. Two concurrent attempts
therefore cannot both pass, which a "have I seen this?" query followed by an insert
would allow.

Spent ids are kept until the task would have expired anyway, after which the
signature check refuses it regardless.
"""

from __future__ import annotations

from sqlalchemy import delete, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.audit.log import append_event
from aria_core.db.models import ConsumedApplyTaskJti
from aria_core.schemas.apply_task import ApplyTask
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    SubjectKind,
)
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId

__all__ = ["TaskReplayed", "consume_task", "prune_consumed_tasks", "record_rejected_task"]


class TaskReplayed(Exception):
    """This task has already been used."""

    def __init__(self, task: ApplyTask) -> None:
        super().__init__(
            f"ApplyTask {task.jti} has already been consumed; a task authorises one application once"
        )
        self.task = task


async def consume_task(session: AsyncSession, task: ApplyTask, *, actor_id: str = "runner") -> None:
    """Mark a task used, or raise :class:`TaskReplayed`.

    Call this before acting on the task, in the same transaction as whatever records
    the action, so a crash cannot leave a task consumed but unused — or used but
    unconsumed.
    """
    tenant_id = TenantId(task.tenant_id)
    try:
        # A savepoint, so a duplicate does not poison the caller's transaction: the
        # audit event below has to be written on the same one.
        async with session.begin_nested():
            await session.execute(
                insert(ConsumedApplyTaskJti).values(
                    jti=task.jti,
                    tenant_id=task.tenant_id,
                    application_id=task.application_id,
                    expires_at=task.expires_at,
                )
            )
    except IntegrityError as error:
        await record_rejected_task(session, task, reason="replayed", actor_id=actor_id)
        raise TaskReplayed(task) from error

    await append_event(
        session,
        AuditEventDraft(
            action=AuditAction.APPLY_TASK_CONSUMED,
            actor_kind=ActorKind.RUNNER,
            actor_id=actor_id,
            subject_kind=SubjectKind.APPLY_TASK,
            subject_id=str(task.jti),
            outcome=AuditOutcome.OK,
            payload={
                "application_id": str(task.application_id),
                "capabilities": ",".join(sorted(capability.value for capability in task.capabilities)),
                "expires_at": task.expires_at.isoformat(),
            },
        ),
        tenant_id=tenant_id,
    )


async def record_rejected_task(
    session: AsyncSession, task: ApplyTask, *, reason: str, actor_id: str = "runner"
) -> None:
    """Record a task that was refused — replayed, expired, badly signed.

    Worth its own event: a rejected task is either a bug or an attempt, and both are
    things to notice (SECURITY.md §16 anomaly alerts).
    """
    await append_event(
        session,
        AuditEventDraft(
            action=AuditAction.APPLY_TASK_REJECTED,
            actor_kind=ActorKind.RUNNER,
            actor_id=actor_id,
            subject_kind=SubjectKind.APPLY_TASK,
            subject_id=str(task.jti),
            outcome=AuditOutcome.DENIED,
            reason=reason,
            payload={"application_id": str(task.application_id)},
        ),
        tenant_id=TenantId(task.tenant_id),
    )


async def prune_consumed_tasks(session: AsyncSession) -> int:
    """Drop spent ids whose tasks have expired. Returns how many were removed.

    Safe because an expired task is refused by the signature check anyway, so the id
    no longer has to be remembered.
    """
    result = await session.execute(
        delete(ConsumedApplyTaskJti).where(ConsumedApplyTaskJti.expires_at < utc_now())
    )
    return result.rowcount or 0  # type: ignore[attr-defined]
