"""The vocabulary of the candidate graph (RESUME_ENGINE §5).

Enums rather than free strings, because every one of these ends up in a CHECK
constraint, an audit payload or a resume artifact, and a typo in any of those is a
silent behaviour change.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AchievementKind",
    "ApprovalStatus",
    "ExperienceBucket",
    "ExperienceKind",
    "GraphSource",
    "MotivationKind",
    "ProfileFieldKind",
    "ProjectKind",
    "Theme",
]


class ApprovalStatus(StrEnum):
    """Nothing in the graph is true until the candidate says it is.

    Import and the enrichment interview both produce ``DRAFT``. Only an explicit
    human action reaches ``APPROVED``, and only approved rows may appear on a resume
    or in an answer (RESUME_ENGINE §5: "Only verified facts can appear on a resume").
    """

    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    """Replaced by a later revision. Kept so a resume built earlier still explains."""

    @property
    def usable(self) -> bool:
        return self is ApprovalStatus.APPROVED


class GraphSource(StrEnum):
    """Where a row came from. RESUME_ENGINE §5 requires this on every fact."""

    RESUME_IMPORT = "resume_import"
    SKILLS_NOTES_IMPORT = "skills_notes_import"
    """A plain-text notes file, e.g. the skills list carried over from ARIA v1."""
    ENRICHMENT_INTERVIEW = "enrichment_interview"
    MOTIVATIONS_INTERVIEW = "motivations_interview"
    USER_EDIT = "user_edit"
    MERGE = "merge"
    """Created by merging near-duplicate imports; records both originals."""


class ExperienceBucket(StrEnum):
    """The four **disjoint** kinds of time in a career.

    Disjoint matters. Every experience lands in exactly one bucket, so a summary can
    print all four without any month being counted twice. An earlier version had
    "professional" include internships *and* reported internships separately, which
    read as 32 months plus 3 months when the honest answer was 29 plus 3.

    Calendars may still overlap between buckets — a part-time campus job can run
    during a research post — so anything combining buckets unions the spans rather
    than adding the totals (see
    :meth:`aria_core.graph.derive.SkillExperience.claimable_months`).
    """

    FULL_TIME = "full_time"
    """Salaried and contract work. What "years of experience" means by default."""
    PART_TIME = "part_time"
    """Paid work alongside something else: an on-campus job, a graduate or research
    assistantship, a few hours a week. Real employment, and not a full-time year \u2014 a
    two-year assistantship at ten hours a week is not two years of experience. Counted
    when a question asks about work experience generally, or about part-time work."""
    INTERNSHIP = "internship"
    """Real, dated, paid work that a hiring manager reads differently. Never folded
    into the full-time figure unless the question or the candidate asks for it."""
    PROJECT = "project"
    """Academic, personal, open-source and volunteer work. Unpaid research too."""


class ExperienceKind(StrEnum):
    """What kind of role an experience was. Maps to exactly one bucket.

    A new grad with two internships and six class projects has two internships, and a
    resume that implies otherwise is the thing this product exists not to produce.

    ``RESEARCH`` and ``PART_TIME`` overlap in practice and the distinction is the
    candidate's to make: an unpaid lab rotation is ``RESEARCH`` (project time), while a
    paid graduate or research assistantship is ``PART_TIME`` (employment). ARIA never
    guesses which — the importer proposes and the candidate confirms, because "were you
    paid for this?" is a fact about them, not something a parser can read off a resume.
    """

    PROFESSIONAL = "professional"
    CONTRACT = "contract"
    PART_TIME = "part_time"
    INTERNSHIP = "internship"
    RESEARCH = "research"
    VOLUNTEER = "volunteer"

    @property
    def bucket(self) -> ExperienceBucket:
        """The one bucket this kind counts towards.

        Contract work counts as full-time: it is paid professional work, and a
        six-month contract is not an internship. Unpaid research and volunteer time is
        real and dated but is not employment, so it sits with projects.
        """
        return _BUCKETS[self]


_BUCKETS: dict[ExperienceKind, ExperienceBucket] = {
    ExperienceKind.PROFESSIONAL: ExperienceBucket.FULL_TIME,
    ExperienceKind.CONTRACT: ExperienceBucket.FULL_TIME,
    ExperienceKind.PART_TIME: ExperienceBucket.PART_TIME,
    ExperienceKind.INTERNSHIP: ExperienceBucket.INTERNSHIP,
    ExperienceKind.RESEARCH: ExperienceBucket.PROJECT,
    ExperienceKind.VOLUNTEER: ExperienceBucket.PROJECT,
}


class ProjectKind(StrEnum):
    ACADEMIC = "academic"
    PERSONAL = "personal"
    RESEARCH = "research"
    OPEN_SOURCE = "open_source"


class ProfileFieldKind(StrEnum):
    """Contact and identity fields, imported from a resume header as drafts.

    There is deliberately **no street-address kind**. SECURITY.md §3 classifies a
    home address as S2, nothing in the product needs one, and a form that asks for
    one goes to the user — the same treatment as SSN and date of birth. A test
    asserts no such kind appears here.
    """

    FULL_NAME = "full_name"
    PREFERRED_NAME = "preferred_name"
    EMAIL = "email"
    PHONE = "phone"
    LOCATION_CITY = "location_city"
    LOCATION_REGION = "location_region"
    """State, province or equivalent."""
    LOCATION_COUNTRY = "location_country"
    LINKEDIN_URL = "linkedin_url"
    GITHUB_URL = "github_url"
    PORTFOLIO_URL = "portfolio_url"
    OTHER_URL = "other_url"


class AchievementKind(StrEnum):
    AWARD = "award"
    PUBLICATION = "publication"
    CERTIFICATION = "certification"
    PATENT = "patent"
    TALK = "talk"


class MotivationKind(StrEnum):
    """The candidate's own reasons, used by the Answer Engine (PRODUCT_SPEC §3.4)."""

    PROBLEM_TYPE = "problem_type"
    """"What problems do you like working on?\""""
    WANTS_NEXT = "wants_next"
    TEAM_PREFERENCE = "team_preference"
    COMPANY_PREFERENCE = "company_preference"
    WORKING_STYLE = "working_style"
    AVOID = "avoid"
    """What they do not want. Just as useful, and never guessed."""


class Theme(StrEnum):
    """What a fact demonstrates — the ``DEMONSTRATES`` edge in RESUME_ENGINE §5.

    Used to spread evidence across a resume rather than repeating one kind of
    achievement, and to answer "tell me about a time you..." from real facts.
    """

    SCALE = "scale"
    PERFORMANCE = "performance"
    RELIABILITY = "reliability"
    ACCESSIBILITY = "accessibility"
    SECURITY = "security"
    LEADERSHIP = "leadership"
    MENTORING = "mentoring"
    OWNERSHIP = "ownership"
    COLLABORATION = "collaboration"
    ML_RESEARCH = "ml_research"
    DATA = "data"
    AUTOMATION = "automation"
    COST_SAVING = "cost_saving"
    USER_IMPACT = "user_impact"
    MIGRATION = "migration"
