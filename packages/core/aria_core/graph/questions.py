"""The question queue: what ARIA will not guess, and what happens when you answer.

CLAUDE.md: low confidence creates a ``UserQuestion``, never a default answer. This
module is where those live and where answering them takes effect.

**Answering applies the change, and the change still needs approving.** Saying "it ran
from June to August 2023" creates the held-back role as a *draft* with those months;
saying "yes, I was paid" sets the kind on a draft role. Both are then approved like
anything else, so the answer and the claim remain two separate deliberate acts. The
answer itself is recorded on the question with a name and a timestamp, because it is
the evidence behind whatever it created — the database enforces that an answered
question has all three.

**Questions are deduplicated per candidate.** Re-importing the same resume must not
ask the same thing again. A queue that grows by five with every upload is a queue
nobody reads, and that is the path that ends with an unreviewed draft on a resume.

Nothing here is deleted. An answered question keeps its answer, and a dismissed one
records that the candidate decided it was not worth answering — which is itself
something to know before asking again.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.audit.log import append_event
from aria_core.db.models import Experience, Motivation, ProfileField, UserQuestion
from aria_core.graph.facts import create_draft_fact
from aria_core.graph.months import MonthSpan
from aria_core.graph.status import (
    ApprovalStatus,
    ExperienceKind,
    GraphSource,
    MotivationKind,
    ProfileFieldKind,
)
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
    "AnswerOutcome",
    "QuestionNotFound",
    "QuestionStatus",
    "QuestionView",
    "UnansweredQuestion",
    "answer_question",
    "ask",
    "dismiss_question",
    "get_question",
    "open_question_count",
    "open_questions",
    "questions_for",
]


class QuestionStatus(StrEnum):
    OPEN = "open"
    """Waiting. The policy engine refuses to submit an application while any is open."""
    ANSWERED = "answered"
    DISMISSED = "dismissed"
    """The candidate decided it was not worth answering. Kept, so it is not re-asked."""


class QuestionNotFound(Exception):
    """No such question in this tenant."""


class UnansweredQuestion(Exception):
    """The answer given does not resolve this kind of question."""

    def __init__(self, message: str, *, expected: str = "") -> None:
        super().__init__(message)
        self.expected = expected


@dataclass(frozen=True, slots=True)
class QuestionView:
    """One question as the CLI and the approval view see it."""

    question_id: uuid.UUID
    kind: str
    prompt: str
    detail: str
    status: QuestionStatus
    source_upload_id: uuid.UUID | None
    source_lines: tuple[int, ...]
    payload: dict[str, Any]
    subject_kind: str | None
    subject_id: uuid.UUID | None
    answer: str | None
    created_at: dt.datetime

    @property
    def is_open(self) -> bool:
        return self.status is QuestionStatus.OPEN

    def summary(self) -> str:
        where = (
            f" (line{'s' if len(self.source_lines) != 1 else ''} "
            + ", ".join(str(number) for number in self.source_lines)
            + ")"
            if self.source_lines
            else ""
        )
        return f"{self.prompt}{where}"


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    """What answering a question did."""

    question_id: uuid.UUID
    #: What the answer created or changed, if anything.
    subject_kind: str | None = None
    subject_id: uuid.UUID | None = None
    #: Draft facts created alongside it — a held-back role brings its bullets.
    fact_ids: tuple[uuid.UUID, ...] = ()
    detail: str = ""

    def summary(self) -> str:
        if self.subject_id is None:
            return f"recorded: {self.detail}" if self.detail else "recorded"
        created = f"{self.subject_kind} {self.subject_id}"
        if self.fact_ids:
            created += f" with {len(self.fact_ids)} draft fact(s)"
        return f"created {created} as a draft for you to approve"


#: Answers that mean yes and no. A question whose answer decides a bucket cannot
#: accept a free-text shrug: "maybe" is not an answer, it is the question again.
_YES: Final[frozenset[str]] = frozenset({"y", "yes", "paid", "true", "1", "employed"})
_NO: Final[frozenset[str]] = frozenset({"n", "no", "unpaid", "false", "0", "volunteer"})


async def ask(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    kind: str,
    prompt: str,
    dedupe_key: str,
    detail: str = "",
    source_upload_id: uuid.UUID | None = None,
    source_lines: tuple[int, ...] = (),
    payload: dict[str, Any] | None = None,
    subject_kind: str | None = None,
    subject_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Record a question, or return ``None`` if this candidate already has it.

    Deduplication is a database constraint rather than a check-then-insert, so two
    concurrent imports cannot both decide the question is new.

    ``subject_id`` names the row the question is about, when there is one. Pass it
    whenever the thing exists: a question about a role already in the graph should
    not have to be matched back to it by employer and title later.
    """
    question_id = new_id()
    statement = (
        pg_insert(UserQuestion)
        .values(
            id=question_id,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            kind=kind,
            prompt=prompt,
            detail=detail,
            status=QuestionStatus.OPEN.value,
            source_upload_id=source_upload_id,
            source_lines=list(source_lines),
            payload=payload or {},
            subject_kind=subject_kind,
            subject_id=subject_id,
            dedupe_key=dedupe_key[:200],
        )
        .on_conflict_do_nothing(constraint="uq_user_questions_candidate_id_dedupe_key")
        .returning(UserQuestion.id)
    )
    created = (await session.execute(statement)).scalar_one_or_none()
    return created


