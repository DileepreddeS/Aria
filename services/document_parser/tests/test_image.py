"""The parser image: a stale one is recognised, and the build context holds nothing private.

No Docker needed: the docker command line is replaced by a stand-in that records what
it was asked and answers as Docker would.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from aria_document_parser import image


def _docker_answers(stdout: str = "", returncode: int = 0) -> Any:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

    return run


class TestStaleImagesAreRecognised:
    def test_an_image_built_from_these_sources_is_current(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", _docker_answers(image.fingerprint() + "\n"))
        assert image.image_status("aria-parser:test").current

    def test_an_image_built_from_other_sources_is_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", _docker_answers("0" * 64))
        status = image.image_status("aria-parser:test")
        assert not status.current
        assert "different sources" in status.reason
        assert "parser-image" in status.reason

    def test_an_image_without_the_label_is_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", _docker_answers("<no value>"))
        assert not image.image_status("aria-parser:test").current

    def test_a_missing_image_says_what_to_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", _docker_answers(returncode=1))
        status = image.image_status("aria-parser:test")
        assert not status.current
        assert "not present" in status.reason
        assert "parser-image" in status.reason

    def test_no_docker_is_reported_rather_than_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(name: str) -> str:
            raise FileNotFoundError(f"{name} is not installed or not on PATH")

        monkeypatch.setattr(image, "_executable", missing)
        assert not image.image_status("aria-parser:test").current


class TestTheFingerprint:
    def test_it_covers_the_worker_the_sandbox_and_the_core_it_imports(self) -> None:
        names = {name for name, _ in image._sources()}
        assert "site/aria_document_parser/worker.py" in names
        assert "site/aria_document_parser/sandbox.py" in names
        assert "site/aria_core/documents.py" in names

    def test_it_changes_when_any_source_changes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        before = image.fingerprint()
        original = Path.read_bytes
        worker = next(path for name, path in image._sources() if name.endswith("/worker.py"))

        def edited(self: Path) -> bytes:
            content = original(self)
            return content + b"\n# edited\n" if self == worker else content

        monkeypatch.setattr(Path, "read_bytes", edited)
        assert image.fingerprint() != before

    def test_it_is_stable(self) -> None:
        assert image.fingerprint() == image.fingerprint()


class TestTheBuildContext:
    def test_it_holds_the_dockerfile_requirements_and_sources_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, list[str]] = {}

        def export(destination: Path) -> None:
            destination.write_text("pypdf==6.19.0 --hash=sha256:00\n", encoding="utf-8")

        def docker(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            context = Path(command[-1])
            seen["files"] = sorted(
                path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()
            )
            seen["command"] = command
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(image, "_export_requirements", export)
        monkeypatch.setattr(subprocess, "run", docker)
        stamp = image.build("aria-parser:test")

        files = seen["files"]
        assert "Dockerfile" in files
        assert "requirements.txt" in files
        assert all(name in ("Dockerfile", "requirements.txt") or name.startswith("site/") for name in files)
        assert not [name for name in files if Path(name).name.startswith(".env")]
        assert f"{image.FINGERPRINT_LABEL}={stamp}" in seen["command"]
        assert Path(seen["command"][-1]) != image._workspace_root()
