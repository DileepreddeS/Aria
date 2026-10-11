"""Derived values: computed from the graph, never typed in (RESUME_ENGINE §5).

"3 years of React" is not a field anyone fills in. It is the union of the date ranges
of the experiences whose approved facts use React, counted in whole months. That
matters because a number nobody wrote down is a number nobody can inflate.

Rules that hold throughout, each because the obvious alternative overstates:

**Only approved facts count.** The input is
:func:`aria_core.graph.facts.approved_skill_usage`. An imported bullet the candidate
has not read yet is not evidence of anything.

**The four buckets are disjoint.** Full-time (salaried and contract), part-time
(campus jobs, paid assistantships), internship, and project (academic, personal, open
source, unpaid research, volunteer). Every experience lands in exactly one, so a
summary can print all four without counting a month twice.

**"Years of experience" means full-time.** By default, nothing else. An internship is
real work that a hiring manager reads differently, a two-year assistantship at ten
hours a week is not two years of experience, and a new grad who answers "2.6 years" on
the strength of either has overstated. Part-time and internship months join the figure
only when the question asks for them, or — for internships — when the candidate has
turned the setting on. See :class:`ClaimPolicy`.

**Combining buckets unions, never adds.** A part-time campus job can run during a
research post, so adding the totals would invent months the calendar does not contain.

**Direct and implied are separate.** React implies JavaScript for *matching*
(RESUME_ENGINE §6). It does not imply years: if no approved fact mentions JavaScript,
its full-time months are zero however much React there is. The implied figure is
reported alongside, labelled, for the Evidence Matcher to weigh — never to print.

**Nothing is rounded up.** See :func:`aria_core.graph.months.months_to_years`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aria_core.db.models import Candidate, Experience, Project
from aria_core.graph.facts import approved_skill_usage
from aria_core.graph.months import MonthSpan, months_to_years, total_months, union_spans
from aria_core.graph.status import ApprovalStatus, ExperienceBucket, ExperienceKind
from aria_core.schemas.base import utc_now
from aria_core.taxonomy.model import SkillTaxonomy

__all__ = [
    "CareerTotals",
    "ClaimPolicy",
    "SkillExperience",
    "compute_career_totals",
    "compute_skill_experience",
    "load_claim_policy",
]


@dataclass(frozen=True, slots=True)
class ClaimPolicy:
    """Which buckets may be part of a "years of experience" claim.

    Full-time always. Everything else only when somebody says so out loud:

    * the question includes internships ("including internships, how many years...?")
      or asks about work experience generally or part-time work — which the Answer
      Engine determines when it classifies the question;
    * the candidate has set ``count_internships_as_experience``.

    There is deliberately no candidate setting for part-time. A setting is for a
    standing preference about how to describe oneself, and "count my campus job as
    experience" is a judgement that depends on what is being asked; leaving it
    question-driven keeps the answer tied to the question rather than to a checkbox
    set months earlier.

    A type rather than two booleans, so a call site cannot opt in without a reason,
    and the reason travels into the audit trail.
    """

    include_internships: bool = False
    include_part_time: bool = False
    reason: str = "default: full-time only"

    @property
    def buckets(self) -> frozenset[ExperienceBucket]:
        """The buckets this policy permits. Project time is never among them."""
        allowed = {ExperienceBucket.FULL_TIME}
        if self.include_internships:
            allowed.add(ExperienceBucket.INTERNSHIP)
        if self.include_part_time:
            allowed.add(ExperienceBucket.PART_TIME)
        return frozenset(allowed)

    @classmethod
    def default(cls) -> ClaimPolicy:
        return cls()

    @classmethod
    def general_work_experience(cls) -> ClaimPolicy:
        """For "how much work experience do you have?" — every kind of paid work.

        Full-time, part-time and internships, unioned. Not project work: a class
        project is not a job however broadly the question is phrased.
        """
        return cls(True, True, "the question asks about work experience generally")

    @classmethod
    def resolve(
        cls,
        *,
        question_includes_internships: bool = False,
        question_includes_part_time: bool = False,
        question_is_general_work_experience: bool = False,
        candidate_setting: bool = False,
    ) -> ClaimPolicy:
        """Combine what the question asked with the candidate's setting."""
        if question_is_general_work_experience:
            return cls.general_work_experience()

        include_internships = question_includes_internships or candidate_setting
        reasons = []
        if question_includes_internships:
            reasons.append("the question includes internships")
        if question_includes_part_time:
            reasons.append("the question includes part-time work")
        if candidate_setting and not question_includes_internships:
            reasons.append("the candidate counts internships as experience")
        elif candidate_setting:
            reasons.append("the candidate opted in")

        if not reasons:
            return cls.default()
        return cls(include_internships, question_includes_part_time, ", and ".join(reasons))