def _to_view(row: UserQuestion) -> QuestionView:
    return QuestionView(
        question_id=row.id,
        kind=row.kind,
        prompt=row.prompt,
        detail=row.detail,
        status=QuestionStatus(row.status),
        source_upload_id=row.source_upload_id,
        source_lines=tuple(int(number) for number in row.source_lines),
        payload=dict(row.payload),
        subject_kind=row.subject_kind,
        subject_id=row.subject_id,
        answer=row.answer,
        created_at=row.created_at,
    )


async def open_questions(session: AsyncSession, candidate_id: uuid.UUID) -> list[QuestionView]:
    """Everything still waiting, oldest first."""
    rows = (
        (
            await session.execute(
                select(UserQuestion)
                .where(
                    UserQuestion.candidate_id == candidate_id,
                    UserQuestion.status == QuestionStatus.OPEN.value,
                )
                .order_by(UserQuestion.created_at, UserQuestion.id)
            )
        )
        .scalars()
        .all()
    )
    return [_to_view(row) for row in rows]


async def questions_for(
    session: AsyncSession, candidate_id: uuid.UUID, *, status: QuestionStatus | None = None
) -> list[QuestionView]:
    """Every question, or only those in one state."""
    statement = select(UserQuestion).where(UserQuestion.candidate_id == candidate_id)
    if status is not None:
        statement = statement.where(UserQuestion.status == status.value)
    rows = (
        (await session.execute(statement.order_by(UserQuestion.created_at, UserQuestion.id))).scalars().all()
    )
    return [_to_view(row) for row in rows]


async def open_question_count(session: AsyncSession, candidate_id: uuid.UUID) -> int:
    """What the policy engine reads before allowing a submission."""
    return len(await open_questions(session, candidate_id))


async def get_question(session: AsyncSession, question_id: uuid.UUID) -> QuestionView:
    row = (
        await session.execute(select(UserQuestion).where(UserQuestion.id == question_id))
    ).scalar_one_or_none()
    if row is None:
        raise QuestionNotFound(str(question_id))
    return _to_view(row)


# --------------------------------------------------------------------- answering


def _parse_month(value: str) -> dt.date | None:
    """A month from an answer, or ``None``.

    Deliberately strict, and nothing like the resume date parser: an answer is typed
    by the candidate in response to a direct question, so it is reasonable to require
    an unambiguous form rather than guess at one. ``YYYY-MM`` always, plus the words
    that mean "still there".
    """
    cleaned = value.strip().lower()
    if cleaned in {"present", "current", "now", "ongoing", ""}:
        return None
    try:
        year, month = cleaned.replace("/", "-").split("-")[:2]
        return dt.date(int(year), int(month), 1)
    except (ValueError, TypeError) as error:
        raise UnansweredQuestion(
            f"{value!r} is not a month I can read", expected="YYYY-MM, or 'present'"
        ) from error


