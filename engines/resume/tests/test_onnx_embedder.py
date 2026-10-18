"""The ONNX embedding adapter: its rules on a stand-in backend (CI), the real model where present.

CI never installs the ml group, so everything the adapter decides (telemetry off before any
session, the query prefix on queries only, CLS or mean pooling, normalisation, offline) is
tested on a stand-in. The real model is then held to its publisher's own reference scores,
printed on its model card by a different implementation (sentence-transformers on PyTorch):
a pooling, prefix or normalisation error moves them, loudly.
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aria_resume.match.embeddings import cosine
from aria_resume.match.onnx_embedder import OnnxBackend, OnnxEmbedder, pool
from aria_resume.models import ModelError, ModelFile, ModelSpec, load_lock, model_directory


class _Array:
    def __init__(self, rows: Any) -> None:
        self.rows = rows

    def tolist(self) -> Any:
        return self.rows


class _Session:
    """Three-dimensional hidden states: token i of text t is [t + 1, i + 1]."""

    def __init__(self, calls: list[str]) -> None:
        calls.append("session")
        self.fed: list[dict[str, Any]] = []

    def get_inputs(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

    def run(self, _outputs: Any, feeds: dict[str, Any]) -> list[_Array]:
        self.fed.append(feeds)
        ids = feeds["input_ids"]
        return [_Array([[[t + 1.0, i + 1.0] for i, _ in enumerate(row)] for t, row in enumerate(ids)])]


class _Tokenizer:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def encode_batch(self, texts: list[str]) -> list[SimpleNamespace]:
        self.seen.extend(texts)
        width = max(len(text.split()) for text in texts)
        encodings = []
        for text in texts:
            words = len(text.split())
            encodings.append(
                SimpleNamespace(
                    ids=list(range(words)) + [0] * (width - words),
                    attention_mask=[1] * words + [0] * (width - words),
                    type_ids=[0] * width,
                )
            )
        return encodings


class _Backend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.tokenizer_ = _Tokenizer()

    def disable_telemetry(self) -> None:
        self.calls.append("telemetry off")

    def session(self, model: Path) -> _Session:
        self.session_ = _Session(self.calls)
        return self.session_

    def tokenizer(self, path: Path, max_tokens: int) -> _Tokenizer:
        return self.tokenizer_

    def int64(self, rows: list[list[int]]) -> list[list[int]]:
        return rows


def _installed(tmp_path: Path, pooling: str = "cls") -> tuple[ModelSpec, Path]:
    files = {"onnx/model.onnx": b"graph", "tokenizer.json": b"{}"}
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(body)
    spec = ModelSpec(
        name="embedding",
        repo="example/tiny",
        commit="0" * 40,
        licence="test",
        files=tuple(
            ModelFile(name, hashlib.sha256(body).hexdigest(), len(body)) for name, body in files.items()
        ),
        pooling=pooling,
        query_prefix="query: ",
    )
    return spec, tmp_path


class TestTheAdapter:
    def test_telemetry_is_off_before_any_session_exists(self, tmp_path: Path) -> None:
        backend = _Backend()
        OnnxEmbedder(*_installed(tmp_path), backend=backend)
        assert backend.calls == ["telemetry off", "session"]

    def test_it_runs_offline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        OnnxEmbedder(*_installed(tmp_path), backend=_Backend())
        assert os.environ["HF_HUB_OFFLINE"] == "1"

    def test_a_missing_or_altered_file_is_an_error_not_a_download(self, tmp_path: Path) -> None:
        spec, directory = _installed(tmp_path)
        (directory / "tokenizer.json").write_bytes(b"{ }")
        with pytest.raises(ModelError, match="does not match"):
            OnnxEmbedder(spec, directory, backend=_Backend())

    def test_the_prefix_goes_on_queries_only(self, tmp_path: Path) -> None:
        backend = _Backend()
        embedder = OnnxEmbedder(*_installed(tmp_path), backend=backend)
        embedder.embed_queries(["python services"])
        embedder.embed_documents(["Built python services"])
        assert backend.tokenizer_.seen == ["query: python services", "Built python services"]

    def test_only_the_inputs_the_graph_declares_are_fed(self, tmp_path: Path) -> None:
        backend = _Backend()
        OnnxEmbedder(*_installed(tmp_path), backend=backend).embed_documents(["a b"])
        assert set(backend.session_.fed[0]) == {"input_ids", "attention_mask"}

    def test_vectors_are_unit_length_and_cls_takes_the_first_token(self, tmp_path: Path) -> None:
        backend = _Backend()
        (vector,) = OnnxEmbedder(*_installed(tmp_path), backend=backend).embed_documents(["one two three"])
        # CLS: token 0 of text 0 is [1, 1].
        assert vector == pytest.approx((1 / math.sqrt(2), 1 / math.sqrt(2)))


class TestPooling:
    def test_cls_ignores_the_other_tokens(self) -> None:
        (vector,) = pool([[[3.0, 4.0], [100.0, 0.0]]], [[1, 1]], "cls")
        assert vector == pytest.approx((0.6, 0.8))

    def test_mean_counts_real_tokens_only(self) -> None:
        # The padded third token must not pull the mean towards it.
        # The padding points elsewhere ([50, 0]), so counting it would turn the vector, not just stretch it.
        (vector,) = pool([[[1.0, 0.0], [0.0, 1.0], [50.0, 0.0]]], [[1, 1, 0]], "mean")
        assert vector == pytest.approx((1 / math.sqrt(2), 1 / math.sqrt(2)))

    def test_every_vector_is_normalised(self) -> None:
        vectors = pool([[[3.0, 4.0]], [[0.0, 7.0]]], [[1], [1]], "cls")
        assert [math.hypot(*vector) for vector in vectors] == pytest.approx([1.0, 1.0])


def test_the_real_backend_switches_onnx_runtime_telemetry_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    runtime = SimpleNamespace(
        disable_telemetry_events=lambda: calls.append("disable_telemetry_events"),
        InferenceSession=lambda path, providers: calls.append(f"session {providers}"),
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    backend = OnnxBackend()
    backend.disable_telemetry()
    backend.session(Path("model.onnx"))
    assert calls == ["disable_telemetry_events", "session ['CPUExecutionProvider']"]


#: Printed on the model card at the pinned commit (e596f507), from sentence-transformers on PyTorch.
PUBLISHED = {
    ("what is snowflake?", "The Data Cloud!"): 0.533809,
    ("what is snowflake?", "Mexico City of Course!"): 0.49207097,
    ("Where can I get the best tacos?", "Mexico City of Course!"): 0.56592476,
    ("Where can I get the best tacos?", "The Data Cloud!"): 0.48255116,
}


def _real_model_or_skip() -> tuple[ModelSpec, Path]:
    spec = load_lock()["embedding"]
    directory = model_directory(spec)
    if not (directory / "onnx" / "model.onnx").is_file():
        pytest.skip(
            "the embedding weights are not installed here (./tasks.ps1 models); CI never downloads them"
        )
    pytest.importorskip("onnxruntime", reason="the ml group is not installed (uv sync --group ml)")
    return spec, directory


@pytest.mark.models
class TestTheRealModel:
    def _scores(self, embedder: OnnxEmbedder) -> dict[tuple[str, str], float]:
        queries = sorted({query for query, _ in PUBLISHED})
        documents = sorted({document for _, document in PUBLISHED})
        q = dict(zip(queries, embedder.embed_queries(queries), strict=True))
        d = dict(zip(documents, embedder.embed_documents(documents), strict=True))
        return {(query, document): cosine(q[query], d[document]) for query, document in PUBLISHED}

    def test_it_reproduces_the_publisher_s_reference_scores(self) -> None:
        embedder = OnnxEmbedder(*_real_model_or_skip())
        for pair, score in self._scores(embedder).items():
            assert score == pytest.approx(PUBLISHED[pair], abs=1e-3), pair
        (vector,) = embedder.embed_documents(["The Data Cloud!"])
        assert len(vector) == embedder.spec.dimensions
        assert math.fsum(value * value for value in vector) == pytest.approx(1.0)

    def test_loading_and_embedding_open_no_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No runtime download path: with every socket refused, the model still loads and runs.
        import socket

        def refused(*_: object, **__: object) -> None:
            raise AssertionError("the embedder tried to open a network connection")

        spec, directory = _real_model_or_skip()
        monkeypatch.setattr(socket.socket, "connect", refused)
        monkeypatch.setattr(socket, "create_connection", refused)
        (vector,) = OnnxEmbedder(spec, directory).embed_queries(["what is snowflake?"])
        assert len(vector) == spec.dimensions

    def test_the_reference_would_catch_the_wrong_pooling(self) -> None:
        spec, directory = _real_model_or_skip()
        mean = ModelSpec(
            **{**{field: getattr(spec, field) for field in spec.__dataclass_fields__}, "pooling": "mean"}
        )
        scores = self._scores(OnnxEmbedder(mean, directory))
        assert any(abs(scores[pair] - PUBLISHED[pair]) > 1e-3 for pair in PUBLISHED)
