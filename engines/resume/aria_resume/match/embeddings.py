"""Sentence embeddings for the matcher's ``semantic_sim``, behind an interface.

The real model runs locally (RESUME_ENGINE §7: candidate data is not sent to a third
party for matching) and is optional: its packages and weights are vetted in
docs/vetting/2026-10-18-ml-group.md and added only once approved. Until then, and in CI
always, :class:`WordHashEmbedder` stands in: deterministic, dependency-free, and good
enough to test that the matcher uses similarity, not to judge how well it does.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

__all__ = ["Embedder", "WordHashEmbedder", "cosine"]

Vector = tuple[float, ...]


class Embedder(Protocol):
    #: Recorded on every EvidenceMap, so a map can be explained with the model that made it.
    model_id: str

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        """One unit-length vector per text, in order."""
        ...


def cosine(left: Vector, right: Vector) -> float:
    """Similarity of two unit vectors, clamped to 0..1: opposite is as unrelated as orthogonal."""
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))


_STOP = frozenset(
    "a an and are as at be by for from in into is it of on or our the to with you your we will "
    "using used use".split()
)


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


class WordHashEmbedder:
    """Bag of stemmed words, hashed into a fixed number of buckets and normalised."""

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions
        self.model_id = f"fake:word-hash-{dimensions}"

    def _bucket(self, word: str) -> int:
        return int.from_bytes(hashlib.blake2b(word.encode(), digest_size=4).digest(), "big") % self.dimensions

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        vectors: list[Vector] = []
        for text in texts:
            counts = [0.0] * self.dimensions
            for word in re.findall(r"[a-z0-9][a-z0-9+#.]*", text.lower()):
                word = word.rstrip(".")
                if len(word) < 2 or word in _STOP:
                    continue
                counts[self._bucket(_stem(word))] += 1.0
            norm = math.sqrt(sum(value * value for value in counts))
            vectors.append(tuple(value / norm for value in counts) if norm else tuple(counts))
        return vectors