async def _apply_missing_dates(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    question: QuestionView,
    answer: str,
) -> AnswerOutcome:
    """Create the held-back role, now that it has months.

    The answer is "2023-06 to 2023-08", or "2023-06 to present". The role arrives as
    a draft with its bullets attached, because a bullet approved without its role has
    no employer and no dates behind it.
    """
    parts = [part.strip() for part in answer.replace(chr(0x2013), "-").split(" to ")]
    if len(parts) == 1:
        parts = [part.strip() for part in parts[0].split("..")]
    if len(parts) != 2:
        raise UnansweredQuestion(
            "I need a start and an end", expected="'2023-06 to 2023-08', or '2024-06 to present'"
        )

    start = _parse_month(parts[0])
    if start is None:
        raise UnansweredQuestion("I need a start month", expected="'2023-06 to 2023-08'")
    end = _parse_month(parts[1])
    if end is not None and end < start:
        raise UnansweredQuestion(f"{parts[1]!r} is before {parts[0]!r}", expected="a later end month")

    span = MonthSpan(start, end)
    proposal = question.payload
    experience_id = new_id()
    await session.execute(
        insert(Experience).values(
            id=experience_id,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            employer=str(proposal.get("employer", ""))[:200],
            title=str(proposal.get("title", ""))[:200],
            client=(str(proposal["client"])[:200] if proposal.get("client") else None),
            kind=str(proposal.get("kind", ExperienceKind.PROFESSIONAL.value)),
            location_city=(str(proposal["location_city"]) if proposal.get("location_city") else None),
            location_region=(str(proposal["location_region"]) if proposal.get("location_region") else None),
            start_month=span.start,
            end_month=span.end,
            status=ApprovalStatus.DRAFT.value,
            # The months came from the candidate, not the document, so the row says so.
            source=GraphSource.USER_EDIT.value,
            source_upload_id=question.source_upload_id,
            source_line=(question.source_lines[0] if question.source_lines else None),
        )
    )

    fact_ids: list[uuid.UUID] = []
    for raw in proposal.get("facts", []) or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        fact_ids.append(
            await create_draft_fact(
                session,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                text=text,
                source=GraphSource.RESUME_IMPORT,
                experience_id=experience_id,
                source_upload_id=question.source_upload_id,
                source_line=raw.get("source_line"),
                skills=[(pair[0], pair[1]) for pair in raw.get("skills", []) if len(pair) == 2],
                themes=[str(theme) for theme in raw.get("themes", [])],
                metrics=[
                    (triple[0], triple[1], triple[2]) for triple in raw.get("metrics", []) if len(triple) == 3
                ],
            )
        )

    return AnswerOutcome(
        question_id=question.question_id,
        subject_kind="experience",
        subject_id=experience_id,
        fact_ids=tuple(fact_ids),
        detail=f"{span.start:%b %Y} to {'present' if span.end is None else format(span.end, '%b %Y')}",
    )


async def _apply_paid_or_unpaid(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    question: QuestionView,
    answer: str,
) -> AnswerOutcome:
    """Set the kind on a role whose bucket depends on whether it was paid.

    Paid is part-time employment and counts towards general work experience; unpaid is
    project time and does not (``docs/DECISIONS.md``). A role with no months yet is
    held by a separate dates question, so this one only has to set the kind.
    """
    cleaned = answer.strip().lower()
    if cleaned in _YES:
        kind = ExperienceKind.PART_TIME
    elif cleaned in _NO:
        kind = ExperienceKind.RESEARCH
    else:
        raise UnansweredQuestion(
            f"{answer!r} does not answer whether you were paid",
            expected="yes or no",
        )

    experience_id = question.subject_id
    if experience_id is None:
        payload = question.payload
        employer, title = str(payload.get("employer", "")), str(payload.get("title", ""))
        found = (
            await session.execute(
                select(Experience.id).where(
                    Experience.candidate_id == candidate_id,
                    Experience.employer == employer,
                    Experience.title == title,
                )
            )
        ).scalar_one_or_none()
        experience_id = found

    if experience_id is None:
        # The role is still held back on its own dates question. The answer is
        # recorded and the kind is carried on the payload, so whichever question is
        # answered second has both halves.
        return AnswerOutcome(
            question_id=question.question_id,
            detail=f"noted as {kind.value}; the role is still waiting on its dates",
        )

    await session.execute(update(Experience).where(Experience.id == experience_id).values(kind=kind.value))
    return AnswerOutcome(
        question_id=question.question_id,
        subject_kind="experience",
        subject_id=experience_id,
        detail=f"recorded as {kind.value} ({kind.bucket.value} time)",
    )


