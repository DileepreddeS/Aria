"""The Resume Engine's public API (RESUME_ENGINE \u00a710): build, explain, and the gap report.

The engine stays pure: it never reads the database. The caller builds the snapshot from
approved rows, stores the result, and names the PDF's file; :func:`file_name` gives the only
name a resume is ever downloaded as, ``First_Last_Resume.pdf``, never from the posting or the
company (\u00a710).

``ResumeResult.status`` is one of the spec's three (DECISIONS 2026-11-08):

* ``no_fit`` when the matcher's fit decision is ``no_fit``, whatever the page: the page is
  still built, so the candidate can see why;
* ``ok`` when the page passed every check (the engine's own gate, ``ready``);
* ``needs_review`` otherwise, with its findings.

Every result carries what reproduces and explains it: the plan, each bullet with its fact
ids, the evidence map, the validation report, the versions of everything that shaped it, and
an input hash over the posting, the snapshot and those versions.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from aria_core.schemas.snapshot import CandidateSnapshot
from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.compose.build import PageResult, build_page
from aria_resume.compose.summary import SUMMARY_VERSION
from aria_resume.compose.templates import load_template
from aria_resume.compose.writer import WRITER_VERSION
from aria_resume.jd.model_step import INSTRUCTIONS_VERSION, read_jd_with_model
from aria_resume.jd.reader import read_jd
from aria_resume.llm import LlmClient
from aria_resume.match.embeddings import Embedder
from aria_resume.match.matcher import match
from aria_resume.normalize.requirements import normalize
from aria_resume.render.html import TEMPLATE_VERSION
from aria_resume.render.pdf import PdfPrinter
from aria_resume.schemas import EvidenceMap, FitDecision, Gap, NormalizedJD
from aria_resume.validate.implied import load_implied_skills
from aria_resume.validate.six_second import SIX_SECOND_VERSION
from aria_resume.validate.style import Style, load_style

__all__ = ["GapReport", "ResumeOptions", "ResumeResult", "build_resume", "explain", "file_name", "gap_report"]


@dataclass(frozen=True, slots=True)
class ResumeOptions:
    #: The day the page is dated and checked for (an ongoing role's months run to it).
    today: dt.date
    style: Style | None = None


@dataclass(frozen=True, slots=True)
class GapReport:
    """What the posting asks for that the evidence meets weakly or not at all."""

    coverage: float
    fit: FitDecision
    missing: tuple[Gap, ...]
    light: tuple[Gap, ...]


@dataclass
class ResumeResult:
    status: Literal["ok", "needs_review", "no_fit"]
    page: PageResult
    normalized: NormalizedJD
    evidence: EvidenceMap
    versions: dict[str, str]
    input_hash: str

    @property
    def pdf(self) -> bytes:
        return self.page.pdf

    def artifacts(self) -> dict[str, Any]:
        """The spec's artifacts, as JSON-ready values: what is stored beside the PDF."""
        document = self.page.document
        return {
            "plan": self.page.plan.model_dump(mode="json"),
            # What the evidence map's requirement ids name, for "why is this bullet here?".
            "requirements": [
                {"id": r.id, "label": r.surface_forms[0], "kind": r.kind.value, "must_have": r.must_have}
                for r in self.normalized.requirements
            ],
            "bullets": [
                {"text": bullet.text, "fact_ids": [str(fact_id) for fact_id in bullet.fact_ids]}
                for section in document.sections
                for entry in section.entries
                for bullet in entry.bullets
            ],
            "evidence_map": self.evidence.model_dump(mode="json"),
            "validation_report": {
                "status": self.page.status,
                "findings": {
                    check: [{"check": finding.check, "detail": finding.detail} for finding in findings]
                    for check, findings in self.page.findings.items()
                },
                "notes": list(self.page.notes),
            },
            "versions": dict(self.versions),
            "input_hash": self.input_hash,
        }


def file_name(snapshot: CandidateSnapshot) -> str:
    """``First_Last_Resume.pdf``: the approved name's first and last words, nothing else."""
    words = [re.sub(r"[^A-Za-z0-9-]", "", word) for word in (snapshot.contact.full_name or "").split()]
    words = [word for word in words if word]
    stem = "_".join([words[0], words[-1]] if len(words) > 1 else words or ["Resume"])
    return f"{stem}_Resume.pdf" if words else "Resume.pdf"


def _versions(
    taxonomy: SkillTaxonomy, style: Style, normalized: NormalizedJD, evidence: EvidenceMap, model: bool
) -> dict[str, str]:
    return {
        "taxonomy": taxonomy.version,
        "style": style.version,
        "implied_skills": load_implied_skills(taxonomy).version,
        "template": f"{evidence.role_type.value}@{load_template(evidence.role_type).version}",
        "render_template": TEMPLATE_VERSION,
        "embedder": evidence.embedder,
        "stage1": f"rules+model (instructions {INSTRUCTIONS_VERSION})" if model else "rules",
        "writer": WRITER_VERSION if model else "none (facts' own words)",
        "summary": SUMMARY_VERSION if model else "none",
        "six_second": SIX_SECOND_VERSION if model else "not run",
    }


def _hash(jd_text: str, snapshot: CandidateSnapshot, versions: dict[str, str], today: dt.date) -> str:
    material = json.dumps(
        {
            "jd": jd_text,
            # What the snapshot says, not when it was read: taken_at differs on every read of
            # the same graph, and as_of already fixes the day its figures are closed at.
            "snapshot": snapshot.model_dump(mode="json", exclude={"taken_at"}),
            "versions": versions,
            "today": today.isoformat(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_resume(
    jd_text: str,
    snapshot: CandidateSnapshot,
    options: ResumeOptions,
    llm: LlmClient | None,
    *,
    taxonomy: SkillTaxonomy,
    embedder: Embedder,
    printer: PdfPrinter,
) -> ResumeResult:
    """One posting to one checked page. ``llm`` None prints the facts' own words, as CI does.

    Synchronous, as :func:`build_page` is (it prints through Chromium with ``asyncio.run``):
    an async caller runs it in a thread (``asyncio.to_thread``).
    """
    style = options.style or load_style()
    parsed = (
        read_jd(jd_text, taxonomy)
        if llm is None
        else asyncio.run(read_jd_with_model(jd_text, taxonomy, llm, source_ref="resume"))
    )
    normalized = normalize(parsed, taxonomy)
    evidence = match(normalized, snapshot, taxonomy, embedder)
    page = build_page(
        normalized,
        evidence,
        snapshot,
        taxonomy,
        template=load_template(evidence.role_type),
        printer=printer,
        today=options.today,
        llm=llm,
        style=style,
    )
    status: Literal["ok", "needs_review", "no_fit"]
    if evidence.fit_decision is FitDecision.NO_FIT:
        status = "no_fit"
    else:
        status = "ok" if page.status == "ready" else "needs_review"
    versions = _versions(taxonomy, style, normalized, evidence, llm is not None)
    return ResumeResult(
        status=status,
        page=page,
        normalized=normalized,
        evidence=evidence,
        versions=versions,
        input_hash=_hash(jd_text, snapshot, versions, options.today),
    )


def explain(result: ResumeResult) -> EvidenceMap:
    """Why each bullet is there: which facts prove which requirements, and how well."""
    return result.evidence


def gap_report(result: ResumeResult) -> GapReport:
    evidence = result.evidence
    return GapReport(
        coverage=evidence.coverage,
        fit=evidence.fit_decision,
        missing=evidence.missing,
        light=evidence.light_gaps,
    )
