"""Shape rules and refusal gates for an import plan (RESUME_ENGINE §5.5).

Applied to every door that assembles entries — a PDF's layout and pasted text alike —
because the gates are what stand between a weaker reading and the graph (DECISIONS
2026-10-11, Q8).

**Per entry.** An employer is at most 60 characters and 8 words, holds no metric, does
not end in a full stop and does not begin with a resume verb ("Built", "Led"); a title
is at most 60 characters with no metric and no full stop; a project's name follows the
employer's rules. Dates start no earlier than 1950. A role needs at least one bullet.
A violation drops that entry and is reported; a dropped entry is never replaced by a
guess. A start date more than a year ahead is flagged, not silently accepted.

**Per bullet.** A bullet under 20 characters is dropped and reported — the bullet,
never the entry (DECISIONS 2026-10-11, Q6). A role left with no bullet is then dropped
under the rule above.

**Per document.** Any one of these returns ``needs_user_fix`` and the plan is not to be
written at all (Q7):

- two or more consecutive roles with no bullets between them — the reading order is
  suspect; this is what a flat-text reading of a float-scrambled page looks like;
- three or more entries and no facts at all;
- more than a quarter of entries failing a shape rule;
- more than three overlapping pairs of role dates — likely bleed across columns.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import combinations

from aria_core.importer.plan import ImportPlan, ProposedExperience, ProposedFact, ProposedProject

__all__ = [
    "MIN_BULLET_CHARACTERS",
    "DroppedBullet",
    "DroppedEntry",
    "IntakeStatus",
    "IntakeVerdict",
    "assess",
]

MIN_BULLET_CHARACTERS = 20
_MAX_NAME_CHARACTERS = 60
_MAX_NAME_WORDS = 8
_EARLIEST_YEAR = 1950
_FUTURE_LIMIT_MONTHS = 12
_MAX_FAILED_SHARE = 0.25
_MAX_OVERLAPPING_PAIRS = 3
_METRIC = re.compile(r"\d+\s?%|\d+\+|\b\d+(?:\.\d+)?[KkMm]\b")
#: The verbs a bullet starts with. An employer or a project name that starts with one
#: is a bullet that has been read as a header.
_RESUME_VERBS = frozenset(
    """achieved added analyzed architected automated built collaborated created cut decreased delivered
    deployed designed developed drove eliminated engineered established grounded implemented improved
    increased integrated launched led maintained managed mentored migrated optimized owned published
    reduced refactored scaled shipped streamlined trained wrote""".split()
)


class IntakeStatus(StrEnum):
    READY = "ready"
    NEEDS_USER_FIX = "needs_user_fix"


@dataclass(frozen=True, slots=True)
class DroppedEntry:
    kind: str
    label: str
    source_line: int
    reason: str


@dataclass(frozen=True, slots=True)
class DroppedBullet:
    entry: str
    source_line: int
    reason: str


@dataclass(frozen=True, slots=True)
class IntakeVerdict:
    status: IntakeStatus
    #: With dropped entries and bullets removed when READY; as assembled otherwise.
    plan: ImportPlan
    #: Why the document was refused. Empty when READY.
    reasons: tuple[str, ...] = ()
    dropped_entries: tuple[DroppedEntry, ...] = ()
    dropped_bullets: tuple[DroppedBullet, ...] = ()
    #: Accepted, but worth the candidate's attention: a start date far ahead, say.
    flags: tuple[str, ...] = ()


def _name_problem(text: str, what: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return f"no {what}"
    if len(stripped) > _MAX_NAME_CHARACTERS:
        return f"the {what} is longer than {_MAX_NAME_CHARACTERS} characters"
    if len(stripped.split()) > _MAX_NAME_WORDS:
        return f"the {what} is more than {_MAX_NAME_WORDS} words"
    if stripped.endswith("."):
        return f"the {what} ends like a sentence"
    if _METRIC.search(stripped):
        return f"the {what} holds a metric"
    if stripped.split()[0].lower() in _RESUME_VERBS:
        return f"the {what} starts with a verb, like a bullet"
    return None


def _title_problem(text: str) -> str | None:
    stripped = text.strip()
    if len(stripped) > _MAX_NAME_CHARACTERS:
        return f"the title is longer than {_MAX_NAME_CHARACTERS} characters"
    if stripped.endswith("."):
        return "the title ends like a sentence"
    if _METRIC.search(stripped):
        return "the title holds a metric"
    return None


def _long_enough(
    facts: tuple[ProposedFact, ...], label: str
) -> tuple[tuple[ProposedFact, ...], list[DroppedBullet]]:
    kept, dropped = [], []
    for fact in facts:
        if len(fact.text.strip()) < MIN_BULLET_CHARACTERS:
            dropped.append(
                DroppedBullet(label, fact.source_line, f"shorter than {MIN_BULLET_CHARACTERS} characters")
            )
        else:
            kept.append(fact)
    return tuple(kept), dropped


def _months_ahead(month: dt.date, today: dt.date) -> int:
    return (month.year - today.year) * 12 + (month.month - today.month)


def _role_problem(role: ProposedExperience, today: dt.date) -> tuple[str | None, str | None]:
    """(reason to drop, flag to raise)."""
    problem = _name_problem(role.employer, "employer") or _title_problem(role.title)
    if problem is not None:
        return problem, None
    if role.span is not None:
        if role.span.start.year < _EARLIEST_YEAR:
            return f"the start date is before {_EARLIEST_YEAR}", None
        if _months_ahead(role.span.start, today) > _FUTURE_LIMIT_MONTHS:
            return None, f"{role.employer}: the start date is more than a year ahead"
    if not role.facts:
        return "a role with no bullets under it", None
    return None, None


def _consecutive_empty_roles(roles: tuple[ProposedExperience, ...]) -> bool:
    run = 0
    for role in sorted(roles, key=lambda item: item.source_line):
        run = run + 1 if not role.facts else 0
        if run >= 2:
            return True
    return False


def _overlapping_pairs(roles: tuple[ProposedExperience, ...], today: dt.date) -> int:
    spans = [role.span for role in roles if role.span is not None]
    count = 0
    for first, second in combinations(spans, 2):
        first_end = first.end or today
        second_end = second.end or today
        if first.start <= second_end and second.start <= first_end:
            count += 1
    return count


def assess(plan: ImportPlan, *, today: dt.date) -> IntakeVerdict:
    """Apply §5.5 to an assembled plan."""
    entries = len(plan.experiences) + len(plan.projects) + len(plan.educations)
    facts = (
        sum(len(role.facts) for role in plan.experiences)
        + sum(len(project.facts) for project in plan.projects)
        + sum(len(education.facts) for education in plan.educations)
        + len(plan.loose_facts)
    )

    reasons: list[str] = []
    if _consecutive_empty_roles(plan.experiences):
        reasons.append(
            "two or more roles in a row have no bullets under them, which is how a page read in the "
            "wrong order looks"
        )
    if entries >= 3 and facts == 0:
        reasons.append(f"{entries} entries were found and not one achievement under any of them")
    overlaps = _overlapping_pairs(plan.experiences, today)
    if overlaps > _MAX_OVERLAPPING_PAIRS:
        reasons.append(
            f"{overlaps} pairs of roles overlap in time, which suggests columns read across each other"
        )

    dropped_entries: list[DroppedEntry] = []
    dropped_bullets: list[DroppedBullet] = []
    flags: list[str] = []
    roles: list[ProposedExperience] = []
    for role in plan.experiences:
        label = f"{role.employer} \u2014 {role.title}".strip(" \u2014")
        kept, short = _long_enough(role.facts, label)
        dropped_bullets.extend(short)
        candidate = replace(role, facts=kept)
        reason, flag = _role_problem(candidate, today)
        if flag:
            flags.append(flag)
        if reason is not None:
            dropped_entries.append(DroppedEntry("experience", label, role.source_line, reason))
        else:
            roles.append(candidate)
    projects: list[ProposedProject] = []
    for project in plan.projects:
        kept, short = _long_enough(project.facts, project.name)
        dropped_bullets.extend(short)
        reason = _name_problem(project.name, "project name")
        if reason is not None:
            dropped_entries.append(DroppedEntry("project", project.name, project.source_line, reason))
        else:
            projects.append(replace(project, facts=kept))

    checked = len(plan.experiences) + len(plan.projects)
    if checked and len(dropped_entries) / checked > _MAX_FAILED_SHARE:
        reasons.append(
            f"{len(dropped_entries)} of {checked} entries do not have the shape of an entry, too many to "
            "trust the rest"
        )

    if reasons:
        return IntakeVerdict(
            status=IntakeStatus.NEEDS_USER_FIX,
            plan=plan,
            reasons=tuple(reasons),
            dropped_entries=tuple(dropped_entries),
            dropped_bullets=tuple(dropped_bullets),
            flags=tuple(flags),
        )
    return IntakeVerdict(
        status=IntakeStatus.READY,
        plan=replace(plan, experiences=tuple(roles), projects=tuple(projects)),
        dropped_entries=tuple(dropped_entries),
        dropped_bullets=tuple(dropped_bullets),
        flags=tuple(flags),
    )
