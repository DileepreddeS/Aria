"""Turning a section's lines into roles, degrees and projects.

Resumes are not structured documents, and the one piece of structure that survives
PDF extraction is the bullet. So an entry is read as **a head of one to three
non-bullet lines followed by its bullets**, and a new entry starts at the first
non-bullet line after a bullet. That rule is simple enough to predict, which matters
more here than cleverness: a candidate who can see why a role was split in two can
fix it in the approval view, and an importer that is right 95% of the time by
inscrutable means is worse than one that is right 90% of the time legibly.

What this module refuses to decide:

* **Dates it cannot read.** "Summer 2023" is a season, not a month. The role is still
  proposed, with a question (see :class:`aria_core.importer.plan.QuestionKind`).
* **Whether an assistantship was paid.** A paid research assistantship is part-time
  employment; an unpaid lab rotation is project time, and the two count differently
  in every "years of experience" answer. ``docs/DECISIONS.md`` records that ARIA never
  guesses this, so the title ``Research Assistant`` always produces a question.
* **Which half of a line is the employer.** "Carvana — Software Engineer" is obvious
  from the title vocabulary; "Zeta — Praxis" is not, and gets asked about rather than
  assigned by position.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace

from aria_core.graph.status import ExperienceKind, ProjectKind
from aria_core.importer.dates import DateRange, parse_date_range
from aria_core.importer.header import classify_url
from aria_core.importer.lines import SourceLine
from aria_core.importer.metrics import detect_themes, extract_metrics
from aria_core.importer.plan import (
    ImportQuestion,
    ProposedEducation,
    ProposedExperience,
    ProposedFact,
    ProposedProject,
    QuestionKind,
)
from aria_core.importer.sections import SectionKind
from aria_core.importer.skills import SkillFindings, tag_skills
from aria_core.taxonomy.model import SkillTaxonomy

__all__ = ["Entry", "group_entries", "parse_education", "parse_experience", "parse_project"]

#: How many non-bullet lines may make up one entry's head: employer, title, location.
_MAX_HEAD_LINES = 3

#: Separators between an employer and a title on one line.
_SEPARATORS = re.compile(r"\s+[\u2013\u2014|\u2022\u00b7]\s+|\s+-\s+|\s+\u2192\s+")

#: "(client: Carvana)" or "Client - Carvana". A consultancy's resume needs both the
#: employer and the client: the employer is who paid, the client is the work.
_CLIENT = re.compile(r"\(?\s*client\s*[:\u2013-]\s*(?P<client>[^)\n,;]+)\)?", re.IGNORECASE)

_CITY_REGION = re.compile(
    r"(?P<city>[A-Z][A-Za-z.'\u2019-]+(?:\s+[A-Z][A-Za-z.'\u2019-]+){0,3})"
    r"\s*,\s*(?P<region>[A-Z]{2}|[A-Z][A-Za-z.'\u2019-]+(?:\s+[A-Z][A-Za-z.'\u2019-]+){0,2})(?!\w)"
)

#: Words that mark a half of a line as the job rather than the firm.
_TITLE_WORDS = re.compile(
    r"\b(?:engineer|developer|programmer|scientist|analyst|architect|administrator|"
    r"manager|director|lead|intern|internship|trainee|apprentice|assistant|associate|"
    r"consultant|designer|researcher|specialist|technician|coordinator|officer|"
    r"founder|owner|volunteer|tutor|instructor|teacher|fellow|clerk|cashier|"
    r"ambassador|representative|supervisor)\b",
    re.IGNORECASE,
)

#: Words that mark a half of a line as the firm rather than the job.
_EMPLOYER_WORDS = re.compile(
    r"\b(?:inc|llc|ltd|corp|corporation|company|co|technologies|technology|systems|"
    r"solutions|labs|laboratory|group|partners|holdings|university|college|institute|"
    r"school|hospital|bank|foundation|gmbh|pvt|plc|consulting|services)\b\.?",
    re.IGNORECASE,
)

#: Title fragments that decide an experience's kind. Checked in order, so
#: "research intern" is an internship rather than research.
_KIND_WORDS: tuple[tuple[re.Pattern[str], ExperienceKind], ...] = (
    (re.compile(r"\bintern(ship)?\b|\bco-?op\b|\btrainee\b", re.IGNORECASE), ExperienceKind.INTERNSHIP),
    (re.compile(r"\bvolunteer\b|\bpro\s?bono\b", re.IGNORECASE), ExperienceKind.VOLUNTEER),
    (
        re.compile(r"\bcontract(or)?\b|\bfreelance\b|\bconsultant\s*\(contract\)", re.IGNORECASE),
        ExperienceKind.CONTRACT,
    ),
    (re.compile(r"\bpart[\s-]?time\b", re.IGNORECASE), ExperienceKind.PART_TIME),
)

#: Titles where paid-or-unpaid decides the bucket, and only the candidate knows.
_ASSISTANTSHIP = re.compile(
    r"\b(?:research|teaching|graduate|lab(?:oratory)?|student)\s+assistant\b|\b(?:[RT]A|GRA|GTA)\b",
    re.IGNORECASE,
)

_DEGREE = re.compile(
    r"\b(?:b\.?\s?(?:s|a|sc|e|tech|eng)\b\.?|bachelor(?:'?s)?(?:\s+of\s+\w+)?|"
    r"m\.?\s?(?:s|a|sc|eng|tech)\b\.?|master(?:'?s)?(?:\s+of\s+\w+)?|mba|"
    r"ph\.?\s?d\b\.?|doctorate|associate(?:'?s)?\s+degree|diploma|"
    r"high\s+school\s+diploma)\b",
    re.IGNORECASE,
)

_SCHOOL_WORDS = re.compile(
    r"\b(?:university|college|institute|school|academy|polytechnic|iit|nit|bits)\b",
    re.IGNORECASE,
)

_GPA = re.compile(
    r"\b(?:c?gpa|grade\s+point\s+average)\b\s*[:\u2013-]?\s*(?P<gpa>\d(?:\.\d{1,2})?(?:\s*/\s*\d(?:\.\d{1,2})?)?)",
    re.IGNORECASE,
)

_FIELD_OF_STUDY = re.compile(r"\b(?:in|of)\s+(?P<field>[A-Z][\w&.,'\u2019+-]*(?:\s+[\w&.'\u2019+-]+){0,4})")

_COURSEWORK = re.compile(r"^(?:relevant\s+)?(?:coursework|courses|classes)\s*[:\u2013-]\s*", re.IGNORECASE)

#: Separators inside a project head line: "Name | Python, FastAPI" or "Name — desc".
_PROJECT_SPLIT = re.compile(r"\s+[\u2013\u2014|]\s+|\s+-\s+")


@dataclass(frozen=True, slots=True)
class Entry:
    """One raw entry: its head lines and its bullets, before interpretation."""

    head: tuple[SourceLine, ...]
    bullets: tuple[SourceLine, ...]

    @property
    def first_line(self) -> int:
        return self.head[0].number if self.head else self.bullets[0].number

    @property
    def head_text(self) -> str:
        return " | ".join(line.text for line in self.head)


def group_entries(lines: tuple[SourceLine, ...]) -> tuple[Entry, ...]:
    """Group a section's lines into entries: a head, then its bullets.

    A blank line does not end an entry — PDF extraction inserts them freely — but a
    non-bullet line after a bullet does, and so does a fourth head line.
    """
    entries: list[Entry] = []
    head: list[SourceLine] = []
    bullets: list[SourceLine] = []

    def flush() -> None:
        if head or bullets:
            entries.append(Entry(tuple(head), tuple(bullets)))

    for line in lines:
        if line.blank:
            continue
        if line.bullet:
            bullets.append(line)
            continue

        # A non-bullet line: either more head, or the start of the next entry.
        if bullets or len(head) >= _MAX_HEAD_LINES:
            flush()
            head, bullets = [line], []
        else:
            head.append(line)

    flush()
    return tuple(entries)


def _facts_from(
    bullets: tuple[SourceLine, ...], taxonomy: SkillTaxonomy, *, today: dt.date
) -> tuple[ProposedFact, ...]:
    """One draft fact per bullet, with its skills, metrics and themes."""
    facts: list[ProposedFact] = []
    for bullet in bullets:
        if len(bullet.text) < 3:
            continue
        findings: SkillFindings = tag_skills(bullet.text, taxonomy, source_line=bullet.number)
        facts.append(
            ProposedFact(
                text=bullet.text,
                source_line=bullet.number,
                skills=findings.skills,
                metrics=extract_metrics(bullet.text, today=today),
                themes=detect_themes(bullet.text),
            )
        )
    return tuple(facts)


def _find_dates(
    head: tuple[SourceLine, ...], *, today: dt.date
) -> tuple[DateRange | None, SourceLine | None]:
    """The first date range in the head, and the line it was on."""
    for line in head:
        found = parse_date_range(line.text, today=today)
        if found is not None and found.start.year is not None:
            return found, line
    return None, None


def _strip(text: str, *patterns: str) -> str:
    for pattern in patterns:
        if pattern:
            text = text.replace(pattern, " ")
    return " ".join(text.split()).strip(" ,;|\u2013-")


def _split_halves(text: str) -> list[str]:
    parts = [part.strip(" ,;") for part in _SEPARATORS.split(text) if part.strip(" ,;")]
    if len(parts) >= 2:
        return parts
    # No separator: "Software Engineer at Accenture" or "Software Engineer, Accenture".
    for pattern in (r"\s+\bat\b\s+", r"\s*,\s*"):
        split = [part.strip() for part in re.split(pattern, text, maxsplit=1) if part.strip()]
        if len(split) == 2:
            return split
    return [text.strip()] if text.strip() else []


def _assign_employer_and_title(
    halves: list[str], source_line: int
) -> tuple[str, str, tuple[ImportQuestion, ...]]:
    """Decide which half is the employer and which is the title, or ask.

    Position is not evidence: resumes write both orders about equally often. The title
    vocabulary is evidence, and so is a company suffix. With neither, this asks.
    """
    if not halves:
        return "", "", ()
    if len(halves) == 1:
        single = halves[0]
        if _TITLE_WORDS.search(single):
            return "", single, ()
        return single, "", ()

    first, second = halves[0], halves[1]
    first_title, second_title = bool(_TITLE_WORDS.search(first)), bool(_TITLE_WORDS.search(second))
    first_firm, second_firm = bool(_EMPLOYER_WORDS.search(first)), bool(_EMPLOYER_WORDS.search(second))

    if second_title and not first_title:
        return first, second, ()
    if first_title and not second_title:
        return second, first, ()
    if first_firm and not second_firm:
        return first, second, ()
    if second_firm and not first_firm:
        return second, first, ()

    question = ImportQuestion(
        kind=QuestionKind.EMPLOYER_OR_TITLE,
        prompt=f"Which of these is the employer: {first!r} or {second!r}?",
        source_lines=(source_line,),
        detail=(
            "Nothing in the line says which half is the job and which is the company, "
            f"so I have guessed employer={first!r}, title={second!r} for you to correct."
        ),
    )
    return first, second, (question,)


def _propose_kind(
    title: str, employer: str, source_line: int
) -> tuple[ExperienceKind, tuple[ImportQuestion, ...]]:
    """The kind of role, and a question when the resume cannot settle it."""
    haystack = f"{title} {employer}"
    for pattern, kind in _KIND_WORDS:
        if pattern.search(haystack):
            return kind, ()

    if _ASSISTANTSHIP.search(haystack):
        return ExperienceKind.RESEARCH, (
            ImportQuestion(
                kind=QuestionKind.PAID_OR_UNPAID,
                prompt=f"Were you paid for {title or employer!r}?",
                source_lines=(source_line,),
                detail=(
                    "A paid assistantship is part-time employment and counts towards "
                    "general work experience; an unpaid one is project time and does not. "
                    "I have proposed project time, which is the smaller claim."
                ),
            ),
        )
    return ExperienceKind.PROFESSIONAL, ()


def _location(head: tuple[SourceLine, ...], used: str) -> tuple[str | None, str | None]:
    for line in head:
        match = _CITY_REGION.search(line.text)
        if match and match.group(0) not in used:
            return match.group("city"), match.group("region")
    return None, None


def _missing_dates_question(
    dates: DateRange | None, label: str, source_line: int
) -> tuple[ImportQuestion, ...]:
    problem = dates.problem if dates is not None else "I could not find any dates for this"
    return (
        ImportQuestion(
            kind=QuestionKind.MISSING_DATES,
            prompt=f"When did {label or 'this role'} start and end?",
            source_lines=(source_line,),
            detail=(
                f"{problem}. Months decide every years-of-experience figure, so I will "
                "not assume one \u2014 the entry is held back until you say."
            ),
        ),
    )


def parse_experience(entry: Entry, taxonomy: SkillTaxonomy, *, today: dt.date) -> ProposedExperience:
    """Read one role out of an entry."""
    dates, date_line = _find_dates(entry.head, today=today)
    date_text = dates.text if dates is not None else ""
    span = dates.span if dates is not None else None

    client_match = next((_CLIENT.search(line.text) for line in entry.head if _CLIENT.search(line.text)), None)
    client = client_match.group("client").strip() if client_match else None

    location_city, location_region = _location(entry.head, used=date_text)
    location_text = ", ".join(part for part in (location_city or "", location_region or "") if part)

    # Head lines with the dates, location and client taken out. A resume often puts
    # the dates on a line of their own, so the line that carried them is usually not
    # the line that carries the employer — reading both off the same line was how an
    # earlier version lost the employer of every role whose dates were below it.
    client_text = client_match.group(0) if client_match else ""
    fragments = [(line, _strip(line.text, date_text, client_text, location_text)) for line in entry.head]
    fragments = [(line, text) for line, text in fragments if text]
    if not fragments:
        return ProposedExperience(
            employer="",
            title="",
            source_line=(date_line or entry.head[0] if entry.head else entry.bullets[0]).number,
            span=span,
            date_text=date_text,
            dates=dates,
            facts=_facts_from(entry.bullets, taxonomy, today=today),
        )

    primary, text = fragments[0]
    halves = _split_halves(text)

    # A head whose second line carries the other half: "Accenture" on one line,
    # "Software Engineer | Jun 2024 - Present" on the next.
    if len(halves) == 1 and len(fragments) > 1:
        halves.append(fragments[1][1])

    employer, title, questions = _assign_employer_and_title(halves, primary.number)
    kind, kind_questions = _propose_kind(title, employer, primary.number)
    questions = questions + kind_questions

    if span is None:
        label = title or employer
        questions = questions + _missing_dates_question(dates, label, primary.number)

    return ProposedExperience(
        employer=employer[:200],
        title=title[:200],
        source_line=primary.number,
        kind=kind,
        client=client[:200] if client else None,
        location_city=location_city,
        location_region=location_region,
        span=span,
        date_text=date_text,
        dates=dates,
        facts=_facts_from(entry.bullets, taxonomy, today=today),
        questions=questions,
    )


def parse_education(entry: Entry, taxonomy: SkillTaxonomy, *, today: dt.date) -> ProposedEducation:
    """Read one education entry out of an entry.

    Unlike a role, an undated degree is still worth storing: the education table's
    months are nullable, because "BS Computer Science, Example University" is a
    complete and useful fact without them.

    Returns an entry with an empty school *and* degree when the lines name neither.
    That is the signal to the caller that this was not an education entry at all.
    """
    dates, _ = _find_dates(entry.head, today=today)
    date_text = dates.text if dates is not None else ""
    span = dates.span if dates is not None else None

    school = ""
    degree = ""
    gpa: str | None = None
    recognised = False
    for line in entry.head:
        if not school and _SCHOOL_WORDS.search(line.text):
            school = _strip(line.text, date_text)
            recognised = True
        degree_match = _DEGREE.search(line.text)
        if not degree and degree_match:
            degree = line.text[degree_match.start() :]
            recognised = True
        gpa_match = _GPA.search(line.text)
        if gpa is None and gpa_match:
            gpa = gpa_match.group("gpa").replace(" ", "")

    # Neither a school nor a degree: this is not an education entry, whatever else
    # is on the lines. A stray "GPA: 3.8/4.0" on its own used to become a row with an
    # empty school and a degree of "GPA:" -- a claim about nothing, waiting in the
    # approval queue. The caller reports the lines instead.
    if not recognised:
        return ProposedEducation(school="", degree="", source_line=entry.first_line, gpa=gpa)

    # A single line holding both: "BS Computer Science, Example University, 2024".
    if school and degree and school.startswith(degree[: max(len(degree) - 1, 1)]):
        parts = [part.strip() for part in school.split(",")]
        school_parts = [part for part in parts if _SCHOOL_WORDS.search(part)]
        if school_parts:
            school = school_parts[0]
            degree = ", ".join(part for part in parts if part not in school_parts)

    if degree:
        degree = _strip(degree, date_text, gpa or "")
        degree = re.sub(_GPA, "", degree).strip(" ,;|")
    if not school:
        school = _strip(entry.head[0].text if entry.head else "", date_text, degree, gpa or "")

    field_of_study: str | None = None
    field_match = _FIELD_OF_STUDY.search(degree)
    if field_match:
        field_of_study = field_match.group("field").strip(" ,;")
    elif "," in degree:
        head, _, tail = degree.partition(",")
        if _DEGREE.search(head) and tail.strip():
            field_of_study = tail.strip(" ,;")

    coursework: str | None = None
    facts: list[SourceLine] = []
    for bullet in entry.bullets:
        if _COURSEWORK.match(bullet.text):
            coursework = _COURSEWORK.sub("", bullet.text).strip()
        else:
            facts.append(bullet)
    # Coursework is often a plain line under the degree rather than a bullet.
    coursework_lines = [line for line in entry.head if _COURSEWORK.match(line.text)]
    if coursework is None and coursework_lines:
        coursework = _COURSEWORK.sub("", coursework_lines[0].text).strip()

    # "Westbrook Institute, Pune, India": the school is the part that names a school,
    # and what follows it is where the school is.
    location_city: str | None = None
    location_region: str | None = None
    if "," in school:
        parts = [part.strip() for part in school.split(",")]
        named = next((index for index, part in enumerate(parts) if _SCHOOL_WORDS.search(part)), 0)
        remainder = ", ".join(parts[named + 1 :])
        place = _CITY_REGION.fullmatch(remainder) if remainder else None
        if place is not None:
            school = parts[named]
            location_city, location_region = place.group("city"), place.group("region")
    if location_city is None:
        # Never from a coursework line ("Deep Learning, Artificial Intelligence" is not a
        # city and a region), nor from the lines the school and degree came from.
        others = tuple(
            line
            for line in entry.head
            if not _COURSEWORK.match(line.text)
            and not _SCHOOL_WORDS.search(line.text)
            and not _DEGREE.search(line.text)
        )
        location_city, location_region = _location(others, used=date_text)

    return ProposedEducation(
        school=school[:200],
        degree=degree[:200],
        source_line=entry.first_line,
        field_of_study=field_of_study[:200] if field_of_study else None,
        gpa=gpa[:20] if gpa else None,
        location_city=location_city,
        location_region=location_region,
        span=span,
        date_text=date_text,
        coursework=coursework,
        facts=_facts_from(tuple(facts), taxonomy, today=today),
    )


def parse_project(
    entry: Entry,
    taxonomy: SkillTaxonomy,
    *,
    today: dt.date,
    section: SectionKind = SectionKind.PROJECTS,
    heading: str = "",
) -> ProposedProject:
    """Read one project out of an entry.

    The kind is a proposal: "academic" when the heading says so, "open source" when
    the entry carries a repository link, "personal" otherwise. All three are drafts,
    and the distinction only affects which bucket the months fall in — never whether
    the work happened.
    """
    dates, _ = _find_dates(entry.head, today=today)
    date_text = dates.text if dates is not None else ""

    primary = entry.head[0] if entry.head else entry.bullets[0]
    text = _strip(primary.text, date_text)

    url: str | None = None
    repository_url: str | None = None
    for line in entry.head:
        for token in line.text.split():
            if "." not in token and "://" not in token:
                continue
            classified = classify_url(token.strip("(),;"))
            if classified is None:
                continue
            _, cleaned = classified
            if "github.com" in cleaned or "gitlab.com" in cleaned:
                repository_url = repository_url or cleaned
            else:
                url = url or cleaned
            text = text.replace(token, " ")

    parts = [part.strip() for part in _PROJECT_SPLIT.split(text) if part.strip()]
    name = (parts[0] if parts else text).strip(" ,;|")

    folded = f"{heading} {entry.head_text}".lower()
    if "academic" in folded or "course" in folded or "class project" in folded:
        kind = ProjectKind.ACADEMIC
    elif "research" in folded:
        kind = ProjectKind.RESEARCH
    elif repository_url:
        kind = ProjectKind.OPEN_SOURCE
    else:
        kind = ProjectKind.PERSONAL

    # A project's description, when it is on the head line rather than in a bullet,
    # is still a claim and is imported as one.
    description_facts: tuple[ProposedFact, ...] = ()
    if len(parts) > 1:
        description = " ".join(parts[1:])
        if len(description) > 20:
            description_facts = _facts_from(
                (replace(primary, text=description, bullet=True),), taxonomy, today=today
            )

    return ProposedProject(
        name=name[:200],
        source_line=primary.number,
        kind=kind,
        url=url,
        repository_url=repository_url,
        span=dates.span if dates is not None else None,
        date_text=date_text,
        facts=description_facts + _facts_from(entry.bullets, taxonomy, today=today),
    )
