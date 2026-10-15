"""Writing an :class:`~aria_core.importer.plan.ImportPlan` into the graph as drafts.

The only module in the importer that touches a database. Everything it writes is a
draft: it calls :func:`aria_core.graph.facts.create_draft_fact`, which has no
parameter for creating an approved fact, so "an import cannot verify its own guesses"
holds by construction rather than by review.

Three rules it enforces, each of which exists because the alternative produces a
claim the candidate never made:

* **A role with no months is not written.** The ``experiences`` table requires a start
  month and ARIA will not invent one, so an undated role comes back in
  :attr:`ImportOutcome.held_back` with its question, and its bullets wait with it.
  A fact detached from the role it belongs to is worse than one that arrives late:
  approved on its own, it has no dates and no employer behind it.
* **A wording the candidate has rejected is never proposed again.** Rejected facts are
  kept precisely so an import can check against them.
* **A role already in the graph is reused, not duplicated.** Same employer, same
  months, same job — importing a second resume adds its bullets to the role that is
  already there instead of doubling the calendar. Degrees, projects and achievements
  follow the same rule (school, degree and months; name and months; title and
  issuer), so importing the same resume twice changes nothing.

Near-duplicate *facts* are reported, never merged automatically: which wording to keep
is the candidate's call. :func:`aria_core.graph.facts.merge_facts` applies their answer.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.audit.log import append_event
from aria_core.db.models import (
    Achievement,
    DeclaredSkill,
    Education,
    Experience,
    ProfileField,
    Project,
)
from aria_core.graph.facts import create_draft_fact, facts_for_deduplication
from aria_core.graph.months import MonthSpan
from aria_core.graph.questions import ask
from aria_core.graph.status import ApprovalStatus
from aria_core.importer.dedupe import (
    DuplicateFact,
    achievement_key,
    education_key,
    find_duplicate_fact,
    match_experience,
    project_key,
)
from aria_core.importer.plan import (
    ImportPlan,
    ImportQuestion,
    ProposedExperience,
    ProposedFact,
    QuestionKind,
)
from aria_core.importer.shape import IntakeVerdict
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    SubjectKind,
)
from aria_core.schemas.identity import TenantId, new_id

__all__ = ["ImportOutcome", "MergeProposal", "apply_import_plan", "record_refused_import"]

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MergeProposal:
    """A new draft fact that looks like one already in the graph.

    Both ids are real rows: the draft was created, so the candidate can read the two
    wordings side by side and keep the better one. Answering it calls
    :func:`aria_core.graph.facts.merge_facts`.
    """

    new_fact_id: uuid.UUID
    existing_fact_id: uuid.UUID
    new_text: str
    existing_text: str
    score: float
    #: The line of this upload the new wording came from, so the question can point
    #: the candidate at it.
    source_line: int = 0

    def as_question(self) -> ImportQuestion:
        return ImportQuestion(
            kind=QuestionKind.POSSIBLE_DUPLICATE,
            prompt=f"Is {self.new_text!r} the same achievement as {self.existing_text!r}?",
            source_lines=(self.source_line,) if self.source_line else (),
            detail=(
                f"They are {self.score:.0%} alike. If they are the same thing, keep the "
                "wording you prefer and I will merge the other into it."
            ),
        )


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """What an import actually wrote, and what it did not."""

    upload_id: uuid.UUID | None
    experience_ids: tuple[uuid.UUID, ...] = ()
    reused_experience_ids: tuple[uuid.UUID, ...] = ()
    education_ids: tuple[uuid.UUID, ...] = ()
    reused_education_ids: tuple[uuid.UUID, ...] = ()
    project_ids: tuple[uuid.UUID, ...] = ()
    reused_project_ids: tuple[uuid.UUID, ...] = ()
    fact_ids: tuple[uuid.UUID, ...] = ()
    profile_field_ids: tuple[uuid.UUID, ...] = ()
    declared_skill_ids: tuple[uuid.UUID, ...] = ()
    achievement_ids: tuple[uuid.UUID, ...] = ()
    reused_achievement_ids: tuple[uuid.UUID, ...] = ()
    #: Entries that could not be written, with the question that would unblock them.
    held_back: tuple[tuple[str, ImportQuestion], ...] = ()
    #: Facts identical to one already present. Skipped silently — there is nothing to
    #: decide about a sentence the graph already has.
    duplicates_skipped: int = 0
    #: Facts matching a wording the candidate rejected. Not proposed again.
    rejected_again: int = 0
    merge_proposals: tuple[MergeProposal, ...] = ()
    questions: tuple[ImportQuestion, ...] = field(default=())
    #: Questions written to the queue. Fewer than ``questions`` when the candidate
    #: has already been asked: a re-import repeats itself, the queue must not.
    question_ids: tuple[uuid.UUID, ...] = ()

    @property
    def facts_created(self) -> int:
        return len(self.fact_ids)

    def counts(self) -> dict[str, int]:
        """Shape only, for the audit payload. No imported text (SECURITY.md §3)."""
        return {
            "experiences_created": len(self.experience_ids),
            "experiences_reused": len(self.reused_experience_ids),
            "educations": len(self.education_ids),
            "educations_reused": len(self.reused_education_ids),
            "projects": len(self.project_ids),
            "projects_reused": len(self.reused_project_ids),
            "facts": len(self.fact_ids),
            "profile_fields": len(self.profile_field_ids),
            "declared_skills": len(self.declared_skill_ids),
            "achievements": len(self.achievement_ids),
            "achievements_reused": len(self.reused_achievement_ids),
            "held_back": len(self.held_back),
            "duplicates_skipped": self.duplicates_skipped,
            "rejected_again": self.rejected_again,
            "merge_proposals": len(self.merge_proposals),
            "questions": len(self.questions),
            "questions_queued": len(self.question_ids),
        }

    def summary_lines(self) -> list[str]:
        lines = [
            f"{len(self.fact_ids)} draft fact(s), "
            f"{len(self.experience_ids)} new role(s), "
            f"{len(self.reused_experience_ids)} role(s) already known, "
            f"{len(self.education_ids)} education entr(y/ies), "
            f"{len(self.project_ids)} project(s)",
        ]
        known = (
            len(self.reused_education_ids) + len(self.reused_project_ids) + len(self.reused_achievement_ids)
        )
        if known:
            lines.append(
                f"  {len(self.reused_education_ids)} education entr(y/ies), "
                f"{len(self.reused_project_ids)} project(s) and {len(self.reused_achievement_ids)} "
                "achievement(s) were already known"
            )
        if self.duplicates_skipped:
            lines.append(f"  {self.duplicates_skipped} bullet(s) were already in your graph")
        if self.rejected_again:
            lines.append(
                f"  {self.rejected_again} bullet(s) matched something you rejected before, so I left them out"
            )
        for label, question in self.held_back:
            lines.append(f"  held back: {label} \u2014 {question.prompt}")
        for proposal in self.merge_proposals:
            lines.append(f"  possible duplicate ({proposal.score:.0%}): {proposal.new_text[:60]!r}")
        for question in self.questions:
            lines.append(f"  ? {question}")
        already = len(self.questions) - len(self.question_ids)
        if already > 0:
            lines.append(f"  ({already} of those you have already been asked)")
        return lines


def _dedupe_key(question: ImportQuestion, label: str) -> str:
    """The identity of a question, independent of which upload raised it.

    Built from the kind and what it is about — an employer and title, a heading, a
    skill list — rather than from the upload id or the line number, both of which
    change between two exports of the same resume. Without that, every re-import
    would add another copy of every question.
    """
    return f"{question.kind.value}:{' '.join(label.lower().split())}"[:200]


def _experience_payload(proposed: ProposedExperience) -> dict[str, object]:
    """Everything needed to create the role once its months are known.

    The bullets travel with it. They are held back for the same reason the role is:
    a bullet approved without its role has no employer and no dates behind it.
    """
    return {
        "employer": proposed.employer,
        "title": proposed.title,
        "client": proposed.client,
        "kind": proposed.kind.value,
        "location_city": proposed.location_city,
        "location_region": proposed.location_region,
        "date_text": proposed.date_text,
        "facts": [
            {
                "text": fact.text,
                "source_line": fact.source_line,
                "skills": [[skill.canonical, skill.surface_form] for skill in fact.skills],
                "themes": [theme.value for theme in fact.themes],
                "metrics": [
                    [metric.exact_value, metric.measures, metric.baseline] for metric in fact.metrics
                ],
            }
            for fact in proposed.facts
        ],
    }


def _dates(span: MonthSpan | None) -> tuple[dt.date | None, dt.date | None]:
    if span is None:
        return None, None
    return span.start, span.end


async def _existing_experiences(
    session: AsyncSession, candidate_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, MonthSpan | None]]:
    rows = (
        await session.execute(
            select(Experience.id, Experience.employer, Experience.start_month, Experience.end_month)
            .where(Experience.candidate_id == candidate_id)
            .order_by(Experience.created_at)
        )
    ).all()
    return [(row[0], row[1], MonthSpan(row[2], row[3]) if row[2] is not None else None) for row in rows]


async def apply_import_plan(
    session: AsyncSession,
    plan: ImportPlan,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    upload_id: uuid.UUID | None = None,
    actor_id: str | None = None,
) -> ImportOutcome:
    """Write a plan into the graph as drafts, and report what happened.

    ``upload_id`` is recorded on every row so the approval view can show the candidate
    the document and the line each draft came from. It is optional only for the notes
    importer and for tests.
    """
    existing = await facts_for_deduplication(session, candidate_id)
    live_facts = [
        (fact_id, text) for fact_id, text, status in existing if status is not ApprovalStatus.REJECTED
    ]
    rejected_facts = [
        (fact_id, text) for fact_id, text, status in existing if status is ApprovalStatus.REJECTED
    ]
    known_experiences = await _existing_experiences(session, candidate_id)

    created_facts: list[uuid.UUID] = []
    merge_proposals: list[MergeProposal] = []
    duplicates_skipped = 0
    rejected_again = 0

    async def write_fact(
        proposed: ProposedFact,
        *,
        experience_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        education_id: uuid.UUID | None = None,
    ) -> None:
        """Create one draft fact, unless the graph already settled this wording."""
        nonlocal duplicates_skipped, rejected_again

        already_rejected = find_duplicate_fact(proposed, rejected_facts)
        if already_rejected is not None:
            # The candidate has seen this sentence and said no. Proposing it again
            # with every re-import is how a review queue becomes something nobody
            # reads.
            rejected_again += 1
            return

        duplicate: DuplicateFact | None = find_duplicate_fact(proposed, live_facts)
        if duplicate is not None and duplicate.merge_without_asking:
            duplicates_skipped += 1
            return

        fact_id = await create_draft_fact(
            session,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            text=proposed.text,
            source=proposed.source,
            experience_id=experience_id,
            project_id=project_id,
            education_id=education_id,
            source_upload_id=upload_id,
            source_line=proposed.source_line,
            skills=proposed.skill_pairs,
            themes=[theme.value for theme in proposed.themes],
            metrics=proposed.metric_triples,
        )
        created_facts.append(fact_id)
        live_facts.append((fact_id, proposed.text))

        if duplicate is not None:
            merge_proposals.append(
                MergeProposal(
                    new_fact_id=fact_id,
                    existing_fact_id=duplicate.existing_fact_id,
                    new_text=proposed.text,
                    existing_text=duplicate.existing_text,
                    score=duplicate.score,
                    source_line=proposed.source_line,
                )
            )

    # ------------------------------------------------------------- profile fields
    # Keyed on (kind, value), not on kind alone. A second resume usually repeats the
    # same name and email, and re-proposing those fills the approval queue with
    # decisions already made. A *different* value for a kind is worth proposing --
    # a newer phone number is news -- so only an exact repeat is skipped.
    known_fields = {
        (row[0], row[1])
        for row in (
            await session.execute(
                select(ProfileField.kind, ProfileField.value).where(ProfileField.candidate_id == candidate_id)
            )
        ).all()
    }

    profile_field_ids: list[uuid.UUID] = []
    for proposed_field in plan.profile_fields:
        key = (proposed_field.kind.value, proposed_field.value.strip()[:500])
        if key in known_fields:
            continue
        known_fields.add(key)
        field_id = new_id()
        await session.execute(
            insert(ProfileField).values(
                id=field_id,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                kind=proposed_field.kind.value,
                value=proposed_field.value,
                status=ApprovalStatus.DRAFT.value,
                source=plan.source.value,
                source_upload_id=upload_id,
                source_line=proposed_field.source_line,
            )
        )
        profile_field_ids.append(field_id)

    # ------------------------------------------------------------------- roles
    experience_ids: list[uuid.UUID] = []
    reused_ids: list[uuid.UUID] = []
    held_back: list[tuple[str, ImportQuestion]] = []
    #: Which row each proposal produced, by proposal identity. A question about a
    #: role that exists names it; a question about a held-back one cannot.
    rows_for: dict[int, uuid.UUID] = {}

    for proposed_experience in plan.experiences:
        if proposed_experience.span is None:
            question = next(
                (q for q in proposed_experience.questions if q.kind is QuestionKind.MISSING_DATES),
                None,
            )
            held_back.append(
                (
                    proposed_experience.label() or f"the entry on line {proposed_experience.source_line}",
                    question
                    or ImportQuestion(
                        kind=QuestionKind.MISSING_DATES,
                        prompt="When did this start and end?",
                        source_lines=(proposed_experience.source_line,),
                    ),
                )
            )
            continue

        reused = match_experience(proposed_experience, known_experiences)
        if reused is not None:
            experience_id = reused
            reused_ids.append(reused)
        else:
            experience_id = new_id()
            await session.execute(
                insert(Experience).values(
                    id=experience_id,
                    tenant_id=tenant_id,
                    candidate_id=candidate_id,
                    employer=proposed_experience.employer,
                    title=proposed_experience.title,
                    client=proposed_experience.client,
                    kind=proposed_experience.kind.value,
                    location_city=proposed_experience.location_city,
                    location_region=proposed_experience.location_region,
                    start_month=proposed_experience.span.start,
                    end_month=proposed_experience.span.end,
                    status=ApprovalStatus.DRAFT.value,
                    source=plan.source.value,
                    source_upload_id=upload_id,
                    source_line=proposed_experience.source_line,
                )
            )
            experience_ids.append(experience_id)
            known_experiences.append((experience_id, proposed_experience.employer, proposed_experience.span))

        rows_for[id(proposed_experience)] = experience_id
        for proposed_fact in proposed_experience.facts:
            await write_fact(proposed_fact, experience_id=experience_id)

    # --------------------------------------------------------------- education
    education_ids: list[uuid.UUID] = []
    known_educations = {
        education_key(row[1], row[2], MonthSpan(row[3], row[4]) if row[3] is not None else None): row[0]
        for row in (
            await session.execute(
                select(
                    Education.id,
                    Education.school,
                    Education.degree,
                    Education.start_month,
                    Education.end_month,
                )
                .where(Education.candidate_id == candidate_id)
                .order_by(Education.created_at)
            )
        ).all()
    }
    reused_education_ids: list[uuid.UUID] = []
    for proposed_education in plan.educations:
        if not proposed_education.school and not proposed_education.degree:
            continue
        degree_key = education_key(
            proposed_education.school, proposed_education.degree, proposed_education.span
        )
        if degree_key in known_educations:
            reused_education_ids.append(known_educations[degree_key])
            for proposed_fact in proposed_education.facts:
                await write_fact(proposed_fact, education_id=known_educations[degree_key])
            continue
        education_id = new_id()
        known_educations[degree_key] = education_id
        start, end = _dates(proposed_education.span)
        await session.execute(
            insert(Education).values(
                id=education_id,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                school=proposed_education.school,
                degree=proposed_education.degree,
                field_of_study=proposed_education.field_of_study,
                gpa=proposed_education.gpa,
                location_city=proposed_education.location_city,
                location_region=proposed_education.location_region,
                start_month=start,
                end_month=end,
                coursework=proposed_education.coursework,
                status=ApprovalStatus.DRAFT.value,
                source=plan.source.value,
                source_upload_id=upload_id,
                source_line=proposed_education.source_line,
            )
        )
        education_ids.append(education_id)
        for proposed_fact in proposed_education.facts:
            await write_fact(proposed_fact, education_id=education_id)

    # ---------------------------------------------------------------- projects
    project_ids: list[uuid.UUID] = []
    known_projects = {
        project_key(row[1], MonthSpan(row[2], row[3]) if row[2] is not None else None): row[0]
        for row in (
            await session.execute(
                select(Project.id, Project.name, Project.start_month, Project.end_month)
                .where(Project.candidate_id == candidate_id)
                .order_by(Project.created_at)
            )
        ).all()
    }
    reused_project_ids: list[uuid.UUID] = []
    for proposed_project in plan.projects:
        if not proposed_project.name:
            continue
        project_identity = project_key(proposed_project.name, proposed_project.span)
        if project_identity in known_projects:
            reused_project_ids.append(known_projects[project_identity])
            for proposed_fact in proposed_project.facts:
                await write_fact(proposed_fact, project_id=known_projects[project_identity])
            continue
        project_id = new_id()
        known_projects[project_identity] = project_id
        start, end = _dates(proposed_project.span)
        await session.execute(
            insert(Project).values(
                id=project_id,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                name=proposed_project.name,
                kind=proposed_project.kind.value,
                url=proposed_project.url,
                repository_url=proposed_project.repository_url,
                start_month=start,
                end_month=end,
                status=ApprovalStatus.DRAFT.value,
                source=plan.source.value,
                source_upload_id=upload_id,
                source_line=proposed_project.source_line,
            )
        )
        project_ids.append(project_id)
        for proposed_fact in proposed_project.facts:
            await write_fact(proposed_fact, project_id=project_id)

    # ------------------------------------------------------------ loose facts
    for proposed_fact in plan.loose_facts:
        await write_fact(proposed_fact)

    # -------------------------------------------------------- declared skills
    declared_ids: list[uuid.UUID] = []
    known_skills = {
        row[0]
        for row in (
            await session.execute(
                select(DeclaredSkill.skill_canonical).where(DeclaredSkill.candidate_id == candidate_id)
            )
        ).all()
    }
    for skill in plan.declared_skills:
        if skill.canonical in known_skills:
            continue
        known_skills.add(skill.canonical)
        skill_id = new_id()
        await session.execute(
            insert(DeclaredSkill).values(
                id=skill_id,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                skill_canonical=skill.canonical,
                surface_form=skill.surface_form,
                status=ApprovalStatus.DRAFT.value,
                source=plan.source.value,
                source_upload_id=upload_id,
                source_line=skill.source_line,
            )
        )
        declared_ids.append(skill_id)

    # ----------------------------------------------------------- achievements
    achievement_ids: list[uuid.UUID] = []
    known_achievements = {
        achievement_key(row[1], row[2]): row[0]
        for row in (
            await session.execute(
                select(Achievement.id, Achievement.title, Achievement.issuer)
                .where(Achievement.candidate_id == candidate_id)
                .order_by(Achievement.created_at)
            )
        ).all()
    }
    reused_achievement_ids: list[uuid.UUID] = []
    for proposed_achievement in plan.achievements:
        award_key = achievement_key(proposed_achievement.title, proposed_achievement.issuer)
        if award_key in known_achievements:
            reused_achievement_ids.append(known_achievements[award_key])
            continue
        achievement_id = new_id()
        known_achievements[award_key] = achievement_id
        await session.execute(
            insert(Achievement).values(
                id=achievement_id,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                kind=proposed_achievement.kind.value,
                title=proposed_achievement.title,
                issuer=proposed_achievement.issuer,
                url=proposed_achievement.url,
                status=ApprovalStatus.DRAFT.value,
                source=plan.source.value,
                source_upload_id=upload_id,
                source_line=proposed_achievement.source_line,
            )
        )
        achievement_ids.append(achievement_id)

    questions = tuple(plan.all_questions) + tuple(proposal.as_question() for proposal in merge_proposals)

    # Write the questions to the queue. A held-back role's question carries the whole
    # proposal, so answering it can create the role; everything else carries the
    # lines that prompted it and nothing more.
    queued: list[uuid.UUID] = []
    payloads = {
        _dedupe_key(question, label): _experience_payload(proposed)
        for proposed in plan.experiences
        if not proposed.datable
        for question in proposed.questions
        if question.kind is QuestionKind.MISSING_DATES
        for label in (proposed.label() or f"line {proposed.source_line}",)
    }
    labels = {
        id(question): proposed.label() or f"line {proposed.source_line}"
        for proposed in plan.experiences
        for question in proposed.questions
    }
    subjects = {
        id(question): rows_for[id(proposed)]
        for proposed in plan.experiences
        if id(proposed) in rows_for
        for question in proposed.questions
    }
    # A possible-duplicate question is about the new draft fact it created.
    subjects.update({id(proposal.as_question()): proposal.new_fact_id for proposal in merge_proposals})

    for question in questions:
        label = labels.get(id(question)) or question.prompt
        dedupe = _dedupe_key(question, label)
        subject_id = subjects.get(id(question))
        question_id = await ask(
            session,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            kind=question.kind.value,
            prompt=question.prompt,
            detail=question.detail,
            dedupe_key=dedupe,
            source_upload_id=upload_id,
            source_lines=question.source_lines,
            payload=payloads.get(dedupe),
            subject_kind="experience" if subject_id is not None else None,
            subject_id=subject_id,
        )
        if question_id is not None:
            queued.append(question_id)

    outcome = ImportOutcome(
        upload_id=upload_id,
        experience_ids=tuple(experience_ids),
        reused_experience_ids=tuple(reused_ids),
        reused_education_ids=tuple(reused_education_ids),
        reused_project_ids=tuple(reused_project_ids),
        reused_achievement_ids=tuple(reused_achievement_ids),
        education_ids=tuple(education_ids),
        project_ids=tuple(project_ids),
        fact_ids=tuple(created_facts),
        profile_field_ids=tuple(profile_field_ids),
        declared_skill_ids=tuple(declared_ids),
        achievement_ids=tuple(achievement_ids),
        held_back=tuple(held_back),
        duplicates_skipped=duplicates_skipped,
        rejected_again=rejected_again,
        merge_proposals=tuple(merge_proposals),
        questions=questions,
        question_ids=tuple(queued),
    )

    await append_event(
        session,
        AuditEventDraft(
            action=AuditAction.GRAPH_IMPORTED,
            actor_kind=ActorKind.USER if actor_id else ActorKind.SYSTEM,
            actor_id=actor_id or "importer",
            subject_kind=SubjectKind.UPLOAD if upload_id else SubjectKind.CANDIDATE,
            subject_id=str(upload_id or candidate_id),
            outcome=AuditOutcome.OK,
            reason=f"imported as drafts from {plan.source.value}",
            payload={"source": plan.source.value, **plan.counts(), **outcome.counts()},
        ),
        tenant_id=tenant_id,
    )
    logger.info(
        "graph_imported",
        source=plan.source.value,
        **{key: value for key, value in outcome.counts().items() if value},
    )
    return outcome


async def record_refused_import(
    session: AsyncSession,
    verdict: IntakeVerdict,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    upload_id: uuid.UUID | None = None,
    actor_id: str | None = None,
) -> None:
    """Audit an import a §5.5 gate refused. Nothing is written to the graph.

    The payload holds gate codes and counts only. The reasons are sentences for the
    candidate and the dropped entries quote their resume, so neither goes in the log.
    """
    await append_event(
        session,
        AuditEventDraft(
            action=AuditAction.GRAPH_IMPORT_REFUSED,
            actor_kind=ActorKind.USER if actor_id else ActorKind.SYSTEM,
            actor_id=actor_id or "importer",
            subject_kind=SubjectKind.UPLOAD if upload_id else SubjectKind.CANDIDATE,
            subject_id=str(upload_id or candidate_id),
            outcome=AuditOutcome.DENIED,
            reason="refused by " + ", ".join(gate.value for gate in verdict.gates),
            payload={
                "source": verdict.plan.source.value,
                "gates": ",".join(gate.value for gate in verdict.gates),
                "entries_dropped": len(verdict.dropped_entries),
                "bullets_dropped": len(verdict.dropped_bullets),
                **verdict.plan.counts(),
            },
        ),
        tenant_id=tenant_id,
    )
    logger.info("graph_import_refused", gates=[gate.value for gate in verdict.gates])
