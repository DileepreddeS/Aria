"""The local checks are CI's checks: same tools, same versions, same flags (DECISIONS 2026-10-29).

GitHub CI failed for 28 runs on findings no local run showed: the local secrets task called a
gitleaks binary no machine had, and `./tasks.ps1 check` had no sast or audit step at all. These
tests read the four files that define the checks and fail when one drifts from another.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
CI = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
TASKS = (REPO_ROOT / "tasks.ps1").read_text(encoding="utf-8")
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
PRE_COMMIT = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")


def _load_gitleaks() -> ModuleType:
    path = REPO_ROOT / "infra" / "checks" / "gitleaks.py"
    spec = importlib.util.spec_from_file_location("gitleaks_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gitleaks_check"] = module
    spec.loader.exec_module(module)
    return module


gitleaks = _load_gitleaks()


def _task(name: str) -> str:
    """The body of one task in tasks.ps1's switch, up to the next task."""
    match = re.search(rf'^    "{re.escape(name)}" \{{\n(.*?)^    \}}', TASKS, re.MULTILINE | re.DOTALL)
    assert match, f"tasks.ps1 has no {name!r} task"
    return match.group(1)


def _make_target(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\n((?:\t.*\n?)+)", MAKEFILE, re.MULTILINE)
    assert match, f"the Makefile has no {name!r} target"
    return match.group(1)


class TestGitleaks:
    def test_the_local_image_is_the_release_ci_installs(self) -> None:
        ci_version = re.search(r"GITLEAKS_VERSION: (\S+)", CI)
        assert ci_version and gitleaks.VERSION == ci_version.group(1)
        assert f":v{gitleaks.VERSION}@sha256:" in gitleaks.IMAGE

    def test_the_local_scans_use_ci_s_flags(self) -> None:
        assert "gitleaks dir . --redact --verbose --exit-code 1" in _make_target("secrets")
        assert "gitleaks git . --redact --verbose --exit-code 1" in CI
        assert gitleaks.FLAGS == ["--redact", "--verbose", "--exit-code", "1"]

    def test_the_local_task_scans_the_tree_and_the_history_as_ci_does(self) -> None:
        assert "infra/checks/gitleaks.py tree history" in _task("secrets")

    def test_the_hook_scans_staged_changes_with_the_same_image(self) -> None:
        assert "entry: uv run python infra/checks/gitleaks.py staged" in PRE_COMMIT
        assert "entry: gitleaks" not in PRE_COMMIT


class TestSemgrep:
    def test_the_local_version_is_the_one_ci_installs(self) -> None:
        ci_version = re.search(r"semgrep==(\S+)", CI)
        local_version = re.search(r'\$wanted = "([^"]+)"', _task("sast"))
        assert ci_version and local_version
        assert local_version.group(1) == ci_version.group(1)

    def test_the_local_rulesets_and_flags_are_ci_s(self) -> None:
        scan = _make_target("sast").splitlines()[0].strip()
        assert scan.startswith("semgrep --config p/python")
        assert scan in _task("sast")

    def test_both_test_each_rule_file_against_its_own_cases(self) -> None:
        assert "semgrep --test --config $$rules $$cases" in _make_target("sast")
        assert "semgrep --test --config $rules.FullName $cases" in _task("sast")

    def test_every_rule_file_has_cases(self) -> None:
        rule_files = sorted((REPO_ROOT / ".semgrep").glob("*.yml"))
        assert rule_files
        for rules in rule_files:
            assert (REPO_ROOT / ".semgrep" / "tests" / f"{rules.stem}.py").is_file(), rules.name


class TestCheckRunsEveryCiJob:
    def test_check_runs_a_local_counterpart_of_every_ci_job(self) -> None:
        check = _task("check")
        # lint, typecheck, test (with its eval step), secrets, sast, dependencies
        tasks = ("lint", "typecheck", "control-chars", "pii", "test-db", "eval", "secrets", "sast", "audit")
        for task in tasks:
            assert f"& $PSCommandPath {task}\n" in check, f"check does not run {task}"

    def test_every_ci_job_is_one_check_knows(self) -> None:
        jobs = set(re.findall(r"^  ([a-z]+):\n    runs-on:", CI, re.MULTILINE))
        assert jobs == {"lint", "typecheck", "test", "secrets", "sast", "dependencies"}, (
            "a CI job was added or renamed: give it a local counterpart in tasks.ps1 check, "
            "then update this list"
        )