async def _apply_as_new_fact(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    question: QuestionView,
    answer: str,
) -> AnswerOutcome:
    """An enrichment answer becomes a **new draft fact**, beside the original.

    RESUME_ENGINE §5 is explicit that enrichment answers become new draft facts, and
    the alternative is worse than it looks: rewriting the bullet that was asked about
    would mean a sentence the candidate typed into a prompt silently replacing one
    they had already approved. The original is left exactly as it was.

    The answer runs through the same metric and skill extraction as an imported
    bullet, so a number in it is captured as a metric with the text that states it,
    and a skill named in it becomes evidence.
    """
    from aria_core.importer.metrics import detect_themes, extract_metrics
    from aria_core.importer.skills import tag_skills
    from aria_core.taxonomy.loader import load_default_taxonomy

    payload = question.payload
    anchor = {
        "experience_id": _as_uuid(payload.get("experience_id")),
        "project_id": _as_uuid(payload.get("project_id")),
        "education_id": _as_uuid(payload.get("education_id")),
    }

    findings = tag_skills(answer, load_default_taxonomy())
    skills = [(skill.canonical, skill.surface_form) for skill in findings.skills]
    # The skill the question was about is evidence even when the answer does not spell
    # it out again: "I used it for the event pipeline" is an answer about Kafka.
    declared = payload.get("skill_canonical")
    if isinstance(declared, str) and declared not in {pair[0] for pair in skills}:
        surface = payload.get("surface_form")
        skills.append((declared, str(surface) if surface else declared))

    fact_id = await create_draft_fact(
        session,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        text=answer,
        source=GraphSource.ENRICHMENT_INTERVIEW,
        experience_id=anchor["experience_id"],
        project_id=anchor["project_id"],
        education_id=anchor["education_id"],
        source_upload_id=question.source_upload_id,
        skills=skills,
        themes=[theme.value for theme in detect_themes(answer)],
        metrics=[
            (metric.exact_value, metric.measures, metric.baseline) for metric in extract_metrics(answer)
        ],
    )
    return AnswerOutcome(
        question_id=question.question_id,
        subject_kind="fact",
        subject_id=fact_id,
        fact_ids=(fact_id,),
        detail="added beside the original, which is unchanged",
    )


async def _apply_profile_field(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    question: QuestionView,
    answer: str,
) -> AnswerOutcome:
    """A contact detail the candidate supplied, as a draft field.

    Still a draft. A phone number is the one field where a typo costs an interview
    and nobody notices, so it gets the same second look as everything else.
    """
    kind = question.payload.get("field_kind")
    if not isinstance(kind, str):
        raise UnansweredQuestion(
            "this question does not say which field it is about", expected="a question from the interview"
        )
    if kind not in {member.value for member in ProfileFieldKind}:
        raise UnansweredQuestion(f"{kind!r} is not a profile field ARIA keeps")

    field_id = new_id()
    await session.execute(
        insert(ProfileField).values(
            id=field_id,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            kind=kind,
            value=answer[:500],
            status=ApprovalStatus.DRAFT.value,
            source=GraphSource.ENRICHMENT_INTERVIEW.value,
        )
    )
    return AnswerOutcome(
        question_id=question.question_id,
        subject_kind="profile_field",
        subject_id=field_id,
        detail=f"recorded as your {kind.replace('_', ' ')}, as a draft",
    )


async def _apply_motivation(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    question: QuestionView,
    answer: str,
) -> AnswerOutcome:
    """One of the six motivations, in the candidate's own words.

    Never inferred from anything. What somebody has done and what they want next are
    different things, and a motivation ARIA made up cannot even be checked against a
    fact — which is why this is the only way a row gets into that table.
    """
    kind = question.payload.get("motivation_kind")
    if not isinstance(kind, str) or kind not in {member.value for member in MotivationKind}:
        raise UnansweredQuestion(
            "this question does not say which motivation it is about",
            expected="a question from the motivations interview",
        )

    motivation_id = new_id()
    await session.execute(
        insert(Motivation).values(
            id=motivation_id,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            kind=kind,
            text=answer,
            status=ApprovalStatus.DRAFT.value,
            source=GraphSource.MOTIVATIONS_INTERVIEW.value,
        )
    )
    return AnswerOutcome(
        question_id=question.question_id,
        subject_kind="motivation",
        subject_id=motivation_id,
        detail=f"recorded as what you said about {kind.replace('_', ' ')}",
    )


