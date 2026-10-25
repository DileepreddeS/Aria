"""The eval's own rules: what a label mismatch says, and when the baseline may change."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from aria_resume.jd.reader import read_jd
from aria_resume.match.embeddings import WordHashEmbedder
from aria_resume.match.matcher import match
from aria_resume.normalize.requirements import normalize
from evals import run
from evals.resume import suite
from evals.resume.suite import PostingResult, Report, label_mismatches

from aria_core.taxonomy.loader import load_default_taxonomy

SYNTHETIC = suite.SUITES["resume-synthetic"]


def _labels(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((SYNTHETIC / f"{name}.labels.yaml").read_text(encoding="utf-8"))
    return loaded


def _mismatches(name: str, labels: dict[str, Any]) -> list[str]:
    taxonomy = load_default_taxonomy()
    parsed = read_jd((SYNTHETIC / f"{name}.txt").read_text(encoding="utf-8"), taxonomy)
    normalized = normalize(parsed, taxonomy)
    evidence = match(normalized, suite.load_candidate(), taxonomy, WordHashEmbedder())
    return label_mismatches(parsed, normalized, evidence, labels)


class TestLabels:
    @pytest.mark.parametrize("name", ["corvane_ai", "larkspur_backend", "tidewell_frontend"])
    def test_each_synthetic_posting_reads_as_labelled(self, name: str) -> None:
        assert _mismatches(name, _labels(name)) == []

    def test_a_must_have_grouped_differently_is_named(self) -> None:
        labels = _labels("corvane_ai")
        labels["must_have"] = [item for item in labels["must_have"] if item != "pytorch|huggingface"]
        labels["must_have"] += ["pytorch", "huggingface"]
        (found,) = _mismatches("corvane_ai", labels)
        assert found == (
            "must_have: read but not labelled ['huggingface|pytorch']; "
            "labelled but not read ['huggingface', 'pytorch']"
        )

    def test_a_skill_read_but_not_labelled_is_named(self) -> None:
        labels = _labels("larkspur_backend")
        labels["skills"] = [skill for skill in labels["skills"] if skill != "kafka"]
        assert _mismatches("larkspur_backend", labels) == [
            "skills: read but not labelled ['kafka']; labelled but not read []"
        ]

    def test_a_scalar_is_named_with_both_readings(self) -> None:
        labels = {**_labels("tidewell_frontend"), "min_years": 3}
        assert _mismatches("tidewell_frontend", labels) == ["min_years: read 5, labelled 3"]


def test_taxonomy_gaps_join_stage_2_s_unplaced_lines_and_the_model_s_drops() -> None:
    from aria_resume.schemas import JDSection, NormalizedJD, ParsedJD, RawRequirement

    parsed = ParsedJD(
        reading_notes=("model: instructions x", "taxonomy_gap: Experience with Zendesk is a plus.")
    )
    normalized = NormalizedJD(
        requirements=(),
        taxonomy_version="test",
        unplaced=(RawRequirement(text="A portfolio of shipped work", section=JDSection.REQUIRED),),
    )
    assert suite.taxonomy_gaps(parsed, normalized) == [
        "A portfolio of shipped work",
        "Experience with Zendesk is a plus.",
    ]


def _report(*, failures: list[str], coverage: float = 0.5) -> Report:
    gates: dict[str, Any] = {
        "truth_violations": [],
        "banned_words": [],
        "recruiter_findings": [],
        "one_page": True,
        "layout_findings": [],
        "ats_findings": [],
        "label_mismatches": [],
    }
    reported = {
        "role_type": "backend",
        "fit": "ok",
        "coverage": coverage,
        "status": "ready",
        "fit_rounds": 1,
        "dropped_facts": 0,
        "bullets": 6,
    }
    return Report("resume-synthetic", {"embedder": "fake"}, [PostingResult("p", gates, reported, failures)])


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    reports, baseline = tmp_path / "reports", tmp_path / "baseline.json"
    monkeypatch.setattr(run, "REPORTS", reports)
    monkeypatch.setattr(run, "BASELINES", {"resume-synthetic": baseline})
    return reports, baseline


def _serve(monkeypatch: pytest.MonkeyPatch, report: Report) -> list[Any]:
    calls: list[Any] = []

    def fake(name: str, *, embedder: Any = None, llm: Any = None) -> Report:
        calls.append(embedder)
        return report

    monkeypatch.setattr(run, "run_suite", fake)
    return calls


class TestBaseline:
    def test_a_run_writes_its_report_aside_and_leaves_the_baseline_alone(
        self, paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reports, baseline = paths
        baseline.write_text("{}\n", encoding="utf-8")
        _serve(monkeypatch, _report(failures=[]))
        assert run.main(["resume-synthetic"]) == 1  # differs from the baseline
        assert baseline.read_text(encoding="utf-8") == "{}\n"
        (written,) = reports.glob("resume-synthetic-*.json")
        assert json.loads(written.read_text(encoding="utf-8"))["passed"] is True

    def test_update_writes_the_baseline_and_the_next_run_matches_it(
        self, paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, baseline = paths
        _serve(monkeypatch, _report(failures=[]))
        assert run.main(["resume-synthetic", "--update-baseline"]) == 0
        assert "written_at" not in json.loads(baseline.read_text(encoding="utf-8"))
        assert run.main(["resume-synthetic"]) == 0

    def test_the_baseline_holds_nothing_the_print_decides(
        self, paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Linux and Windows lay a page out differently; the baseline must not notice.
        report = _report(failures=[])
        report.postings[0].gates["layout_findings"] = ["a line runs 1.7pt past the margin"]
        _serve(monkeypatch, report)
        run.main(["resume-synthetic", "--update-baseline"])
        _, baseline = paths
        (posting,) = json.loads(baseline.read_text(encoding="utf-8"))["postings"]
        assert posting["reported"] == {"role_type": "backend", "fit": "ok", "coverage": 0.5}
        assert posting["gates"]["layout_findings"] == 1 and posting["gates"]["one_page"] is True

    def test_coverage_is_compared_to_three_places(
        self, paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve(monkeypatch, _report(failures=[], coverage=1 / 3))
        run.main(["resume-synthetic", "--update-baseline"])
        _serve(monkeypatch, _report(failures=[], coverage=1 / 3 + 1e-12))
        assert run.main(["resume-synthetic"]) == 0

    def test_a_moved_number_fails_until_the_baseline_is_updated(
        self, paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve(monkeypatch, _report(failures=[]))
        run.main(["resume-synthetic", "--update-baseline"])
        _serve(monkeypatch, _report(failures=[], coverage=0.25))
        assert run.main(["resume-synthetic"]) == 1

    def test_update_is_refused_while_a_gate_fails(
        self, paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, baseline = paths
        _serve(monkeypatch, _report(failures=["truth_violations: [...]"]))
        assert run.main(["resume-synthetic", "--update-baseline"]) == 1
        assert not baseline.exists()

    def test_update_is_refused_with_the_models(
        self, paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, baseline = paths
        calls = _serve(monkeypatch, _report(failures=[]))
        assert run.main(["resume-synthetic", "--models", "--update-baseline"]) == 2
        assert calls == [] and not baseline.exists()

    def test_update_is_refused_for_a_suite_without_a_baseline(
        self, paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _serve(monkeypatch, _report(failures=[]))
        assert run.main(["resume", "--update-baseline"]) == 2
        assert calls == []


def test_the_reports_directory_is_ignored_by_git() -> None:
    ignored = (Path(__file__).parents[2] / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "evals/resume/reports/" in ignored
