"""A plan to a page in the facts' own words: the repair loop's last resort (RESUME_ENGINE §9).

Every bullet is the approved fact's text, verbatim; every header is the snapshot's
(:mod:`.headers`); the contact line holds only approved contact details. Nothing here is
written, so the page passes the truth check (6b) by construction. It may fail the recruiter
check (6c), a fact's first word is not always an allowed verb, which is what the writer
(5b) is for; a fallback page that fails 6c goes to the candidate as needs_review.

There is no summary: a template sentence is the AI voice §1 bans, and only the writer,
from evidence, may add one (DECISIONS 2026-10-18).
"""

from __future__ import annotations

from aria_core.schemas.snapshot import CandidateSnapshot
from aria_resume.compose.headers import Header, degree_header, project_header, role_header
from aria_resume.compose.planner import PlanError
from aria_resume.document import (
    ContactItem,
    DocumentBullet,
    DocumentEntry,
    DocumentSection,
    ResumeDocument,
    SectionKind,
)
from aria_resume.schemas import PlannedEntry, ResumePlan

__all__ = ["contact_items", "fallback_document"]


def _bare(url: str) -> str:
    for prefix in ("https://www.", "https://", "http://www.", "http://"):
        if url.startswith(prefix):
            return url[len(prefix) :].rstrip("/")
    return url


def contact_items(snapshot: CandidateSnapshot) -> tuple[ContactItem, ...]:
    contact = snapshot.contact
    items: list[ContactItem] = []
    if contact.email:
        items.append(ContactItem(text=contact.email, url=f"mailto:{contact.email}"))
    if contact.phone:
        items.append(ContactItem(text=contact.phone))
    place = ", ".join(part for part in (contact.location_city, contact.location_region) if part)
    if place:
        items.append(ContactItem(text=place))
    for url in (contact.linkedin_url, contact.github_url, contact.portfolio_url):
        if url and url.startswith("https://"):
            items.append(ContactItem(text=_bare(url), url=url))
    return tuple(items)


def _header(entry: PlannedEntry, snapshot: CandidateSnapshot) -> Header:
    if entry.kind is SectionKind.EXPERIENCE:
        return role_header(
            next(item for item in snapshot.experiences if item.experience_id == entry.source_id)
        )
    if entry.kind is SectionKind.PROJECTS:
        return project_header(next(item for item in snapshot.projects if item.project_id == entry.source_id))
    return degree_header(next(item for item in snapshot.education if item.education_id == entry.source_id))


def fallback_document(plan: ResumePlan, snapshot: CandidateSnapshot) -> ResumeDocument:
    if not snapshot.contact.full_name:
        raise PlanError("the snapshot has no approved name")
    sections: list[DocumentSection] = []
    for planned in plan.sections:
        entries = []
        for entry in planned.entries:
            header = _header(entry, snapshot)
            bullets = tuple(
                DocumentBullet(
                    text=" ".join(snapshot.fact(slot.fact_ids[0]).text.split()), fact_ids=slot.fact_ids
                )
                for slot in entry.bullets
            )
            entries.append(
                DocumentEntry(
                    title=header.title,
                    detail=header.detail,
                    dates=header.dates,
                    subline=header.subline,
                    bullets=bullets,
                    source_id=entry.source_id,
                )
            )
        sections.append(DocumentSection(kind=planned.kind, lines=planned.lines, entries=tuple(entries)))
    return ResumeDocument(
        name=snapshot.contact.full_name, contact=contact_items(snapshot), sections=tuple(sections)
    )