def _as_uuid(value: object) -> uuid.UUID | None:
    """A uuid from a JSON payload, or None. The payload is data, not a promise."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


#: Which kinds change the graph when answered, and how. A kind that is not here is
#: recorded and nothing else — "what is the SECURITY CLEARANCE section?" is useful to
#: have asked and answered without the importer acting on it unprompted.
_APPLIERS: Final[dict[str, Any]] = {
    "missing_dates": _apply_missing_dates,
    "paid_or_unpaid": _apply_paid_or_unpaid,
    # The enrichment interview. All three become a new draft fact: the answer is a
    # claim about the candidate's work, so it goes where claims go.
    "missing_metric": _apply_as_new_fact,
    "missing_context": _apply_as_new_fact,
    "unevidenced_skill": _apply_as_new_fact,
    "missing_profile_field": _apply_profile_field,
    "motivation": _apply_motivation,
}


async def answer_question(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    question_id: uuid.UUID,
    answer: str,
    answered_by: UserId,
) -> AnswerOutcome:
    """Record an answer and apply whatever it unblocks.

    The question is locked for the transaction, so two answers cannot both create the
    role. Answering an already-answered question is refused rather than applied twice.
    """
    cleaned = answer.strip()
    if not cleaned:
        raise UnansweredQuestion("an answer cannot be blank", expected="anything but empty")

    row = (
        await session.execute(select(UserQuestion).where(UserQuestion.id == question_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise QuestionNotFound(str(question_id))
    if row.status != QuestionStatus.OPEN.value:
        raise UnansweredQuestion(f"this question is already {row.status}", expected="an open question")

    question = _to_view(row)
    applier = _APPLIERS.get(question.kind)
    if applier is None:
        outcome = AnswerOutcome(question_id=question_id, detail="recorded against the question")
    else:
        outcome = await applier(
            session,
            tenant_id=tenant_id,
            candidate_id=row.candidate_id,
            question=question,
            answer=cleaned,
        )

    await session.execute(
        update(UserQuestion)
        .where(UserQuestion.id == question_id)
        .values(
            status=QuestionStatus.ANSWERED.value,
            answer=cleaned,
            answered_at=utc_now(),
            answered_by=answered_by,
            subject_kind=outcome.subject_kind,
            subject_id=outcome.subject_id,
        )
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        question_id=question_id,
        action=AuditAction.USER_QUESTION_ANSWERED,
        actor_id=str(answered_by),
        payload={
            "kind": question.kind,
            "created": outcome.subject_kind or "nothing",
            "facts_created": len(outcome.fact_ids),
            # The answer itself is S1 and stays in the row, not in the audit payload.
            "answer_length": len(cleaned),
        },
    )
    return outcome


async def dismiss_question(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    question_id: uuid.UUID,
    actor: UserId,
    reason: str = "",
) -> None:
    """The candidate decided this is not worth answering. Kept, so it is not re-asked."""
    row = (
        await session.execute(
            select(UserQuestion.status).where(UserQuestion.id == question_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise QuestionNotFound(str(question_id))
    if row != QuestionStatus.OPEN.value:
        raise UnansweredQuestion(f"this question is already {row}", expected="an open question")

    await session.execute(
        update(UserQuestion)
        .where(UserQuestion.id == question_id)
        .values(status=QuestionStatus.DISMISSED.value)
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        question_id=question_id,
        action=AuditAction.USER_QUESTION_DISMISSED,
        actor_id=str(actor),
        payload={"reason_length": len(reason)},
        reason=reason[:500],
    )


async def _audit(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    question_id: uuid.UUID,
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
            subject_kind=SubjectKind.USER_QUESTION,
            subject_id=str(question_id),
            outcome=AuditOutcome.OK,
            reason=reason,
            payload=payload,
        ),
        tenant_id=tenant_id,
    )
