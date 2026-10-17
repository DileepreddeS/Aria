"""6a ATS check (RESUME_ENGINE §9): the printed PDF read back the way an ATS reads it.

Two independent extractors (pypdf, pdfplumber) must read the text cleanly and in order,
and our own
Stage 0 must read the page back into exactly the entries the document laid out: the same
employers, titles, months and bullet counts, every header followed by its own bullets.
Eating our own output is the strongest ATS test available, and it keeps the generator and
the parser honest about each other.

The orphan-line rule of 6d is checked here too, because it needs the page read back line
by line, which only this check does.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from collections.abc import Sequence

import pdfplumber
from pypdf import PdfReader

from aria_core.documents import LayoutDocument
from aria_core.importer.assemble import parse_resume_layout
from aria_core.importer.classify import ClassifiedLine, LineClass, classify
from aria_core.importer.dates import parse_date_range
from aria_core.taxonomy.model import SkillTaxonomy
from aria_document_parser.errors import ParseRefused
from aria_document_parser.pdf import extract_pdf
from aria_resume.document import DocumentEntry, ResumeDocument, SectionKind
from aria_resume.validate import Finding

__all__ = ["check_ats"]

_UNMAPPED = "(cid:"


def _texts(pdf: bytes) -> dict[str, str]:
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        plumber = "\n".join(page.extract_text() or "" for page in document.pages)
    return {
        "pypdf": "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages),
        "pdfplumber": plumber,
    }


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def _months(dates: str, today: dt.date) -> tuple[dt.date | None, dt.date | None]:
    parsed = parse_date_range(dates, today=today)
    if parsed is None or parsed.span is None:
        return None, None
    return parsed.span.start, parsed.span.end


def _entries(document: ResumeDocument, kind: SectionKind) -> list[DocumentEntry]:
    return [entry for section in document.sections if section.kind is kind for entry in section.entries]


def _extraction(texts: dict[str, str], document: ResumeDocument) -> list[Finding]:
    findings: list[Finding] = []
    for name, text in texts.items():
        ligatures = sorted({character for character in text if "\ufb00" <= character <= "\ufb06"})
        if ligatures:
            findings.append(Finding("glyph_integrity", f"{name} reads ligature characters {ligatures}"))
        if _UNMAPPED in text:
            findings.append(Finding("glyph_integrity", f"{name} reads unmapped glyphs, {_UNMAPPED}N)"))
        if document.contact and _collapsed(document.contact_line) not in _collapsed(text):
            findings.append(
                Finding("contact_line", f"{name} does not read the contact line as one run of text")
            )
        lines = {line.strip() for line in text.splitlines()}
        for section in document.sections:
            if section.heading not in lines:
                findings.append(Finding("headings", f"{name} does not read the heading {section.heading}"))
    return findings


def _round_trip(
    layout: LayoutDocument, document: ResumeDocument, taxonomy: SkillTaxonomy, today: dt.date
) -> list[Finding]:
    plan = parse_resume_layout(layout, taxonomy, today=today)
    findings: list[Finding] = []

    roles = _entries(document, SectionKind.EXPERIENCE)
    if len(plan.experiences) != len(roles):
        findings.append(
            Finding(
                "round_trip", f"Stage 0 reads {len(plan.experiences)} roles where the page has {len(roles)}"
            )
        )
    for read, entry in zip(plan.experiences, roles, strict=False):
        start, end = _months(entry.dates, today)
        span = read.span
        if (read.employer, read.title) != (entry.title, entry.detail or ""):
            findings.append(
                Finding("round_trip", f"Stage 0 reads {read.employer!r} / {read.title!r} for {entry.title!r}")
            )
        if span is None or (span.start, span.end) != (start, end):
            findings.append(
                Finding("round_trip", f"Stage 0 reads other months for {entry.title}: {read.date_text!r}")
            )
        if len(read.facts) != len(entry.bullets):
            findings.append(
                Finding(
                    "round_trip",
                    f"Stage 0 reads {len(read.facts)} bullets under {entry.title}, not {len(entry.bullets)}",
                )
            )

    degrees = _entries(document, SectionKind.EDUCATION)
    read_degrees = [(item.school, item.degree) for item in plan.educations]
    if read_degrees != [(entry.title, entry.detail or "") for entry in degrees]:
        findings.append(Finding("round_trip", f"Stage 0 reads the degrees as {read_degrees}"))

    projects = _entries(document, SectionKind.PROJECTS)
    if [project.name for project in plan.projects] != [entry.title for entry in projects]:
        findings.append(
            Finding("round_trip", f"Stage 0 reads the projects as {[p.name for p in plan.projects]}")
        )
    for read_project, entry in zip(plan.projects, projects, strict=False):
        # A project header's tagline is read as a fact on the header's own line.
        bullets = [fact for fact in read_project.facts if fact.source_line != read_project.source_line]
        if len(bullets) != len(entry.bullets):
            findings.append(
                Finding(
                    "round_trip",
                    f"Stage 0 reads {len(bullets)} bullets under {entry.title}, not {len(entry.bullets)}",
                )
            )

    for unparsed in plan.unparsed:
        findings.append(
            Finding("round_trip", f"Stage 0 could not place line {unparsed.first_line}: {unparsed.reason}")
        )
    return findings


def _stream_order(texts: dict[str, str], document: ResumeDocument) -> list[Finding]:
    """Each extractor's text reads the page in order: every heading, then each entry's title,
    dates, second line and bullets, nothing out of place.

    This is what a flat-reading ATS sees. Our Stage 0 reads by geometry and recovers a
    floated date column (RESUME_ENGINE §10); an extractor that reads the text layer in
    stream order does not, and puts every date above the name.
    """
    expected: list[str] = []
    for section in document.sections:
        expected.append(section.heading)
        expected.extend(section.lines)
        for entry in section.entries:
            expected.append(entry.title)
            expected.extend(part for part in (entry.detail, entry.dates, entry.subline) if part)
            expected.extend(" ".join(bullet.text.split()[:4]) for bullet in entry.bullets)
    findings: list[Finding] = []
    for name, text in texts.items():
        stream = _collapsed(text)
        cursor = 0
        for item in expected:
            at = stream.find(_collapsed(item), cursor)
            if at < 0:
                findings.append(Finding("reading_order", f"{name} reads {item!r} out of place"))
                break
            cursor = at + len(_collapsed(item))
    return findings


def _reading_order(classified: Sequence[ClassifiedLine], document: ResumeDocument) -> list[Finding]:
    """Every entry header is followed by its own bullets, before anything else's."""
    entries = [entry for section in document.sections for entry in section.entries]
    headers = [index for index, line in enumerate(classified) if line.kind is LineClass.ENTRY_HEADER]
    findings: list[Finding] = []
    if len(headers) != len(entries):
        return [
            Finding(
                "reading_order", f"{len(headers)} entry headers in the text where the page has {len(entries)}"
            )
        ]
    for position, (index, entry) in enumerate(zip(headers, entries, strict=True)):
        end = headers[position + 1] if position + 1 < len(headers) else len(classified)
        block = classified[index + 1 : end]
        section_break = next(
            (i for i, line in enumerate(block) if line.kind is LineClass.SECTION_HEADER), len(block)
        )
        bullets = sum(1 for line in block[:section_break] if line.kind is LineClass.BULLET)
        if bullets != len(entry.bullets):
            findings.append(
                Finding(
                    "reading_order",
                    f"{entry.title} is followed by {bullets} bullets, not its own {len(entry.bullets)}",
                )
            )
    return findings


