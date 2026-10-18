"""Entry headers, computed from the approved snapshot, never written by a model.

An employer, a job title, two months: each is a claim, and the candidate approved each one
on the role itself. So a header is not composed, it is looked up, and the truth check (6b)
compares the printed header with this function's answer for the same role.

- **A role:** the employer in bold, then the title; the months in the right-hand band; the
  client, when there is one, and the location on the second line. The employer is who paid
  the candidate: "Accenture — Software Engineer" with "Client: Carvana", never
  "Carvana (via Accenture)", which would misstate the employer (DECISIONS 2026-10-18).
- **A project:** its name; its months when it has them.
- **A degree:** the school, then the degree and field; the month it ends (or ended).

Months print as two months, or a month and "Present", joined by a spaced en dash ("Feb 2024",
the dash, "Jul 2025"); or one month for a degree: the one date style 6d checks for.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from aria_core.schemas.snapshot import SnapshotEducation, SnapshotExperience, SnapshotProject

__all__ = ["Header", "degree_header", "month", "project_header", "role_header"]

EN_DASH = "\u2013"


@dataclass(frozen=True, slots=True)
class Header:
    title: str
    detail: str | None
    dates: str
    subline: str | None


def month(value: dt.date) -> str:
    return value.strftime("%b %Y")


def _span(start: dt.date | None, end: dt.date | None, *, current_allowed: bool) -> str:
    if start is None:
        return month(end) if end else ""
    if end is None:
        return f"{month(start)} {EN_DASH} Present" if current_allowed else month(start)
    if (start.year, start.month) == (end.year, end.month):
        return month(start)
    return f"{month(start)} {EN_DASH} {month(end)}"


def _place(city: str | None, region: str | None) -> str | None:
    parts = [part for part in (city, region) if part]
    return ", ".join(parts) if parts else None


def role_header(role: SnapshotExperience) -> Header:
    second = [
        part
        for part in (
            f"Client: {role.client}" if role.client else None,
            _place(role.location_city, role.location_region),
        )
        if part
    ]
    return Header(
        title=role.employer,
        detail=role.title,
        dates=_span(role.start_month, role.end_month, current_allowed=True),
        subline=" \u00b7 ".join(second) if second else None,
    )


def project_header(project: SnapshotProject) -> Header:
    return Header(
        title=project.name,
        detail=None,
        dates=_span(project.start_month, project.end_month, current_allowed=True),
        subline=None,
    )


def degree_header(degree: SnapshotEducation) -> Header:
    detail = f"{degree.degree} in {degree.field_of_study}" if degree.field_of_study else degree.degree
    when = degree.end_month or degree.start_month
    return Header(
        title=degree.school,
        detail=detail,
        dates=month(when) if when else "",
        subline=_place(degree.location_city, degree.location_region),
    )
