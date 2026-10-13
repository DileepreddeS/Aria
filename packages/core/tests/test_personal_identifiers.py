"""The guard that keeps a real person's identifiers out of the repository.

Uses a made-up person, Quentin Testperson, as the denylisted identity, so nothing real
is needed to test the guard and nothing real is written here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

DENYLIST = """
# a made-up person
name: Quentin Testperson
name: Testperson
email: q.testperson@example.org
phone: +1 (555) 010-9999
handle: qtest-1234
"""


def _load() -> ModuleType:
    path = REPO_ROOT / "infra" / "checks" / "no_personal_identifiers.py"
    spec = importlib.util.spec_from_file_location("no_personal_identifiers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["no_personal_identifiers"] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def _findings(tmp_path: Path, text: str, name: str = "sample.py") -> list[str]:
    (tmp_path / name).write_text(text, encoding="utf-8")
    findings: list[str] = guard.scan([name], guard.parse_denylist(DENYLIST), root=tmp_path)
    return findings


class TestWhatIsCaught:
    def test_a_surname_on_its_own_when_listed_on_its_own(self, tmp_path: Path) -> None:
        assert _findings(tmp_path, "assert 'Testperson' not in rendered") == [
            "sample.py:1: a denylisted name"
        ]

    @pytest.mark.parametrize(
        "text",
        [
            'RESUME = "Quentin Testperson\\n"',
            "QUENTIN TESTPERSON",
            "quentin_testperson_resume.docx",
            "Quentin-Testperson",
        ],
    )
    def test_the_name_in_any_case_and_with_any_separator(self, tmp_path: Path, text: str) -> None:
        # The full name alone: with the surname listed too, it would match whatever the
        # separator, and this test would not be about separators.
        (tmp_path / "sample.py").write_text(text, encoding="utf-8")
        full_name_only = guard.parse_denylist("name: Quentin Testperson")
        assert guard.scan(["sample.py"], full_name_only, root=tmp_path) == ["sample.py:1: a denylisted name"]

    @pytest.mark.parametrize("text", ["Q.Testperson@Example.org", "mailto:q.testperson@example.org"])
    def test_the_email_in_any_case(self, tmp_path: Path, text: str) -> None:
        # The address also holds the surname, so it is reported as both.
        assert "sample.py:1: a denylisted email" in _findings(tmp_path, text)

    @pytest.mark.parametrize("text", ["(555) 010-9999", "555.010.9999", "+15550109999", "555 010 9999"])
    def test_the_phone_with_any_separators(self, tmp_path: Path, text: str) -> None:
        assert _findings(tmp_path, text) == ["sample.py:1: a denylisted phone"]

    def test_the_handle_as_a_whole_token(self, tmp_path: Path) -> None:
        assert _findings(tmp_path, "linkedin.com/in/qtest-1234") == ["sample.py:1: a denylisted handle"]

    def test_a_name_in_a_file_s_path(self, tmp_path: Path) -> None:
        findings = _findings(tmp_path, "nothing here", name="Testperson_notes.txt")
        assert findings == ["Testperson_notes.txt: a denylisted name in the file's path"]


class TestWhatIsNot:
    @pytest.mark.parametrize(
        "text",
        [
            "Jordan A. Rivera",  # the synthetic identity
            "Testpersonality",  # a longer word is a different word
            "555 010 99990",  # a longer number is a different number
            "qtest-12345",  # a longer handle is a different handle
        ],
    )
    def test_near_misses_pass(self, tmp_path: Path, text: str) -> None:
        assert _findings(tmp_path, text) == []

    def test_binary_files_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "blob.bin").write_bytes(b"\0\0Quentin Testperson")
        assert guard.scan(["blob.bin"], guard.parse_denylist(DENYLIST), root=tmp_path) == []


class TestTheDenylistItself:
    def test_a_finding_never_includes_the_value(self, tmp_path: Path) -> None:
        # CI logs must not republish what the check exists to keep out.
        findings = _findings(tmp_path, "Quentin Testperson, q.testperson@example.org, (555) 010-9999")
        rendered = "\n".join(findings)
        assert "Testperson" not in rendered and "example.org" not in rendered and "9999" not in rendered

    def test_no_denylist_is_a_failure_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(guard.DENYLIST_ENV, raising=False)
        monkeypatch.setattr(guard, "DENYLIST_FILE", tmp_path / "absent.txt")
        assert guard.main([]) == 2

    def test_an_empty_denylist_is_a_failure_not_a_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(guard.DENYLIST_ENV, "# nothing but a comment\n")
        assert guard.main([]) == 2

    def test_the_environment_wins_over_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        listed = tmp_path / "listed.txt"
        listed.write_text("Quentin Testperson", encoding="utf-8")
        monkeypatch.setenv(guard.DENYLIST_ENV, DENYLIST)
        monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
        assert guard.main(["listed.txt"]) == 1

    @pytest.mark.parametrize("line", ["Quentin Testperson", "colour: blue", "phone: 123"])
    def test_a_malformed_line_is_refused(self, line: str) -> None:
        with pytest.raises(ValueError):
            guard.parse_denylist(line)
