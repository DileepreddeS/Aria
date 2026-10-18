"""The local embedding model behind the matcher: ONNX Runtime and a tokenizer file, no torch.

Everything the model needs is a file on disk, pinned in models.lock and checked against its
SHA-256 each time it is loaded. No code path here can download: the Hugging Face client is
never imported, ``HF_HUB_OFFLINE=1`` is set anyway, and a missing file is an error naming
``./tasks.ps1 models`` (docs/vetting/2026-10-18-ml-group.md).

The model-specific part is small and is ours to read: tokenize (with the model's query
prefix on queries only), run the graph, take the CLS token (or the masked mean, as the lock
says), L2-normalise. ONNX Runtime's telemetry is switched off before any session exists.

The runtime libraries are reached through :class:`Backend`, so the rules above are tested in
CI with a stand-in, where the ml group is never installed; the real model is tested against
its publisher's own reference scores wherever its weights are present.
"""

from __future__ import annotations

import importlib
import math
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from aria_resume.match.embeddings import Vector
from aria_resume.models import ModelSpec, load_lock, model_directory, verify

__all__ = ["Backend", "OnnxBackend", "OnnxEmbedder", "pool"]

_MODEL_FILE, _TOKENIZER_FILE = "onnx/model.onnx", "tokenizer.json"


class Backend(Protocol):
    def disable_telemetry(self) -> None: ...

    def session(self, model: Path) -> Any: ...

    def tokenizer(self, path: Path, max_tokens: int) -> Any: ...

    def int64(self, rows: list[list[int]]) -> Any: ...


class OnnxBackend:
    """onnxruntime, tokenizers and numpy, imported only when a real model is loaded."""

    def __init__(self) -> None:
        self._runtime: Any = importlib.import_module("onnxruntime")
        self._tokenizers: Any = importlib.import_module("tokenizers")
        self._numpy: Any = importlib.import_module("numpy")

    def disable_telemetry(self) -> None:
        self._runtime.disable_telemetry_events()

    def session(self, model: Path) -> Any:
        return self._runtime.InferenceSession(str(model), providers=["CPUExecutionProvider"])

    def tokenizer(self, path: Path, max_tokens: int) -> Any:
        tokenizer = self._tokenizers.Tokenizer.from_file(str(path))
        tokenizer.enable_truncation(max_length=max_tokens)
        tokenizer.enable_padding()
        return tokenizer

    def int64(self, rows: list[list[int]]) -> Any:
        return self._numpy.asarray(rows, dtype=self._numpy.int64)


def pool(
    hidden: Sequence[Sequence[Sequence[float]]], masks: Sequence[Sequence[int]], mode: str
) -> list[Vector]:
    """One L2-normalised vector per text: the CLS token's, or the mean over real tokens."""
    vectors: list[Vector] = []
    for states, mask in zip(hidden, masks, strict=True):
        if mode == "cls":
            raw = list(states[0])
        else:
            kept = [row for row, keep in zip(states, mask, strict=True) if keep]
            raw = (
                [sum(column) / len(kept) for column in zip(*kept, strict=True)]
                if kept
                else [0.0] * len(states[0])
            )
        norm = math.sqrt(sum(value * value for value in raw))
        vectors.append(tuple(value / norm for value in raw) if norm else tuple(raw))
    return vectors


class OnnxEmbedder:
    def __init__(
        self,
        spec: ModelSpec | None = None,
        directory: Path | None = None,
        *,
        backend: Backend | None = None,
        batch_size: int = 32,
    ) -> None:
        # A second lock: were anything to reach for the Hub client, it would find itself offline.
        os.environ["HF_HUB_OFFLINE"] = "1"
        self.spec = spec or load_lock()["embedding"]
        directory = directory or model_directory(self.spec)
        verify(self.spec, directory)
        self._backend = backend or OnnxBackend()
        self._backend.disable_telemetry()
        self._session = self._backend.session(directory / _MODEL_FILE)
        self._tokenizer = self._backend.tokenizer(directory / _TOKENIZER_FILE, self.spec.max_tokens)
        self._inputs = {item.name for item in self._session.get_inputs()}
        self._batch_size = batch_size
        self.model_id = f"{self.spec.repo}@{self.spec.commit[:12]}"

    def embed_queries(self, texts: Sequence[str]) -> list[Vector]:
        return self._embed([self.spec.query_prefix + text for text in texts])

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return self._embed(list(texts))

    def _embed(self, texts: list[str]) -> list[Vector]:
        vectors: list[Vector] = []
        for start in range(0, len(texts), self._batch_size):
            encodings = self._tokenizer.encode_batch(texts[start : start + self._batch_size])
            masks = [list(encoding.attention_mask) for encoding in encodings]
            feeds = {
                "input_ids": self._backend.int64([list(encoding.ids) for encoding in encodings]),
                "attention_mask": self._backend.int64(masks),
                "token_type_ids": self._backend.int64([list(encoding.type_ids) for encoding in encodings]),
            }
            (hidden, *_) = self._session.run(
                None, {name: value for name, value in feeds.items() if name in self._inputs}
            )
            vectors.extend(pool(hidden.tolist(), masks, self.spec.pooling))
        return vectors
