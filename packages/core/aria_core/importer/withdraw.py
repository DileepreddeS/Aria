"""Withdraw what a superseded upload imported (DECISIONS 2026-10-12).

When a file is read again (migration 0006), the old reading's drafts and questions
stay where they were: nothing is withdrawn on the candidate's behalf. This is the one
step that withdraws them when the candidate asks, after seeing the count.

**Deleted, not rejected.** A rejected fact blocks its wording from ever being proposed
again, and a dismissed question blocks its key; either would suppress the same, correct
bullet or question when the new reading is imported. These are proposals from a reading
the candidate discarded, never approved, so they cannot have reached a resume. They are
deleted, and one audit event records exactly what went.

**Per row, never per upload.** Only rows still in draft go, and only open questions.
Anything the candidate approved or rejected is their decision and survives, reported as
kept. A role, degree or project goes only if every fact under it goes too: deleting a
parent deletes its facts by cascade, and the new reading may have attached its own
facts, or the candidate approved one, under a role the old reading created.

Questions that go with the drafts: the old upload's open questions, open questions
about a row being deleted, and open "is this the same achievement?" questions whose
other half is a fact being deleted.

**Answers whose only effect is going.** An answered question whose answer made or
changed a row being deleted ("were you paid?" setting a role's kind, "when?" creating a
held-back role) is withdrawn with it. Left answered, its key would stop the next import
asking it again, and the answer would be lost with the row: the new reading's role
would come back without it, and nothing would ask. Answers whose row stays, and
dismissals, are the candidate's decisions and stay.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.audit.log import append_event
from aria_core.db.models import (
    Achievement,
    DeclaredSkill,
    Education,
    Experience,
    ProfileField,
    Project,
    Upload,
    UserQuestion,
)
from aria_core.graph.facts import (
    delete_draft_facts,
    draft_facts_from_upload,
    fact_ids_under,
    fact_status_counts,
)
from aria_core.graph.questions import QuestionStatus
from aria_core.graph.status import ApprovalStatus
from aria_core.importer.plan import QuestionKind
from aria_core.schemas.audit import ActorKind, AuditAction, AuditEventDraft, AuditOutcome, SubjectKind
from aria_core.schemas.identity import TenantId

__all__ = ["NotSuperseded", "UploadNotFound", "Withdrawal", "plan_withdrawal", "withdraw"]

_DRAFT = ApprovalStatus.DRAFT.value


class UploadNotFound(Exception):
    """No such upload for this candidate."""


class NotSuperseded(Exception):
    """The upload is the current reading of its file; its drafts are withdrawn one by one."""


@dataclass(frozen=True, slots=True)
class Withdrawal:
    """What withdrawing one superseded upload deletes, and what it keeps."""

    upload_id: uuid.UUID
    superseded_by: uuid.UUID
    facts: tuple[uuid.UUID, ...] = ()
    experiences: tuple[uuid.UUID, ...] = ()
    education: tuple[uuid.UUID, ...] = ()
    projects: tuple[uuid.UUID, ...] = ()
    profile_fields: tuple[uuid.UUID, ...] = ()
    declared_skills: tuple[uuid.UUID, ...] = ()
    achievements: tuple[uuid.UUID, ...] = ()
    questions: tuple[uuid.UUID, ...] = ()
    #: Answered questions whose answer's row is being deleted.
    answers: tuple[uuid.UUID, ...] = ()
    #: Rows from this upload that stay, as counts by kind and reason: "approved
    #: facts", "rejected roles", "roles still holding other facts".
    kept: dict[str, int] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "facts": len(self.facts),
            "roles": len(self.experiences),
            "education": len(self.education),
            "projects": len(self.projects),
            "questions": len(self.questions),
            "answers": len(self.answers),
            "profile_fields": len(self.profile_fields),
            "declared_skills": len(self.declared_skills),
            "achievements": len(self.achievements),
        }

    @property
    def empty(self) -> bool:
        return not any(self.counts().values())


async def _rows(
    session: AsyncSession, model: type, candidate_id: uuid.UUID, upload_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    rows = (
        await session.execute(
            select(model.id, model.status).where(  # type: ignore[attr-defined]
                model.candidate_id == candidate_id,  # type: ignore[attr-defined]
                model.source_upload_id == upload_id,  # type: ignore[attr-defined]
            )
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


async def plan_withdrawal(
    session: AsyncSession, *, candidate_id: uuid.UUID, upload_id: uuid.UUID
) -> Withdrawal:
    """Count what :func:`withdraw` would delete. Reads only."""
    upload = (
        await session.execute(select(Upload.candidate_id, Upload.superseded_by).where(Upload.id == upload_id))
    ).first()
    if upload is None or upload[0] != candidate_id:
        raise UploadNotFound(str(upload_id))
    if upload[1] is None:
        raise NotSuperseded(str(upload_id))

    kept: Counter[str] = Counter()

    def drafts(name: str, rows: list[tuple[uuid.UUID, str]]) -> list[uuid.UUID]:
        for _, status in rows:
            if status != _DRAFT:
                kept[f"{status} {name}"] += 1
        return [row_id for row_id, status in rows if status == _DRAFT]

    facts = await draft_facts_from_upload(session, candidate_id, upload_id)
    fact_ids = {fact_id for fact_id, _ in facts}
    for status, count in (await fact_status_counts(session, candidate_id, upload_id)).items():
        if status != _DRAFT:
            kept[f"{status} facts"] += count

    # Parents go only when nothing would be left under them.
    parents = {
        "roles": drafts("roles", await _rows(session, Experience, candidate_id, upload_id)),
        "education": drafts("education", await _rows(session, Education, candidate_id, upload_id)),
        "projects": drafts("projects", await _rows(session, Project, candidate_id, upload_id)),
    }
    children = await fact_ids_under(
        session,
        experience_ids=parents["roles"],
        project_ids=parents["projects"],
        education_ids=parents["education"],
    )
    holding = {parent for fact_id, parent in children if fact_id not in fact_ids}
    for name, ids in parents.items():
        kept[f"{name} still holding facts that stay"] += sum(1 for parent in ids if parent in holding)
        parents[name] = [parent for parent in ids if parent not in holding]

    profile_fields = drafts("profile fields", await _rows(session, ProfileField, candidate_id, upload_id))
    declared_skills = drafts("declared skills", await _rows(session, DeclaredSkill, candidate_id, upload_id))
    achievements = drafts("achievements", await _rows(session, Achievement, candidate_id, upload_id))

    going = fact_ids | {row for ids in parents.values() for row in ids}
    duplicate_of_going = {f" the same achievement as {text!r}?" for _, text in facts}
    open_questions = (
        await session.execute(
            select(
                UserQuestion.id,
                UserQuestion.source_upload_id,
                UserQuestion.subject_id,
                UserQuestion.kind,
                UserQuestion.prompt,
            ).where(
                UserQuestion.candidate_id == candidate_id,
                UserQuestion.status == QuestionStatus.OPEN.value,
            )
        )
    ).all()
    questions = [
        row[0]
        for row in open_questions
        if row[1] == upload_id
        or (row[2] is not None and row[2] in going)
        or (
            row[3] == QuestionKind.POSSIBLE_DUPLICATE.value
            and any(row[4].endswith(suffix) for suffix in duplicate_of_going)
        )
    ]
    deleted_rows = going | set(profile_fields)
    answers = [
        row[0]
        for row in (
            await session.execute(
                select(UserQuestion.id, UserQuestion.subject_id).where(
                    UserQuestion.candidate_id == candidate_id,
                    UserQuestion.status == QuestionStatus.ANSWERED.value,
                    UserQuestion.subject_id.is_not(None),
                )
            )
        ).all()
        if row[1] in deleted_rows
    ]
    kept_questions = (
        await session.execute(
            select(UserQuestion.status).where(
                UserQuestion.source_upload_id == upload_id,
                UserQuestion.status != QuestionStatus.OPEN.value,
                UserQuestion.id.not_in(answers) if answers else UserQuestion.id.is_not(None),
            )
        )
    ).all()
    for (status,) in kept_questions:
        kept[f"{status} questions"] += 1

    return Withdrawal(
        upload_id=upload_id,
        superseded_by=upload[1],
        facts=tuple(fact_id for fact_id, _ in facts),
        experiences=tuple(parents["roles"]),
        education=tuple(parents["education"]),
        projects=tuple(parents["projects"]),
        profile_fields=tuple(profile_fields),
        declared_skills=tuple(declared_skills),
        achievements=tuple(achievements),
        questions=tuple(questions),
        answers=tuple(answers),
        kept={reason: count for reason, count in kept.items() if count},
    )


async def withdraw(
    session: AsyncSession,
    withdrawal: Withdrawal,
    *,
    tenant_id: TenantId,
    actor_id: str,
) -> dict[str, int]:
    """Delete what ``withdrawal`` lists, still only where each row is a draft; audit it.

    Returns the counts actually deleted. A row approved since the plan was counted is
    not deleted: the status is checked again in every statement.
    """
    deleted = {
        "questions": len(
            (
                await session.execute(
                    delete(UserQuestion)
                    .where(
                        UserQuestion.id.in_(list(withdrawal.questions)),
                        UserQuestion.status == QuestionStatus.OPEN.value,
                    )
                    .returning(UserQuestion.id)
                )
            ).all()
        )
        if withdrawal.questions
        else 0,
        "answers": len(
            (
                await session.execute(
                    delete(UserQuestion)
                    .where(
                        UserQuestion.id.in_(list(withdrawal.answers)),
                        UserQuestion.status == QuestionStatus.ANSWERED.value,
                    )
                    .returning(UserQuestion.id)
                )
            ).all()
        )
        if withdrawal.answers
        else 0,
        "facts": await delete_draft_facts(session, withdrawal.facts),
    }
    for name, model, ids in (
        ("roles", Experience, withdrawal.experiences),
        ("education", Education, withdrawal.education),
        ("projects", Project, withdrawal.projects),
        ("profile_fields", ProfileField, withdrawal.profile_fields),
        ("declared_skills", DeclaredSkill, withdrawal.declared_skills),
        ("achievements", Achievement, withdrawal.achievements),
    ):
        deleted[name] = (
            len(
                (
                    await session.execute(
                        delete(model)
                        .where(model.id.in_(list(ids)), model.status == _DRAFT)
                        .returning(model.id)
                    )
                ).all()
            )
            if ids
            else 0
        )

    # Reconstructive: the rows are gone, so the event says exactly what went and from
    # where. Ids and counts only; the text was the candidate's resume (S1) and the
    # payload is S0/S1 metadata.
    await append_event(
        session,
        AuditEventDraft(
            action=AuditAction.GRAPH_WITHDRAWN,
            actor_kind=ActorKind.USER,
            actor_id=actor_id,
            subject_kind=SubjectKind.UPLOAD,
            subject_id=str(withdrawal.upload_id),
            outcome=AuditOutcome.OK,
            reason="drafts from a superseded reading withdrawn at the candidate's request",
            payload={
                "upload_id": str(withdrawal.upload_id),
                "superseded_by": str(withdrawal.superseded_by),
                **deleted,
                **{f"kept {reason}": count for reason, count in sorted(withdrawal.kept.items())},
            },
        ),
        tenant_id=tenant_id,
    )
    return deleted
