"""The candidate graph (RESUME_ENGINE §5).

Nodes and edges in ordinary tables: a graph database is not needed at one career's
worth of data, and Postgres gives us the row-level security and the constraints that
matter more here than traversal speed.

Three rules are enforced by the schema rather than by the code that uses it:

* **Nothing is true until the candidate says so.** Every row that makes a claim
  carries an :class:`~aria_core.graph.status.ApprovalStatus`, and import produces
  drafts only. The read path for engines is
  :func:`aria_core.graph.facts.approved_facts`, and an architecture test fails if
  anything else reads the table.
* **A fact's text is immutable.** Editing one writes a new row in
  ``fact_revisions``; ``facts.current_version`` moves. RESUME_ENGINE §1.10 promises
  a resume is reproducible from the same inputs and versions, which is only true if
  a resume can point at the exact text it used.
* **Every claim knows where it came from.** ``source_upload_id`` and
  ``source_line`` travel on drafts, so the approval view can show the candidate the
  line of their own resume a fact was taken from instead of asking them to trust the
  importer.

Months, not days: resumes say "Jun 2024 - Present". See
:func:`aria_core.db.models.base.month_column`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aria_core.db.models.base import Base, created_at_column, month_column, uuid_primary_key
from aria_core.graph.status import (
    AchievementKind,
    ApprovalStatus,
    ExperienceKind,
    GraphSource,
    MotivationKind,
    ProfileFieldKind,
    ProjectKind,
    Theme,
)

__all__ = [
    "GRAPH_TENANT_TABLES",
    "Achievement",
    "Candidate",
    "DeclaredSkill",
    "Education",
    "Experience",
    "Fact",
    "FactMetric",
    "FactRevision",
    "FactSkill",
    "FactTheme",
    "Motivation",
    "ProfileField",
    "Project",
    "Upload",
]


def _enum_check(column: str, enum: type[StrEnum], name: str) -> CheckConstraint:
    """A CHECK listing an enum's values.

    Text with a constraint rather than a Postgres enum type: adding a value is then
    an ordinary migration instead of a type rewrite, and the constraint still stops
    a typo reaching the table.
    """
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=name)


def _status_column() -> Mapped[str]:
    return mapped_column(String(20), nullable=False, default=ApprovalStatus.DRAFT.value)


class Candidate(Base):
    """One person's graph. One per user, so the join is explicit rather than implied."""

    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Whether "years of experience" may include internship months for this
    #: candidate. Default false: by default the figure is full-time only, and a new
    #: grad who answers "2.6 years" on the strength of internships has overstated.
    #: Some candidates legitimately want them counted, so it is their setting to
    #: make — not ours, and not a guess (see aria_core.graph.derive.ClaimPolicy).
    count_internships_as_experience: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (UniqueConstraint("user_id"),)


class Upload(Base):
    """One file the candidate gave us, and what happened to it (SECURITY.md §7).

    ``original_filename`` is untrusted input: it is recorded so the candidate
    recognises the file, and is never used to build a path or to decide a type.
    ``detected_type`` comes from magic bytes.
    """

    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    #: As the client sent it. Untrusted, display-only (S1 — it often contains a name).
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    detected_type: Mapped[str] = mapped_column(String(20), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Generated name inside the quarantine store. Never derived from the filename.
    quarantine_name: Mapped[str] = mapped_column(String(100), nullable=False)
    scan_status: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Which scanner said so, e.g. "clamav 1.4.2" or "dev-scanner (not scanned)".
    scan_detail: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    parse_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    parse_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Extracted text — this phase's sanitized derivative. Untrusted content (S1).
    extracted_text: Mapped[str | None] = mapped_column(Text)
    #: True when the parser was unsure: a page whose reading order its two extractors
    #: disagreed about. The facts from this upload still import, and the approval view
    #: tells the candidate which pages to check against the original.
    needs_review: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: The original is deleted after processing unless the candidate opts to keep it.
    keep_original: Mapped[bool] = mapped_column(nullable=False, default=False)
    original_deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("size_bytes > 0 AND size_bytes <= 10485760", name="size_within_limit"),
        CheckConstraint("detected_type IN ('pdf', 'docx', 'txt', 'png', 'jpeg')", name="type_is_allowed"),
        CheckConstraint(
            "scan_status IN ('clean', 'infected', 'not_scanned', 'error')", name="scan_status_is_known"
        ),
        CheckConstraint(
            "parse_status IN ('pending', 'parsed', 'refused', 'failed')", name="parse_status_is_known"
        ),
        UniqueConstraint("candidate_id", "sha256"),
        Index("ix_uploads_candidate_id_created_at", "candidate_id", "created_at"),
    )