async def load_claim_policy(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    question_includes_internships: bool = False,
    question_includes_part_time: bool = False,
    question_is_general_work_experience: bool = False,
) -> ClaimPolicy:
    """The policy for one candidate, reading their setting from the graph."""
    setting = await session.scalar(
        select(Candidate.count_internships_as_experience).where(Candidate.id == candidate_id)
    )
    return ClaimPolicy.resolve(
        question_includes_internships=question_includes_internships,
        question_includes_part_time=question_includes_part_time,
        question_is_general_work_experience=question_is_general_work_experience,
        candidate_setting=bool(setting),
    )


@dataclass(frozen=True, slots=True)
class SkillExperience:
    """Evidence for one skill, split by bucket.

    Spans rather than month counts, because combining buckets has to union them: an
    internship that ran during a research post must not add its months twice. The
    spans are already closed at ``as_of``, so every length here is unambiguous.
    """

    canonical: str
    full_time_spans: tuple[MonthSpan, ...] = ()
    part_time_spans: tuple[MonthSpan, ...] = ()
    internship_spans: tuple[MonthSpan, ...] = ()
    project_spans: tuple[MonthSpan, ...] = ()
    #: Full-time spans reaching this skill only through taxonomy implication — React
    #: counting towards JavaScript. For matching, never for a printed claim.
    implied_full_time_spans: tuple[MonthSpan, ...] = ()
    #: How many approved facts name it. One is a mention; six is a theme.
    fact_count: int = 0
    as_of: dt.date = dt.date.min

    # ------------------------------------------------------------------- buckets
    @property
    def full_time_months(self) -> int:
        return total_months(list(self.full_time_spans), now=self.as_of)

    @property
    def part_time_months(self) -> int:
        return total_months(list(self.part_time_spans), now=self.as_of)

    @property
    def internship_months(self) -> int:
        return total_months(list(self.internship_spans), now=self.as_of)

    @property
    def project_months(self) -> int:
        return total_months(list(self.project_spans), now=self.as_of)

    @property
    def implied_full_time_months(self) -> int:
        return total_months(list(self.implied_full_time_spans), now=self.as_of)

    def _spans_in(self, bucket: ExperienceBucket) -> tuple[MonthSpan, ...]:
        return {
            ExperienceBucket.FULL_TIME: self.full_time_spans,
            ExperienceBucket.PART_TIME: self.part_time_spans,
            ExperienceBucket.INTERNSHIP: self.internship_spans,
            ExperienceBucket.PROJECT: self.project_spans,
        }[bucket]

    # --------------------------------------------------------------------- claims
    def claimable_months(self, policy: ClaimPolicy | None = None) -> int:
        """Months this skill may be claimed for, under ``policy``.

        Unions the permitted buckets, so overlapping roles cannot inflate the answer.
        Project months are never part of a years-of-experience claim.
        """
        resolved = policy or ClaimPolicy.default()
        spans = [span for bucket in resolved.buckets for span in self._spans_in(bucket)]
        return total_months(spans, now=self.as_of)

    def claimable_years(self, policy: ClaimPolicy | None = None) -> float:
        return months_to_years(self.claimable_months(policy))

    @property
    def last_used(self) -> dt.date | None:
        """The most recent month any approved fact using this skill was current."""
        merged = union_spans(
            [
                *self.full_time_spans,
                *self.part_time_spans,
                *self.internship_spans,
                *self.project_spans,
            ],
            now=self.as_of,
        )
        return merged[-1].end if merged else None

    @property
    def has_full_time_evidence(self) -> bool:
        return self.full_time_months > 0

    def months_in(self, bucket: ExperienceBucket) -> int:
        return total_months(list(self._spans_in(bucket)), now=self.as_of)

    def describe(self) -> str:
        """A phrase to show the candidate. Buckets are listed, never summed."""
        parts = []
        if self.full_time_months:
            parts.append(f"{months_to_years(self.full_time_months):.1f}y full-time")
        if self.part_time_months:
            parts.append(f"{self.part_time_months}mo part-time")
        if self.internship_months:
            parts.append(f"{self.internship_months}mo internship")
        if self.project_months:
            parts.append(f"{months_to_years(self.project_months):.1f}y projects")
        if not parts:
            parts.append("declared, no dated evidence")
        if self.last_used:
            parts.append(f"last used {self.last_used:%b %Y}")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class CareerTotals:
    """Totals over the whole graph, in the same four disjoint buckets."""

    full_time_spans: tuple[MonthSpan, ...] = ()
    part_time_spans: tuple[MonthSpan, ...] = ()
    internship_spans: tuple[MonthSpan, ...] = ()
    project_spans: tuple[MonthSpan, ...] = ()
    skills: dict[str, SkillExperience] = field(default_factory=dict)
    as_of: dt.date = dt.date.min

    @property
    def full_time_months(self) -> int:
        return total_months(list(self.full_time_spans), now=self.as_of)

    @property
    def part_time_months(self) -> int:
        return total_months(list(self.part_time_spans), now=self.as_of)

    @property
    def internship_months(self) -> int:
        return total_months(list(self.internship_spans), now=self.as_of)

    @property
    def project_months(self) -> int:
        return total_months(list(self.project_spans), now=self.as_of)

    def _spans_in(self, bucket: ExperienceBucket) -> tuple[MonthSpan, ...]:
        return {
            ExperienceBucket.FULL_TIME: self.full_time_spans,
            ExperienceBucket.PART_TIME: self.part_time_spans,
            ExperienceBucket.INTERNSHIP: self.internship_spans,
            ExperienceBucket.PROJECT: self.project_spans,
        }[bucket]

    def months_in(self, bucket: ExperienceBucket) -> int:
        return total_months(list(self._spans_in(bucket)), now=self.as_of)

    def claimable_months(self, policy: ClaimPolicy | None = None) -> int:
        """Total months of experience claimable under ``policy``. Unions, never adds."""
        resolved = policy or ClaimPolicy.default()
        spans = [span for bucket in resolved.buckets for span in self._spans_in(bucket)]
        return total_months(spans, now=self.as_of)

    def claimable_years(self, policy: ClaimPolicy | None = None) -> float:
        return months_to_years(self.claimable_months(policy))

    @property
    def first_working_month(self) -> dt.date | None:
        """When paid work started: full-time, part-time or internship, whichever first."""
        merged = union_spans(
            [*self.full_time_spans, *self.part_time_spans, *self.internship_spans],
            now=self.as_of,
        )
        return merged[0].start if merged else None

    def summary_lines(self) -> list[str]:
        """What a "graph complete" report prints. Four buckets, never a total.

        There is deliberately no combined number here: which buckets count is a
        question-by-question decision (see :class:`ClaimPolicy`), so a summary printing
        one total would be asserting an answer nobody asked for.
        """
        return [
            f"full-time:  {self.full_time_months} months ({months_to_years(self.full_time_months):.1f}y)",
            f"part-time:  {self.part_time_months} months",
            f"internship: {self.internship_months} months",
            f"projects:   {self.project_months} months ({months_to_years(self.project_months):.1f}y)",
        ]


