"""The local gitleaks wrapper (infra/checks/gitleaks.py) when Docker does not cooperate.

The pre-commit hook once waited forever on an engine that never answered (DECISIONS
2026-11-07). Docker is replaced by a recording fake, so these tests need no engine.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load() -> ModuleType:
    path = REPO_ROOT / "infra" / "checks" / "gitleaks.py"
    spec = importlib.util.spec_from_file_location("gitleaks_wrapper", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gitleaks_wrapper"] = module
    spec.loader.exec_module(module)
    return module


gitleaks = _load()


class FakeDocker:
    """Answers `docker version`, `docker run` and `docker rm` as each test arranges."""

    def __init__(self, *, engine: str = "answers", scan: str = "clean") -> None:
        self.engine = engine
        self.scan = scan
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[0] != "docker":
            raise AssertionError(f"unexpected command {command}")
        if command[1] == "version":
            if self.engine == "missing":
                raise FileNotFoundError("docker")
            if self.engine == "silent":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if self.engine == "stopped":
                return subprocess.CompletedProcess(command, 1, "", "error during connect: pipe not found")
            return subprocess.CompletedProcess(command, 0, "28.4.0\n", "")
        if command[1] == "run":
            if self.scan == "hangs":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if self.scan == "interrupted":
                raise KeyboardInterrupt
            return subprocess.CompletedProcess(command, 1 if self.scan == "leak" else 0)
        if command[1] == "rm":
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError(f"unexpected docker command {command}")

    def runs(self) -> list[list[str]]:
        return [call for call in self.calls if call[1] == "run"]

    def removed(self) -> list[str]:
        return [call[-1] for call in self.calls if call[1] == "rm"]


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    # The wrapper calls subprocess.run itself, where ruff's S603 can see each call.
    monkeypatch.setattr(gitleaks.subprocess, "run", fake)
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    return fake


class TestAnEngineThatDoesNotAnswer:
    def test_a_silent_engine_fails_in_seconds_and_says_what_to_do(
        self, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
    ) -> None:
        docker.engine = "silent"
        assert gitleaks.main(["staged"]) == 3
        error = capsys.readouterr().err
        assert f"did not answer `docker version` in {gitleaks.ENGINE_TIMEOUT}s" in error
        assert "restart Docker Desktop" in error
        assert docker.runs() == []  # never reaches the call that waits without end

    def test_a_stopped_engine_is_named_as_such(
        self, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
    ) -> None:
        docker.engine = "stopped"
        assert gitleaks.main(["staged"]) == 3
        assert "pipe not found" in capsys.readouterr().err

    def test_no_docker_at_all_is_named_as_such(
        self, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
    ) -> None:
        docker.engine = "missing"
        assert gitleaks.main(["staged"]) == 3
        assert "docker is not on PATH" in capsys.readouterr().err

    def test_the_engine_is_asked_once_for_every_mode(self, docker: FakeDocker) -> None:
        assert gitleaks.main(["staged", "history"]) == 0
        assert [call[1] for call in docker.calls] == ["version", "run", "run"]


class TestAScanThatDoesNotFinish:
    def test_a_scan_past_its_time_fails_and_its_container_is_removed(
        self, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
    ) -> None:
        docker.scan = "hangs"
        assert gitleaks.main(["staged"]) == 3
        assert f"did not finish in {gitleaks.SCAN_TIMEOUT}s" in capsys.readouterr().err
        # --rm removes a container only when it exits; killing the CLI leaves it running.
        assert docker.removed() == [_name(docker.runs()[0])]

    def test_ctrl_c_removes_the_container_too(self, docker: FakeDocker) -> None:
        docker.scan = "interrupted"
        with pytest.raises(KeyboardInterrupt):
            gitleaks.main(["staged"])
        assert docker.removed() == [_name(docker.runs()[0])]

    def test_a_finding_is_still_a_failure(self, docker: FakeDocker) -> None:
        docker.scan = "leak"
        assert gitleaks.main(["staged"]) == 1
        assert docker.removed() == []


def _name(run: list[str]) -> str:
    return run[run.index("--name") + 1]


def _env(run: list[str]) -> list[str]:
    return [run[i + 1] for i, part in enumerate(run) if part == "-e"]


class TestTheIndexBeingCommitted:
    """`git commit <paths>` builds a temporary index and names it in GIT_INDEX_FILE."""

    def test_the_hook_scans_the_index_git_names(
        self, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_INDEX_FILE", str(gitleaks.ROOT / ".git" / "next-index-4242.lock"))
        assert gitleaks.main(["staged"]) == 0
        assert _env(docker.runs()[0]) == ["GIT_INDEX_FILE=/scan/.git/next-index-4242.lock"]

    def test_a_relative_index_is_read_from_the_top_of_the_work_tree(
        self, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(gitleaks.ROOT)
        monkeypatch.setenv("GIT_INDEX_FILE", ".git/index.lock")
        assert gitleaks.main(["staged"]) == 0
        assert _env(docker.runs()[0]) == ["GIT_INDEX_FILE=/scan/.git/index.lock"]

    def test_an_index_outside_the_repository_is_refused_rather_than_ignored(
        self, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "index"))
        assert gitleaks.main(["staged"]) == 3
        assert docker.runs() == []

    def test_without_git_s_variable_the_repository_s_own_index_is_read(self, docker: FakeDocker) -> None:
        assert gitleaks.main(["staged"]) == 0
        assert _env(docker.runs()[0]) == []

    def test_only_the_staged_scan_reads_an_index(
        self, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_INDEX_FILE", str(gitleaks.ROOT / ".git" / "index.lock"))
        assert gitleaks.main(["history"]) == 0
        assert _env(docker.runs()[0]) == []
