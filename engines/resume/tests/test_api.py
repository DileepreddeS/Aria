"""The engine's public API (RESUME_ENGINE \u00a710): status, artifacts, file name, reproducibility."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import aria_resume.api as api
import pytest
from aria_resume.api import ResumeOptions, build_resume, explain, file_name, gap_report
from aria_resume.match.embeddings import WordHashEmbedder
from aria_resume.render.pdf import ChromiumPrinter
from aria_resume.schemas import FitDecision

from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotContact, SnapshotEducation
from aria_core.taxonomy.model import SkillTaxonomy

JDS = Path(__file__).parent / "fixtures" / "jds"
TODAY = dt.date(2026, 10, 23)


def _named(snapshot: CandidateSnapshot, name: str = "Jordan A. Rivera") -> CandidateSnapshot:
    degree = SnapshotEducation(
        education_id=uuid.UUID(int=7),
        school="Cascadia State University",
        degree="M.S.",
        field_of_study="Computer Science",
        end_month=dt.date(2026, 12, 1),
    )
    contact = SnapshotContact(full_name=name, email="jordan.rivera@example.com", phone="(555) 014-2288")
    return snapshot.model_copy(update={"contact": contact, "education": (degree,)})


def _jd(name: str = "larkspur_backend") -> str:
    return (JDS / f"{name}.txt").read_text(encoding="utf-8")


class TestTheFileName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Jordan A. Rivera", "Jordan_Rivera_Resume.pdf"),
            ("Jordan Rivera", "Jordan_Rivera_Resume.pdf"),
            ("Jordan", "Jordan_Resume.pdf"),
            ("  ", "Resume.pdf"),
            ("Jordan ../../etc/Rivera", "Jordan_etcRivera_Resume.pdf"),
        ],
    )
    def test_first_and_last_words_only(self, snapshot: CandidateSnapshot, name: str, expected: str) -> None:
        assert file_name(_named(snapshot, name)) == expected


class _Page:
    def __init__(self, status: str) -> None:
        self.status = status
        self.findings: dict[str, list[Any]] = {"truth": []}
        self.notes: list[str] = []


def _with_page(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    monkeypatch.setattr(api, "build_page", lambda *args, **kwargs: _Page(status))


def _build(snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, jd: str = "larkspur_backend") -> Any:
    return build_resume(
        _jd(jd),
        _named(snapshot),
        ResumeOptions(today=TODAY),
        None,
        taxonomy=taxonomy,
        embedder=WordHashEmbedder(),
        printer=ChromiumPrinter(),
    )


class TestTheStatus:
    def test_a_ready_page_is_ok(
        self, monkeypatch: pytest.MonkeyPatch, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        _with_page(monkeypatch, "ready")
        result = _build(snapshot, taxonomy)
        assert result.evidence.fit_decision is not FitDecision.NO_FIT
        assert result.status == "ok"

    def test_a_page_with_findings_needs_review(
        self, monkeypatch: pytest.MonkeyPatch, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        _with_page(monkeypatch, "needs_review")
        assert _build(snapshot, taxonomy).status == "needs_review"

    def test_no_fit_wins_over_a_ready_page(
        self, monkeypatch: pytest.MonkeyPatch, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        # The synthetic frontend posting is a no-fit for the backend fixture (the eval's own result).
        _with_page(monkeypatch, "ready")
        result = _build(snapshot, taxonomy, "tidewell_frontend")
        assert result.evidence.fit_decision is FitDecision.NO_FIT
        assert result.status == "no_fit"

    def test_explain_and_gaps_are_the_evidence_map_s(
        self, monkeypatch: pytest.MonkeyPatch, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        _with_page(monkeypatch, "ready")
        result = _build(snapshot, taxonomy)
        assert explain(result) is result.evidence
        report = gap_report(result)
        assert (report.missing, report.light, report.coverage) == (
            result.evidence.missing,
            result.evidence.light_gaps,
            result.evidence.coverage,
        )

    def test_the_hash_moves_with_the_posting_and_the_day(
        self, monkeypatch: pytest.MonkeyPatch, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        _with_page(monkeypatch, "ready")
        first, again = _build(snapshot, taxonomy), _build(snapshot, taxonomy)
        assert first.input_hash == again.input_hash
        other = build_resume(
            _jd() + "\nAlso: Rust.",
            _named(snapshot),
            ResumeOptions(today=TODAY),
            None,
            taxonomy=taxonomy,
            embedder=WordHashEmbedder(),
            printer=ChromiumPrinter(),
        )
        later = build_resume(
            _jd(),
            _named(snapshot),
            ResumeOptions(today=TODAY + dt.timedelta(days=1)),
            None,
            taxonomy=taxonomy,
            embedder=WordHashEmbedder(),
            printer=ChromiumPrinter(),
        )
        assert len({first.input_hash, other.input_hash, later.input_hash}) == 3

    def test_the_same_graph_read_twice_hashes_alike(
        self, monkeypatch: pytest.MonkeyPatch, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        # Each read of the graph stamps taken_at; the API reads it afresh for every build, and
        # the replay caught two identical builds hashing apart (DECISIONS 2026-11-08).
        _with_page(monkeypatch, "ready")
        later = snapshot.model_copy(update={"taken_at": snapshot.taken_at + dt.timedelta(minutes=5)})
        first = _build(snapshot, taxonomy)
        again = build_resume(
            _jd(),
            _named(later),
            ResumeOptions(today=TODAY),
            None,
            taxonomy=taxonomy,
            embedder=WordHashEmbedder(),
            printer=ChromiumPrinter(),
        )
        assert first.input_hash == again.input_hash

    def test_no_model_says_so_in_the_versions(
        self, monkeypatch: pytest.MonkeyPatch, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        _with_page(monkeypatch, "ready")
        versions = _build(snapshot, taxonomy).versions
        assert (versions["stage1"], versions["six_second"]) == ("rules", "not run")
        assert {"taxonomy", "style", "template", "render_template", "embedder"} <= set(versions)


@pytest.mark.chromium
class TestARealPage:
    def test_the_same_inputs_give_the_same_page_and_artifacts(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        first, again = _build(snapshot, taxonomy), _build(snapshot, taxonomy)
        assert first.status == "ok", first.page.findings
        assert first.input_hash == again.input_hash
        artifacts = first.artifacts()
        assert set(artifacts) == {
            "plan",
            "requirements",
            "bullets",
            "evidence_map",
            "validation_report",
            "versions",
            "input_hash",
        }
        assert set(first.evidence.per_requirement) <= {r["id"] for r in artifacts["requirements"]}
        assert artifacts["bullets"] == again.artifacts()["bullets"]
        assert all(bullet["fact_ids"] for bullet in artifacts["bullets"])
        assert first.pdf.startswith(b"%PDF")