async def _approved_experience_spans(
    session: AsyncSession, candidate_id: uuid.UUID, *, now: dt.date
) -> dict[uuid.UUID, tuple[MonthSpan, ExperienceBucket]]:
    rows = (
        await session.execute(
            select(Experience.id, Experience.start_month, Experience.end_month, Experience.kind).where(
                Experience.candidate_id == candidate_id,
                Experience.status == ApprovalStatus.APPROVED.value,
            )
        )
    ).all()
    return {row[0]: (MonthSpan(row[1], row[2]).resolve(now), ExperienceKind(row[3]).bucket) for row in rows}


async def _approved_project_spans(
    session: AsyncSession, candidate_id: uuid.UUID, *, now: dt.date
) -> dict[uuid.UUID, MonthSpan | None]:
    rows = (
        await session.execute(
            select(Project.id, Project.start_month, Project.end_month).where(
                Project.candidate_id == candidate_id,
                Project.status == ApprovalStatus.APPROVED.value,
            )
        )
    ).all()
    # A project with no dates is still evidence of the skill, just not of time.
    return {row[0]: (MonthSpan(row[1], row[2]).resolve(now) if row[1] is not None else None) for row in rows}


async def compute_skill_experience(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    taxonomy: SkillTaxonomy | None = None,
    now: dt.date | None = None,
) -> dict[str, SkillExperience]:
    """Months of evidence per skill, by bucket, from approved facts only.

    Pass ``taxonomy`` to also compute implied months; without it only direct evidence
    is returned. ``now`` fixes the clock, which is what makes this testable and what
    stops two reads of one graph disagreeing.
    """
    moment = now or utc_now().date()
    experiences = await _approved_experience_spans(session, candidate_id, now=moment)
    projects = await _approved_project_spans(session, candidate_id, now=moment)
    usage = await approved_skill_usage(session, candidate_id)

    by_bucket: dict[ExperienceBucket, dict[str, list[MonthSpan]]] = {
        bucket: defaultdict(list) for bucket in ExperienceBucket
    }
    counts: dict[str, int] = defaultdict(int)
    touched: set[str] = set()

    for canonical, experience_id, project_id in usage:
        counts[canonical] += 1
        touched.add(canonical)
        if experience_id is not None and experience_id in experiences:
            span, bucket = experiences[experience_id]
            by_bucket[bucket][canonical].append(span)
        elif project_id is not None:
            project_span = projects.get(project_id)
            if project_span is not None:
                by_bucket[ExperienceBucket.PROJECT][canonical].append(project_span)

    result: dict[str, SkillExperience] = {
        canonical: SkillExperience(
            canonical=canonical,
            full_time_spans=tuple(by_bucket[ExperienceBucket.FULL_TIME][canonical]),
            part_time_spans=tuple(by_bucket[ExperienceBucket.PART_TIME][canonical]),
            internship_spans=tuple(by_bucket[ExperienceBucket.INTERNSHIP][canonical]),
            project_spans=tuple(by_bucket[ExperienceBucket.PROJECT][canonical]),
            fact_count=counts[canonical],
            as_of=moment,
        )
        for canonical in sorted(touched)
    }

    if taxonomy is not None:
        result = _add_implied_spans(
            result,
            taxonomy=taxonomy,
            now=moment,
            full_time=by_bucket[ExperienceBucket.FULL_TIME],
        )
    return result


