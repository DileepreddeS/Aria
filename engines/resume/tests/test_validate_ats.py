"""6a ATS check: the printed page read back by two extractors and by our own Stage 0.

Each test breaks the page one known way and requires the check to say so. They print
through Playwright's Chromium (`uv run playwright install chromium`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from functools import cache

import pytest
from aria_resume.document import (
    ContactItem,
    DocumentBullet,
    DocumentEntry,
    DocumentSection,
    ResumeDocument,
    SectionKind,
)
from aria_resume.render.html import render_html
from aria_resume.render.links import add_links
from aria_resume.render.pdf import ChromiumPrinter
from aria_resume.validate import Finding
from aria_resume.validate.ats import check_ats

from aria_core.taxonomy.loader import load_default_taxonomy

pytestmark = pytest.mark.chromium

EN = "\u2013"
TODAY = dt.date(2026, 10, 16)


def _document(*, extra_bullet: bool = False) -> ResumeDocument:
    nordvale = [
        DocumentBullet(text="Cut p95 API latency by 30% with Redis caching, an efficient fix for the office"),
        DocumentBullet(text="Built 15+ React screens for the checkout platform"),
    ]
    if extra_bullet:
        nordvale.append(DocumentBullet(text="Wrote the on-call runbook adopted by three teams"))
    return ResumeDocument(
        name="Jordan A. Rivera",
        contact=(
            ContactItem(text="jordan.rivera@example.com", url="mailto:jordan.rivera@example.com"),
            ContactItem(text="(555) 014-2288"),
            ContactItem(text="Portland, OR"),
        ),
        sections=(
            DocumentSection(
                kind=SectionKind.SUMMARY, lines=("Software engineer with three years of backend work.",)
            ),
            DocumentSection(
                kind=SectionKind.EXPERIENCE,
                entries=(
                    DocumentEntry(
                        title="Nordvale Consulting",
                        detail="Software Engineer",
                        dates=f"Feb 2024 {EN} Jul 2025",
                        subline="Pune, India",
                        bullets=tuple(nordvale),
                    ),
                    # The bold employer is the shorter half: the header style Stage 0 once misread.
                    DocumentEntry(
                        title="Brightfold Systems",
                        detail="Associate Software Engineer",
                        dates=f"Jul 2022 {EN} Dec 2023",
                        bullets=(
                            DocumentBullet(
                                text="Shipped 8 full-stack applications with Node.js and PostgreSQL"
                            ),
                        ),
                    ),
                ),
            ),
            DocumentSection(
                kind=SectionKind.PROJECTS,
                entries=(
                    DocumentEntry(
                        title="VERA-RAG",
                        detail="Self-Verifying Adaptive RAG System",
                        bullets=(
                            DocumentBullet(text="Built a closed-loop RAG system with hybrid retrieval"),
                        ),
                    ),
                ),
            ),
            DocumentSection(
                kind=SectionKind.EDUCATION,
                entries=(
                    DocumentEntry(
                        title="Cascadia State University", detail="M.S. Computer Science", dates="Dec 2026"
                    ),
                ),
            ),
            DocumentSection(kind=SectionKind.SKILLS, lines=("Languages: Python, TypeScript, SQL",)),
        ),
    )


def _print(html: str, document: ResumeDocument) -> bytes:
    return add_links(asyncio.run(ChromiumPrinter().print_pdf(html)), document)


@cache
def _clean() -> bytes:
    return _print(render_html(_document()), _document())


def _check(pdf: bytes, document: ResumeDocument | None = None, **kwargs: object) -> list[Finding]:
    return check_ats(pdf, document or _document(), load_default_taxonomy(), today=TODAY, **kwargs)  # type: ignore[arg-type]


def _checks(findings: list[Finding]) -> set[str]:
    return {finding.check for finding in findings}


def test_our_page_reads_back_exactly() -> None:
    assert _check(_clean()) == []


def test_a_floated_date_column_is_out_of_order_in_the_stream() -> None:
    html = render_html(_document()).replace(".when {\n  display: inline-block;", ".when {\n  float: right;")
    findings = _check(_print(html, _document()))
    assert any(
        finding.check == "reading_order" and finding.detail.startswith("pypdf") for finding in findings
    )


def test_ligatures_left_on_are_caught() -> None:
    html = (
        render_html(_document())
        .replace('font-feature-settings: "liga" 0, "clig" 0, "dlig" 0;', "")
        .replace("font-variant-ligatures: none;", "")
    )
    assert "glyph_integrity" in _checks(_check(_print(html, _document())))


def test_a_page_that_does_not_say_what_the_plan_said_fails_the_round_trip() -> None:
    # The plan has three bullets under Nordvale; the page printed two.
    findings = _check(_clean(), _document(extra_bullet=True))
    assert any(finding.check == "round_trip" and "Nordvale" in finding.detail for finding in findings)


def test_a_missing_keyword_is_named() -> None:
    findings = _check(_clean(), keywords=("PostgreSQL", "Kubernetes"))
    assert [finding.detail for finding in findings] == ["the JD's 'Kubernetes' does not appear in the text"]
