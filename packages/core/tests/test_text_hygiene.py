"""Non-ASCII characters in Python string literals are reported (infra/checks/no_control_characters.py).

The samples are written to temporary files with the characters inserted by code, so this
file's own source stays ASCII — which the check also covers.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DASH = "\u2013"


def _load() -> ModuleType:
    path = REPO_ROOT / "infra" / "checks" / "no_control_characters.py"
    spec = importlib.util.spec_from_file_location("no_control_characters", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["no_control_characters"] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def _findings(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    findings: list[str] = check.literal_findings(path)
    return findings


@pytest.mark.parametrize(
    "source",
    [
        f'RANGE = "2023 {DASH} 2024"\n',
        f"PATTERN = r'[-{DASH}]'\n",
        f'MESSAGE = f"{{count}} roles {DASH} checked"\n',
        f'def f() -> str:\n    """Doc."""\n    return "a {DASH} b"\n',
    ],
)
def test_a_non_ascii_character_in_a_literal_is_reported_with_its_escape(tmp_path: Path, source: str) -> None:
    findings = _findings(tmp_path, source)
    assert len(findings) == 1
    assert "EN DASH" in findings[0]
    assert "\\u2013" in findings[0]


@pytest.mark.parametrize(
    "source",
    [
        f'"""A module docstring {DASH} kept as written."""\n',
        f'class C:\n    """A class docstring {DASH} too."""\n',
        f"X = 1  # a comment {DASH} is prose\n",
        'RANGE = "2023 \\u2013 2024"\n',
    ],
)
def test_docstrings_comments_and_escapes_are_left_alone(tmp_path: Path, source: str) -> None:
    assert _findings(tmp_path, source) == []


def test_a_file_that_does_not_parse_is_not_this_check_s_business(tmp_path: Path) -> None:
    assert _findings(tmp_path, f'X = "{DASH}\n') == []


def test_the_repository_passes() -> None:
    files = [path for path in check.tracked_text_files() if path.suffix == ".py"]
    assert [finding for path in files for finding in check.literal_findings(REPO_ROOT / path)] == []
