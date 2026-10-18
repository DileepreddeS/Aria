"""models.lock, the files it pins, and the one tool allowed to download them."""

from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from aria_resume.models import LOCK_PATH, ModelError, ModelFile, ModelSpec, load_lock, model_directory, verify

FETCH = Path(__file__).resolve().parents[3] / "infra" / "tools" / "fetch_models.py"


def _fetch_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_models", FETCH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheLock:
    def test_the_shipped_lock_pins_a_commit_and_hashes(self) -> None:
        embedding = load_lock()["embedding"]
        assert embedding.repo == "Snowflake/snowflake-arctic-embed-s"
        assert len(embedding.commit) == 40
        assert {file.path for file in embedding.files} == {"onnx/model.onnx", "tokenizer.json"}
        assert all(len(file.sha256) == 64 for file in embedding.files)

    def test_the_url_names_the_commit_never_a_branch(self) -> None:
        embedding = load_lock()["embedding"]
        url = embedding.url(embedding.file("onnx/model.onnx"))
        assert url == f"https://huggingface.co/{embedding.repo}/resolve/{embedding.commit}/onnx/model.onnx"
        assert "/main/" not in url

    @pytest.mark.parametrize(
        ("change", "says"),
        [
            ({"commit": "main"}, "40-character commit"),
            ({"files": {"onnx/model.onnx": {"sha256": "abc", "size": 1}}}, "SHA-256"),
            ({"files": {"../../evil": {"sha256": "a" * 64, "size": 1}}}, "inside the model"),
            ({"repo": "not a repo"}, "repo"),
            ({"pooling": "max"}, "pooling"),
        ],
    )
    def test_a_lock_that_loosens_a_pin_is_refused(
        self, tmp_path: Path, change: dict[str, Any], says: str
    ) -> None:
        data = yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))
        data["models"]["embedding"].update(change)
        path = tmp_path / "models.lock"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(ModelError, match=says):
            load_lock(path)


def _spec(body: bytes) -> ModelSpec:
    return ModelSpec(
        name="tiny",
        repo="example/tiny",
        commit="0" * 40,
        licence="test",
        files=(ModelFile("weights.bin", hashlib.sha256(body).hexdigest(), len(body)),),
    )


class TestVerify:
    def test_a_missing_file_says_what_to_run(self, tmp_path: Path) -> None:
        with pytest.raises(ModelError, match=r"tasks\.ps1 models"):
            verify(_spec(b"weights"), tmp_path)

    def test_an_altered_file_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "weights.bin").write_bytes(b"weightz")
        with pytest.raises(ModelError, match="does not match"):
            verify(_spec(b"weights"), tmp_path)

    def test_the_pinned_file_passes(self, tmp_path: Path) -> None:
        (tmp_path / "weights.bin").write_bytes(b"weights")
        verify(_spec(b"weights"), tmp_path)


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class TestTheFetcher:
    def _serve(
        self, monkeypatch: pytest.MonkeyPatch, module: ModuleType, body: bytes, seen: list[str]
    ) -> None:
        def urlopen(request: Any, timeout: float) -> _Response:
            seen.append(request.full_url)
            return _Response(body)

        monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)

    def test_a_matching_download_is_put_in_place_from_the_commit_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, seen = _fetch_module(), list[str]()
        self._serve(monkeypatch, module, b"weights", seen)
        spec = _spec(b"weights")
        module.fetch(spec, tmp_path)
        assert (model_directory(spec, tmp_path) / "weights.bin").read_bytes() == b"weights"
        assert seen == [f"https://huggingface.co/example/tiny/resolve/{'0' * 40}/weights.bin"]

    def test_a_download_that_does_not_match_is_deleted_and_nothing_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, seen = _fetch_module(), list[str]()
        self._serve(monkeypatch, module, b"tampered", seen)
        spec = _spec(b"weights")
        with pytest.raises(ModelError, match="did not match"):
            module.fetch(spec, tmp_path)
        directory = model_directory(spec, tmp_path)
        assert not any(path.is_file() for path in directory.rglob("*"))

    def test_a_verified_model_is_not_fetched_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, seen = _fetch_module(), list[str]()
        self._serve(monkeypatch, module, b"weights", seen)
        spec = _spec(b"weights")
        module.fetch(spec, tmp_path)
        module.fetch(spec, tmp_path)
        assert len(seen) == 1