def _orphans(classified: Sequence[ClassifiedLine]) -> list[Finding]:
    return [
        Finding("orphan_line", f"a bullet ends with one word on its own line: {line.line.text!r}")
        for line in classified
        if line.kind is LineClass.CONTINUATION and len(line.line.text.split()) == 1
    ]


def check_ats(
    pdf: bytes,
    document: ResumeDocument,
    taxonomy: SkillTaxonomy,
    *,
    today: dt.date,
    keywords: Sequence[str] = (),
) -> list[Finding]:
    """Everything an ATS, or our own Stage 0, would misread; empty when the page reads true.

    ``keywords`` are the JD surface forms of the evidenced must-haves (§9 6a keyword
    coverage); each one missing from the text is a finding.
    """
    texts = _texts(pdf)
    findings = _extraction(texts, document) + _stream_order(texts, document)
    try:
        layout = extract_pdf(pdf).layout
    except ParseRefused as refusal:
        return [*findings, Finding("round_trip", f"Stage 0 refuses the page: {refusal}")]
    classified = classify(layout)
    findings += _round_trip(layout, document, taxonomy, today)
    findings += _reading_order(classified, document)
    findings += _orphans(classified)
    words = _collapsed(texts["pdfplumber"]).lower()
    for keyword in keywords:
        if not re.search(rf"(?<!\w){re.escape(keyword.lower())}(?!\w)", words):
            findings.append(Finding("keywords", f"the JD's {keyword!r} does not appear in the text"))
    return findings
