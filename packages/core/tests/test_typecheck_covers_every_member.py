"""Every workspace member is typechecked, in CI and locally.

services/document_parser, the component that reads hostile uploads, was a workspace
member from Stage 0 and absent from the Makefile's typecheck target: CI never ran mypy
over it. Nothing noticed, because a missing line fails nothing. This test reads the
member list and both typecheck lists (the Makefile's, which CI runs, and tasks.ps1's,
which a developer runs) and fails on any member either one leaves out.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _members() -> set[str]:
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(root["tool"]["uv"]["workspace"]["members"])


def _makefile_targets() -> set[str]:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    block = makefile.split("\ntypecheck:", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"uv run mypy (\S+)", block))


def _tasks_targets() -> set[str]:
    return set(re.findall(r"uv run mypy (\S+?) ?\}", (REPO_ROOT / "tasks.ps1").read_text(encoding="utf-8")))


@pytest.mark.parametrize("where", ["Makefile", "tasks.ps1"])
def test_every_member_is_typechecked(where: str) -> None:
    targets = _makefile_targets() if where == "Makefile" else _tasks_targets()
    missing = sorted(_members() - targets)
    assert not missing, f"{where} does not typecheck {missing}"


def test_the_lists_are_read_at_all() -> None:
    # Guards against the parsing above finding nothing and the subtraction passing vacuously.
    assert len(_makefile_targets()) >= 5 and len(_tasks_targets()) >= 5
