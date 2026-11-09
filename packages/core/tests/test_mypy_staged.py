"""The mypy hook checks the index, not the work tree (infra/checks/mypy_staged.py).

Three commits failed the hook because mypy, run on the work tree, read an untracked router
beside the staged files (DECISIONS 2026-11-09). Each test builds a small repository whose
index and work tree disagree, and asserts the export holds the index's side only.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load() -> ModuleType:
    path = REPO_ROOT / "infra" / "checks" / "mypy_staged.py"
    spec = importlib.util.spec_from_file_location("mypy_staged_hook", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["mypy_staged_hook"] = module
    spec.loader.exec_module(module)
    return module


mypy_staged = _load()


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / "pyproject.toml").write_bytes(b"[tool.mypy]\nstrict = true\n")
    (root / "pkg" / "py.typed").write_bytes(b"")
    (root / "pkg" / "staged.py").write_bytes(b"VALUE: int = 1\n")
    (root / "pkg" / "notes.txt").write_bytes(b"not read by mypy\n")
    _git(root, "add", "pyproject.toml", "pkg")
    return root


class TestTheExport:
    def test_untracked_files_are_left_out(self, repo: Path, tmp_path: Path) -> None:
        (repo / "pkg" / "untracked.py").write_bytes(b"from pkg.missing import nothing\n")
        tree = tmp_path / "tree"
        mypy_staged.export_index(repo, tree)
        assert (tree / "pkg" / "staged.py").is_file()
        assert not (tree / "pkg" / "untracked.py").exists()

    def test_the_staged_copy_wins_over_the_work_tree_s(self, repo: Path, tmp_path: Path) -> None:
        (repo / "pkg" / "staged.py").write_bytes(b"VALUE: int = 'unstaged and wrong'\n")
        tree = tmp_path / "tree"
        mypy_staged.export_index(repo, tree)
        assert (tree / "pkg" / "staged.py").read_bytes() == b"VALUE: int = 1\n"

    def test_only_what_mypy_reads_is_exported(self, repo: Path, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        names = mypy_staged.export_index(repo, tree)
        assert sorted(names) == ["pkg/py.typed", "pkg/staged.py", "pyproject.toml"]
        assert not (tree / "pkg" / "notes.txt").exists()

    def test_a_file_from_an_earlier_export_does_not_survive(self, repo: Path, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        (tree / "pkg").mkdir(parents=True)
        (tree / "pkg" / "deleted_since.py").write_bytes(b"x: int = 'stale'\n")
        mypy_staged.export_index(repo, tree)
        assert not (tree / "pkg" / "deleted_since.py").exists()

    def test_the_index_git_names_for_a_path_limited_commit_is_the_one_exported(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `git commit <paths>` commits a temporary index and names it in GIT_INDEX_FILE.
        temporary = repo / ".git" / "next-index"
        (repo / "pkg" / "staged.py").write_bytes(b"VALUE: int = 2\n")
        env = {**os.environ, "GIT_INDEX_FILE": str(temporary)}
        _git(repo, "read-tree", "--empty", env=env)
        _git(repo, "add", "pyproject.toml", "pkg/staged.py", env=env)
        (repo / "pkg" / "staged.py").write_bytes(b"VALUE: int = 3\n")
        monkeypatch.setenv("GIT_INDEX_FILE", str(temporary))
        tree = tmp_path / "tree"
        names = mypy_staged.export_index(repo, tree)
        assert sorted(names) == ["pkg/staged.py", "pyproject.toml"]
        assert (tree / "pkg" / "staged.py").read_bytes() == b"VALUE: int = 2\n"