def _add_implied_spans(
    direct: dict[str, SkillExperience],
    *,
    taxonomy: SkillTaxonomy,
    now: dt.date,
    full_time: dict[str, list[MonthSpan]],
) -> dict[str, SkillExperience]:
    """Fill in implied full-time spans without touching any direct bucket.

    React's months flow to JavaScript here, and only here. Direct buckets stay
    whatever the facts say, so a caller printing ``full_time_months`` cannot print an
    implication by accident.

    Only full-time spans propagate: an implied *project* year would be two inferences
    stacked on each other.
    """
    implied: dict[str, list[MonthSpan]] = defaultdict(list)
    for canonical, spans in full_time.items():
        if canonical not in taxonomy:
            continue
        for target in taxonomy.implications(canonical):
            implied[target].extend(spans)

    updated = dict(direct)
    for target, spans in implied.items():
        existing = updated.get(target)
        if existing is None:
            updated[target] = SkillExperience(
                canonical=target, implied_full_time_spans=tuple(spans), as_of=now
            )
        else:
            updated[target] = SkillExperience(
                canonical=existing.canonical,
                full_time_spans=existing.full_time_spans,
                part_time_spans=existing.part_time_spans,
                internship_spans=existing.internship_spans,
                project_spans=existing.project_spans,
                implied_full_time_spans=tuple(spans),
                fact_count=existing.fact_count,
                as_of=existing.as_of,
            )
    return dict(sorted(updated.items()))


async def compute_career_totals(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    taxonomy: SkillTaxonomy | None = None,
    now: dt.date | None = None,
) -> CareerTotals:
    """Time in each disjoint bucket, plus per-skill evidence.

    Experiences count towards totals whether or not they carry facts: a job the
    candidate approved is time they worked, even before its bullets are written.
    Skills, by contrast, need facts — a skill with no fact behind it has no months.
    """
    moment = now or utc_now().date()
    experiences = await _approved_experience_spans(session, candidate_id, now=moment)
    projects = await _approved_project_spans(session, candidate_id, now=moment)

    spans_by_bucket: dict[ExperienceBucket, list[MonthSpan]] = {bucket: [] for bucket in ExperienceBucket}
    for span, bucket in experiences.values():
        spans_by_bucket[bucket].append(span)
    spans_by_bucket[ExperienceBucket.PROJECT].extend(span for span in projects.values() if span is not None)

    return CareerTotals(
        full_time_spans=tuple(spans_by_bucket[ExperienceBucket.FULL_TIME]),
        part_time_spans=tuple(spans_by_bucket[ExperienceBucket.PART_TIME]),
        internship_spans=tuple(spans_by_bucket[ExperienceBucket.INTERNSHIP]),
        project_spans=tuple(spans_by_bucket[ExperienceBucket.PROJECT]),
        skills=await compute_skill_experience(session, candidate_id, taxonomy=taxonomy, now=moment),
        as_of=moment,
    )
