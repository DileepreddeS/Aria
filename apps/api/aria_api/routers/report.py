"""What "graph complete" means, as a set of checks that pass or fail.

The founder asked for a completeness report that defines complete **measurably**. A
definition nobody can check is not a gate, and "the graph looks good" is how a product
ends up applying for jobs with half a resume.

The definition, and why each line is in it:

================================  =============================================
At least one approved experience  Nothing can be tailored from an empty history.
At least five approved facts      A resume needs evidence to choose between. One
                                  fact means every application shows the same
                                  bullet.
Approved name, email and phone    An application cannot be submitted without
                                  them, and ARIA will not invent them.
A city and region                 Location matching, and most forms ask.
At least one education entry      Nearly every new-grad form requires it.
At least eight approved skills    Below this, keyword matching has nothing to
                                  work with.
No open questions                 An open question is something ARIA refused to
                                  guess. The policy engine already blocks
                                  submission while one is open, so a graph with
                                  open questions is not ready by definition.
No uploads awaiting review        A page the extractors disagreed about may have
                                  imported text in the wrong order.
================================  =============================================

The thresholds are judgement calls and they are written here, in one place, so they
can be argued with. What is not a judgement call is that they are *checked* rather
than felt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aria_api.deps import CallerDep, SessionDep
from aria_api.schemas import CompletenessResponse
from aria_core.db.models import DeclaredSkill, Education, Experience, ProfileField, Project, Upload
from aria_core.graph.facts import approved_facts
from aria_core.graph.questions import open_questions
from aria_core.graph.status import ApprovalStatus, ProfileFieldKind

__all__ = ["MINIMUMS", "router"]

router = APIRouter(prefix="/report", tags=["report"])

#: The thresholds, in one place so they can be argued with.
MINIMUMS = {
    "experiences": 1,
    "facts": 5,
    "educations": 1,
    "skills": 8,
}

#: Contact fields an application cannot be submitted without. ARIA will not invent
#: any of them, so a missing one is a hard stop rather than a warning.
REQUIRED_PROFILE_FIELDS = (
    ProfileFieldKind.FULL_NAME,
    ProfileFieldKind.EMAIL,
    ProfileFieldKind.PHONE,
    ProfileFieldKind.LOCATION_CITY,
    ProfileFieldKind.LOCATION_REGION,
)


@dataclass(frozen=True, slots=True)
class Check:
    """One requirement, and what is missing from it."""

    name: str
    met: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "met": self.met, "detail": self.detail}


async def _approved_count(session: AsyncSession, model: Any, candidate_id: uuid.UUID) -> int:
    """How many of one kind of row the candidate has approved.

    ``model`` is deliberately loose: the four graph tables counted here share the
    two columns this needs and have no common base beyond Base, so narrowing the
    type would mean a Protocol describing SQLAlchemy columns for no benefit.
    """
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(
                    model.candidate_id == candidate_id,
                    model.status == ApprovalStatus.APPROVED.value,
                )
            )
        ).scalar_one()
    )


@router.get("/completeness", response_model=CompletenessResponse)
async def completeness(caller: CallerDep, session: SessionDep) -> CompletenessResponse:
    """Whether the graph is ready to build resumes from, and what is missing."""
    facts = await approved_facts(session, caller.candidate_id)
    experiences = await _approved_count(session, Experience, caller.candidate_id)
    educations = await _approved_count(session, Education, caller.candidate_id)
    projects = await _approved_count(session, Project, caller.candidate_id)
    skills = await _approved_count(session, DeclaredSkill, caller.candidate_id)

    approved_fields = {
        row[0]
        for row in (
            await session.execute(
                select(ProfileField.kind).where(
                    ProfileField.candidate_id == caller.candidate_id,
                    ProfileField.status == ApprovalStatus.APPROVED.value,
                )
            )
        ).all()
    }
    missing_fields = [kind.value for kind in REQUIRED_PROFILE_FIELDS if kind.value not in approved_fields]

    waiting = await open_questions(session, caller.candidate_id)
    needs_review = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Upload)
                .where(Upload.candidate_id == caller.candidate_id, Upload.needs_review.is_(True))
            )
        ).scalar_one()
    )

    checks = [
        Check(
            "approved_experience",
            experiences >= MINIMUMS["experiences"],
            f"{experiences} approved role(s); need {MINIMUMS['experiences']}",
        ),
        Check(
            "approved_facts",
            len(facts) >= MINIMUMS["facts"],
            f"{len(facts)} approved fact(s); need {MINIMUMS['facts']} so a resume has "
            "evidence to choose between",
        ),
        Check(
            "contact_details",
            not missing_fields,
            "all present" if not missing_fields else f"missing: {', '.join(missing_fields)}",
        ),
        Check(
            "education",
            educations >= MINIMUMS["educations"],
            f"{educations} approved education entr(y/ies); need {MINIMUMS['educations']}",
        ),
        Check(
            "skills",
            skills >= MINIMUMS["skills"],
            f"{skills} approved skill(s); need {MINIMUMS['skills']} for keyword matching",
        ),
        Check(
            "no_open_questions",
            not waiting,
            "none open"
            if not waiting
            else f"{len(waiting)} open: " + "; ".join(q.prompt for q in waiting[:3]),
        ),
        Check(
            "no_uploads_awaiting_review",
            needs_review == 0,
            "none"
            if needs_review == 0
            else f"{needs_review} upload(s) had pages the two PDF readers disagreed about",
        ),
    ]

    counts = {
        "approved_experiences": experiences,
        "approved_educations": educations,
        "approved_projects": projects,
        "approved_facts": len(facts),
        "approved_skills": skills,
        "approved_profile_fields": len(approved_fields),
        "open_questions": len(waiting),
        "uploads_needing_review": needs_review,
    }

    complete = all(check.met for check in checks)
    summary = [
        "graph complete" if complete else "graph not complete yet",
        *(f"  {'ok  ' if check.met else 'need'} {check.name}: {check.detail}" for check in checks),
    ]

    return CompletenessResponse(
        complete=complete,
        checks=[check.as_dict() for check in checks],
        counts=counts,
        summary=summary,
    )