class ProfileField(Base):
    """Contact and identity fields, imported from a resume header as drafts.

    No street-address kind exists (see
    :class:`~aria_core.graph.status.ProfileFieldKind`).
    """

    __tablename__ = "profile_fields"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = _status_column()
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    source_line: Mapped[int | None] = mapped_column()
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who approved it. Approving a role is as much a truth claim as approving a
    #: bullet, so it carries a name for the same reason facts do.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        _enum_check("kind", ProfileFieldKind, "kind_is_known"),
        _enum_check("status", ApprovalStatus, "status_is_known"),
        _enum_check("source", GraphSource, "source_is_known"),
        # One approved value per kind, except the kinds a person can have several of.
        Index(
            "uq_profile_fields_one_approved_per_kind",
            "candidate_id",
            "kind",
            unique=True,
            postgresql_where="status = 'approved' AND kind <> 'other_url'",
        ),
        Index("ix_profile_fields_candidate_id_status", "candidate_id", "status"),
    )


class Experience(Base):
    """A job. ``client`` captures consulting arrangements (Carvana via Accenture)."""

    __tablename__ = "experiences"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    employer: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    client: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: City and region only. No street address is collected (SECURITY.md §3).
    location_city: Mapped[str | None] = mapped_column(String(120))
    location_region: Mapped[str | None] = mapped_column(String(120))
    start_month: Mapped[dt.date] = month_column()
    #: NULL means "Present": still there as of now, not "unknown".
    end_month: Mapped[dt.date | None] = month_column(nullable=True)
    status: Mapped[str] = _status_column()
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    source_line: Mapped[int | None] = mapped_column()
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who approved it. Approving a role is as much a truth claim as approving a
    #: bullet, so it carries a name for the same reason facts do.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        _enum_check("kind", ExperienceKind, "kind_is_known"),
        _enum_check("status", ApprovalStatus, "status_is_known"),
        _enum_check("source", GraphSource, "source_is_known"),
        CheckConstraint("EXTRACT(DAY FROM start_month) = 1", name="start_is_a_month"),
        CheckConstraint("end_month IS NULL OR EXTRACT(DAY FROM end_month) = 1", name="end_is_a_month"),
        CheckConstraint("end_month IS NULL OR end_month >= start_month", name="ends_after_it_starts"),
        Index("ix_experiences_candidate_id_status", "candidate_id", "status"),
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    url: Mapped[str | None] = mapped_column(String(500))
    repository_url: Mapped[str | None] = mapped_column(String(500))
    start_month: Mapped[dt.date | None] = month_column(nullable=True)
    end_month: Mapped[dt.date | None] = month_column(nullable=True)
    status: Mapped[str] = _status_column()
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    source_line: Mapped[int | None] = mapped_column()
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who approved it. Approving a role is as much a truth claim as approving a
    #: bullet, so it carries a name for the same reason facts do.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        _enum_check("kind", ProjectKind, "kind_is_known"),
        _enum_check("status", ApprovalStatus, "status_is_known"),
        _enum_check("source", GraphSource, "source_is_known"),
        CheckConstraint("start_month IS NULL OR EXTRACT(DAY FROM start_month) = 1", name="start_is_a_month"),
        CheckConstraint("end_month IS NULL OR EXTRACT(DAY FROM end_month) = 1", name="end_is_a_month"),
        CheckConstraint(
            "start_month IS NULL OR end_month IS NULL OR end_month >= start_month",
            name="ends_after_it_starts",
        ),
        Index("ix_projects_candidate_id_status", "candidate_id", "status"),
    )


class Education(Base):
    __tablename__ = "education"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    school: Mapped[str] = mapped_column(String(200), nullable=False)
    degree: Mapped[str] = mapped_column(String(200), nullable=False)
    field_of_study: Mapped[str | None] = mapped_column(String(200))
    #: Optional on purpose: a GPA is only on a resume if the candidate wants it there.
    gpa: Mapped[str | None] = mapped_column(String(20))
    location_city: Mapped[str | None] = mapped_column(String(120))
    location_region: Mapped[str | None] = mapped_column(String(120))
    start_month: Mapped[dt.date | None] = month_column(nullable=True)
    end_month: Mapped[dt.date | None] = month_column(nullable=True)
    coursework: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = _status_column()
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    source_line: Mapped[int | None] = mapped_column()
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who approved it. Approving a role is as much a truth claim as approving a
    #: bullet, so it carries a name for the same reason facts do.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        _enum_check("status", ApprovalStatus, "status_is_known"),
        _enum_check("source", GraphSource, "source_is_known"),
        CheckConstraint("start_month IS NULL OR EXTRACT(DAY FROM start_month) = 1", name="start_is_a_month"),
        CheckConstraint("end_month IS NULL OR EXTRACT(DAY FROM end_month) = 1", name="end_is_a_month"),
        Index("ix_education_candidate_id_status", "candidate_id", "status"),
    )


