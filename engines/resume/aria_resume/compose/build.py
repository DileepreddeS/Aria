"""One page, start to finish: plan, fit, write, print, and every check (RESUME_ENGINE §8-§10).

The engine never returns a page as ready while any check has a finding (DECISIONS 2026-10-23):
6b truth, 6c recruiter (every rule, not only banned words), 6d layout and 6a ATS read-back.
Such a page comes back as ``needs_review`` with its findings, for a person to see.

With no model client the page is the facts' own words, which is what CI prints; with one,
:func:`aria_resume.compose.writer.write_page` writes each slot within the line budget the
fitted plan gave it, and the page is printed and checked again as written.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

from aria_core.schemas.snapshot import CandidateSnapshot
from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.compose.fallback import fallback_document
from aria_resume.compose.fit import fit_to_page
from aria_resume.compose.line_budget import Page
from aria_resume.compose.planner import plan
from aria_resume.compose.templates import RoleTemplate
from aria_resume.compose.writer import WriterStats, write_page
from aria_resume.document import ResumeDocument
from aria_resume.llm import LlmClient
from aria_resume.render.html import render_html
from aria_resume.render.links import add_links
from aria_resume.render.pdf import PdfPrinter
from aria_resume.schemas import EvidenceMap, NormalizedJD, RequirementKind, ResumePlan
from aria_resume.validate import Finding
from aria_resume.validate.ats import check_ats
from aria_resume.validate.layout import check_layout
from aria_resume.validate.recruiter import check_recruiter
from aria_resume.validate.style import Style, load_style
from aria_resume.validate.truth import check_truth

__all__ = ["PageResult", "build_page"]

_SKILL_KINDS = (RequirementKind.SKILL, RequirementKind.DOMAIN, RequirementKind.SOFT)


@dataclass
class PageResult:
    status: Literal["ready", "needs_review"]
    document: ResumeDocument
    pdf: bytes
    plan: ResumePlan
    fit_rounds: int
    #: "truth", "recruiter", "layout", "ats": each check's findings, empty when it passes.
    findings: dict[str, list[Finding]]
    #: None when no model wrote: every bullet is its fact's own words.
    writer: WriterStats | None = None
    notes: list[str] = field(default_factory=list)


def build_page(
    normalized: NormalizedJD,
    evidence: EvidenceMap,
    snapshot: CandidateSnapshot,
    taxonomy: SkillTaxonomy,
    *,
    template: RoleTemplate,
    printer: PdfPrinter,
    today: dt.date,
    llm: LlmClient | None = None,
    style: Style | None = None,
) -> PageResult:
    style = style or load_style()

    def build(page: Page) -> tuple[ResumePlan, bytes]:
        planned = plan(normalized, evidence, snapshot, taxonomy, template=template, page=page)
        document = fallback_document(planned, snapshot)
        return planned, add_links(asyncio.run(printer.print_pdf(render_html(document))), document)

    fitted = fit_to_page(build)
    stats: WriterStats | None = None
    if llm is None:
        document, pdf = fallback_document(fitted.plan, snapshot), fitted.pdf
    else:
        written, stats = asyncio.run(
            write_page(fitted.plan, snapshot, normalized, evidence, taxonomy, style, llm)
        )
        document = fallback_document(fitted.plan, snapshot, written)
        pdf = add_links(asyncio.run(printer.print_pdf(render_html(document))), document)

    keywords = [
        requirement.surface_forms[0]
        for requirement in normalized.requirements
        if requirement.must_have and requirement.kind in _SKILL_KINDS and requirement.id in evidence.strong
    ]
    layout = check_layout(pdf, document)
    if not fitted.fits:
        layout.append(Finding("one_page", "the page did not fit on one page after fitting"))
    findings = {
        "truth": check_truth(document, snapshot, taxonomy),
        "recruiter": check_recruiter(document, snapshot, style),
        "layout": layout,
        "ats": check_ats(pdf, document, taxonomy, today=today, keywords=keywords),
    }
    return PageResult(
        status="ready" if not any(findings.values()) else "needs_review",
        document=document,
        pdf=pdf,
        plan=fitted.plan,
        fit_rounds=fitted.rounds,
        findings=findings,
        writer=stats,
        notes=list(stats.notes) if stats else [],
    )
