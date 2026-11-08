"""The engine's own gate: a page with any finding is never returned as ready (DECISIONS 2026-10-23)."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest
from aria_resume.compose.build import build_page
from aria_resume.compose.templates import load_template
from aria_resume.jd.reader import read_jd
from aria_resume.match.embeddings import WordHashEmbedder
from aria_resume.match.matcher import match
from aria_resume.normalize.requirements import normalize
from aria_resume.render.pdf import ChromiumPrinter
from aria_resume.validate.style import Style, load_style

from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotContact, SnapshotEducation
from aria_core.taxonomy.model import SkillTaxonomy

JDS = Path(__file__).parent / "fixtures" / "jds"
TODAY = dt.date(2026, 10, 23)


def _named(snapshot: CandidateSnapshot) -> CandidateSnapshot:
    return snapshot.model_copy(
        update={
            "contact": SnapshotContact(
                full_name="Jordan A. Rivera",
                email="jordan.rivera@example.com",
                phone="(555) 014-2288",
                location_city="Portland",
                location_region="OR",
            ),
            "education": (
                SnapshotEducation(
                    education_id=uuid.UUID(int=7),
                    school="Cascadia State University",
                    degree="M.S.",
                    field_of_study="Computer Science",
                    end_month=dt.date(2026, 12, 1),
                ),
            ),
        }
    )


def _build(
    snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, style: Style, *, portfolio_url: str | None = None
) -> object:
    named = _named(snapshot)
    if portfolio_url is not None:
        contact = named.contact.model_copy(update={"portfolio_url": portfolio_url})
        named = named.model_copy(update={"contact": contact})
    normalized = normalize(
        read_jd((JDS / "larkspur_backend.txt").read_text(encoding="utf-8"), taxonomy), taxonomy
    )
    evidence = match(normalized, named, taxonomy, WordHashEmbedder())
    return build_page(
        normalized,
        evidence,
        named,
        taxonomy,
        template=load_template(evidence.role_type),
        printer=ChromiumPrinter(),
        today=TODAY,
        style=style,
    )


@pytest.mark.chromium
class TestTheEngineGate:
    def test_a_page_every_check_passes_is_ready(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        # The fixture's facts open with allowed verbs, and its vague phrase is in its own fact.
        page = _build(snapshot, taxonomy, load_style())
        assert page.status == "ready", page.findings  # type: ignore[attr-defined]

    def test_a_contact_item_wider_than_a_line_makes_it_needs_review(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        """Never broken inside: printed whole, the page shrunk to fit it, for a person to see."""
        long_url = (
            "https://jordan-rivera-example.dev/writing/notes-on-building-a-resume-engine-that-never-"
            "breaks-a-link-across-two-printed-lines-of-text"
        )
        page = _build(snapshot, taxonomy, load_style(), portfolio_url=long_url)
        assert page.status == "needs_review"  # type: ignore[attr-defined]
        checks = {finding.check for finding in page.findings["layout"]}  # type: ignore[attr-defined]
        assert {"contact_width", "scaled"} <= checks

    def test_a_single_recruiter_finding_makes_it_needs_review(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        # Every bullet must carry a number: the fixture has bullets without one.
        strict = load_style().model_copy(update={"min_quantified_share": 1.0})
        page = _build(snapshot, taxonomy, strict)
        assert page.status == "needs_review"  # type: ignore[attr-defined]
        assert [f.check for f in page.findings["recruiter"]] == ["too_few_quantified"]  # type: ignore[attr-defined]