class Fact(Base):
    """The atomic unit: one true statement, verbatim as the candidate approved it.

    The text lives in :class:`FactRevision`. This row is the identity, the lifecycle
    and the anchor — which experience, project or education it belongs to.
    """

    __tablename__ = "facts"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    #: Exactly one anchor is set, or none for a standalone fact. A CHECK enforces it,
    #: because a fact attached to two experiences would appear twice on a resume.
    experience_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("experiences.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    education_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("education.id", ondelete="CASCADE")
    )
    status: Mapped[str] = _status_column()
    current_version: Mapped[int] = mapped_column(nullable=False, default=1)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    source_line: Mapped[int | None] = mapped_column()
    #: Set when this fact was created by merging near-duplicates, pointing at the
    #: fact it was merged into or from, so a merge can be explained and undone.
    merged_into_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facts.id", ondelete="SET NULL")
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        _enum_check("status", ApprovalStatus, "status_is_known"),
        _enum_check("source", GraphSource, "source_is_known"),
        CheckConstraint(
            "(experience_id IS NOT NULL)::int + (project_id IS NOT NULL)::int "
            "+ (education_id IS NOT NULL)::int <= 1",
            name="at_most_one_anchor",
        ),
        CheckConstraint("current_version >= 1", name="version_starts_at_one"),
        CheckConstraint(
            "(status = 'approved') = (approved_at IS NOT NULL)", name="approved_rows_record_when"
        ),
        Index("ix_facts_candidate_id_status", "candidate_id", "status"),
        Index("ix_facts_source_upload_id", "source_upload_id"),
    )


class FactRevision(Base):
    """One version of a fact's text. Immutable once written.

    An edit appends; nothing updates. A resume records the versions it used, so it
    can be explained months later with the exact words that were on it
    (RESUME_ENGINE §1.10, §9).
    """

    __tablename__ = "fact_revisions"

    fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facts.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        _enum_check("source", GraphSource, "source_is_known"),
        CheckConstraint("version >= 1", name="version_starts_at_one"),
        CheckConstraint("length(btrim(text)) > 0", name="text_is_not_blank"),
    )


class FactMetric(Base):
    """A number a fact produced — the ``PRODUCED`` edge.

    ``exact_value`` is verbatim: "30%" stays "30%", "500K+" stays "500K+", and
    "mAP@0.5 = 0.700" keeps its precision (RESUME_ENGINE §1.6). It is text, not a
    number, because parsing it to a float and rendering it back is exactly how a
    metric gets rounded.
    """

    __tablename__ = "fact_metrics"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facts.id", ondelete="CASCADE"), nullable=False
    )
    exact_value: Mapped[str] = mapped_column(String(100), nullable=False)
    #: What it measures, in the candidate's words: "p95 latency", "weekly active users".
    measures: Mapped[str | None] = mapped_column(String(300))
    #: "30% faster **than the previous pipeline**" — the comparison, verbatim.
    baseline: Mapped[str | None] = mapped_column(String(300))
    source_line: Mapped[int | None] = mapped_column()
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("length(btrim(exact_value)) > 0", name="value_is_not_blank"),
        Index("ix_fact_metrics_fact_id", "fact_id"),
    )


class FactSkill(Base):
    """The ``USES`` edge: this fact is evidence of this skill.

    ``surface_form`` keeps the candidate's own spelling, so a resume can print what
    the posting asked for while the match is made on the canonical id.
    """

    __tablename__ = "fact_skills"

    fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facts.id", ondelete="CASCADE"), primary_key=True
    )
    skill_canonical: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    surface_form: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (Index("ix_fact_skills_skill_canonical", "skill_canonical"),)


class FactTheme(Base):
    """The ``DEMONSTRATES`` edge."""

    __tablename__ = "fact_themes"

    fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facts.id", ondelete="CASCADE"), primary_key=True
    )
    theme: Mapped[str] = mapped_column(String(30), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (_enum_check("theme", Theme, "theme_is_known"),)


class DeclaredSkill(Base):
    """A skill the candidate says they have, independent of any one fact.

    This is what the resume's Skills section draws on: "candidate's verified skills
    only" (RESUME_ENGINE §7). Kept apart from :class:`FactSkill` because the two
    answer different questions — "do you have this skill?" and "what did you do with
    it?" — and a skill with no fact behind it can be listed but never bulleted.
    """

    __tablename__ = "declared_skills"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    skill_canonical: Mapped[str] = mapped_column(String(80), nullable=False)
    surface_form: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = _status_column()
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    source_line: Mapped[int | None] = mapped_column()
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who approved it. Approving a role is as much a truth claim as approving a
    #: bullet, so it carries a name for the same reason facts do.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        _enum_check("status", ApprovalStatus, "status_is_known"),
        _enum_check("source", GraphSource, "source_is_known"),
        UniqueConstraint("candidate_id", "skill_canonical"),
    )


class Achievement(Base):
    __tablename__ = "achievements"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(200))
    awarded_month: Mapped[dt.date | None] = month_column(nullable=True)
    url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = _status_column()
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    source_line: Mapped[int | None] = mapped_column()
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who approved it. Approving a role is as much a truth claim as approving a
    #: bullet, so it carries a name for the same reason facts do.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        _enum_check("kind", AchievementKind, "kind_is_known"),
        _enum_check("status", ApprovalStatus, "status_is_known"),
        _enum_check("source", GraphSource, "source_is_known"),
        CheckConstraint(
            "awarded_month IS NULL OR EXTRACT(DAY FROM awarded_month) = 1", name="awarded_is_a_month"
        ),
    )


class Motivation(Base):
    """The candidate's own reasons and preferences, in their own words.

    Used by the Answer Engine for "why this role?" (PRODUCT_SPEC §3.4). Never
    inferred: an invented motivation is a lie in the most personal part of an
    application.
    """

    __tablename__ = "motivations"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = _status_column()
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who approved it. Approving a role is as much a truth claim as approving a
    #: bullet, so it carries a name for the same reason facts do.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at_column()
    updated_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        _enum_check("kind", MotivationKind, "kind_is_known"),
        _enum_check("status", ApprovalStatus, "status_is_known"),
        _enum_check("source", GraphSource, "source_is_known"),
        CheckConstraint("length(btrim(text)) > 0", name="text_is_not_blank"),
        Index("ix_motivations_candidate_id_kind", "candidate_id", "kind"),
    )


class UserQuestion(Base):
    """Something ARIA will not guess, waiting for the candidate to answer.

    The table behind "ask, don't guess" (CLAUDE.md). Before this existed, an
    importer's questions lived on the plan and in the audit log and vanished with the
    request, so the only way to see them again was to import the file twice.

    ``payload`` carries what answering would create. A role the resume dated "Summer
    2023" is not in the graph at all, so the question has to hold the whole proposal
    or answering it could only record a date with nothing to attach it to.

    ``dedupe_key`` is unique per candidate. Re-importing the same resume must not ask
    the same question again: a queue that grows by five with every upload is one
    nobody reads, and that ends with an unreviewed draft on a resume.
    """

    __tablename__ = "user_questions"

    id: Mapped[uuid.UUID] = uuid_primary_key()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    #: S1 — quotes the candidate's own resume back to them.
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    source_lines: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    #: S1 — what answering would create, in the candidate's own words.
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Set once the question has produced a row, so a view can link to it.
    subject_kind: Mapped[str | None] = mapped_column(String(20))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    answer: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    answered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("length(btrim(prompt)) > 0", name="prompt_is_not_blank"),
        # An answered question names who answered it and when. Without this an answer
        # could appear with nobody attached, and the row it created would have no
        # evidence behind it.
        CheckConstraint(
            "(status <> 'answered') OR "
            "(answer IS NOT NULL AND answered_at IS NOT NULL AND answered_by IS NOT NULL)",
            name="answer_has_an_author",
        ),
        UniqueConstraint("candidate_id", "dedupe_key", name="candidate_id_dedupe_key"),
        Index("ix_user_questions_candidate_id", "candidate_id"),
        Index("ix_user_questions_tenant_id", "tenant_id"),
    )


#: Graph tables, all tenant-scoped. Joined with the platform list in
#: :mod:`aria_core.db.models`, which is what the row-level security sweep checks.
GRAPH_TENANT_TABLES: frozenset[str] = frozenset(
    {
        "candidates",
        "uploads",
        "profile_fields",
        "experiences",
        "projects",
        "education",
        "facts",
        "fact_revisions",
        "fact_metrics",
        "fact_skills",
        "fact_themes",
        "declared_skills",
        "achievements",
        "motivations",
        "user_questions",
    }
)
