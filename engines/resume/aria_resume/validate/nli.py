"""6b's entailment check: the cited facts must entail what a written bullet says.

RESUME_ENGINE section 9 6b. The numbers and technologies a bullet names are checked by code
(:mod:`.truth`); what code cannot read is a claim in words: "led" for "helped", "the platform"
for "a screen of it", a scope or a team the facts never mention. A local NLI cross-encoder,
cross-encoder/nli-deberta-v3-base as models.lock pins it, reads each pair (the cited facts as
the premise, the bullet as the hypothesis) and gives the chance of each label.

Both sides are first put in the taxonomy's spelling: the writer is told to name a skill by
its display form, so a fact's "RAG" comes back as "retrieval-augmented generation", which the
model, knowing no acronyms, read as a new claim (DECISIONS 2026-11-09).

A bullet passes only when entailment is at least :data:`ENTAILMENT_MIN`: neutral and
contradiction are both a claim beyond the facts, so they fail together. The threshold was set
against the bullets a model wrote for the synthetic candidate and against overclaims written
to fail (DECISIONS 2026-11-09).

Everything the model needs is a file on disk, checked against its SHA-256 when it is loaded,
as for the embedder; the label order is read from its pinned config.json, never assumed. The
runtime is reached through the embedder's :class:`~aria_resume.match.onnx_embedder.Backend`,
so the rules here are tested in CI with a stand-in, and the real model wherever its weights
are present.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aria_core.importer.skills import tag_skills
from aria_core.taxonomy.model import SkillTaxonomy
from aria_resume.match.onnx_embedder import Backend, OnnxBackend
from aria_resume.models import ModelError, ModelSpec, load_lock, model_directory, verify
from aria_resume.validate import Finding

__all__ = [
    "ENTAILMENT_MIN",
    "Entailer",
    "Entailment",
    "OnnxEntailer",
    "check_entailed",
    "in_taxonomy_spelling",
    "premise",
    "softmax",
]

#: The least chance of entailment a written bullet may have (DECISIONS 2026-11-09).
ENTAILMENT_MIN = 0.5

_MODEL_FILE, _TOKENIZER_FILE, _CONFIG_FILE = "onnx/model.onnx", "tokenizer.json", "config.json"
_LABELS = frozenset({"contradiction", "entailment", "neutral"})


@dataclass(frozen=True, slots=True)
class Entailment:
    """The chance of each label for one (premise, hypothesis) pair; they sum to one."""

    entailment: float
    neutral: float
    contradiction: float


class Entailer(Protocol):
    model_id: str

    def judge(self, pairs: Sequence[tuple[str, str]]) -> list[Entailment]: ...


def softmax(logits: Sequence[float]) -> list[float]:
    top = max(logits)
    exps = [math.exp(value - top) for value in logits]
    total = math.fsum(exps)
    return [value / total for value in exps]


def _literally(value: str) -> Callable[[re.Match[str]], str]:
    return lambda _: value


def in_taxonomy_spelling(text: str, taxonomy: SkillTaxonomy) -> str:
    """Every skill the text names, spelt as the taxonomy displays it ("RAG" as its long form)."""
    text = " ".join(text.split())
    skills = sorted(tag_skills(text, taxonomy).skills, key=lambda skill: -len(skill.surface_form))
    for skill in skills:
        display = taxonomy.display_form(skill.canonical)
        if display != skill.surface_form:
            pattern = rf"(?<![\w-]){re.escape(skill.surface_form)}(?![\w-])"
            # A function, so a display form is never read as a replacement template.
            text = re.sub(pattern, _literally(display), text)
    return text


def premise(facts: Sequence[str], taxonomy: SkillTaxonomy) -> str:
    """The cited facts, as one premise, in the order they are cited."""
    return " ".join(in_taxonomy_spelling(fact, taxonomy) for fact in facts)


def check_entailed(
    text: str, facts: Sequence[str], entailer: Entailer, taxonomy: SkillTaxonomy
) -> list[Finding]:
    """A finding when the facts do not entail the bullet; said so the writer can fix it."""
    (scores,) = entailer.judge([(premise(facts, taxonomy), in_taxonomy_spelling(text, taxonomy))])
    if scores.entailment >= ENTAILMENT_MIN:
        return []
    reading = "contradicts" if scores.contradiction > scores.neutral else "goes beyond"
    return [
        Finding(
            "not_entailed",
            f"{text!r} {reading} its facts (entailment {scores.entailment:.2f}, below {ENTAILMENT_MIN}): "
            "say only what the facts say, in their scope",
        )
    ]


def _label_order(config: Path) -> tuple[str, ...]:
    raw = json.loads(config.read_text(encoding="utf-8"))
    id2label = raw.get("id2label") if isinstance(raw, dict) else None
    if not isinstance(id2label, dict):
        raise ModelError(f"nli: {config} has no id2label")
    order = tuple(str(id2label[key]).casefold() for key in sorted(id2label, key=int))
    if set(order) != _LABELS or len(order) != len(_LABELS):
        raise ModelError(f"nli: {config} labels {order}, not contradiction, entailment and neutral")
    return order


class OnnxEntailer:
    def __init__(
        self,
        spec: ModelSpec | None = None,
        directory: Path | None = None,
        *,
        backend: Backend | None = None,
        batch_size: int = 16,
    ) -> None:
        # A second lock, as for the embedder: nothing here may reach the Hub.
        os.environ["HF_HUB_OFFLINE"] = "1"
        self.spec = spec or load_lock()["nli"]
        directory = directory or model_directory(self.spec)
        verify(self.spec, directory)
        self._labels = _label_order(directory / _CONFIG_FILE)
        self._backend = backend or OnnxBackend()
        self._backend.disable_telemetry()
        self._session = self._backend.session(directory / _MODEL_FILE)
        self._tokenizer = self._backend.tokenizer(directory / _TOKENIZER_FILE, self.spec.max_tokens)
        self._inputs = {item.name for item in self._session.get_inputs()}
        self._batch_size = batch_size
        self.model_id = f"{self.spec.repo}@{self.spec.commit[:12]}"

    def judge(self, pairs: Sequence[tuple[str, str]]) -> list[Entailment]:
        judged: list[Entailment] = []
        for start in range(0, len(pairs), self._batch_size):
            encodings = self._tokenizer.encode_batch(list(pairs[start : start + self._batch_size]))
            feeds = {
                "input_ids": self._backend.int64([list(encoding.ids) for encoding in encodings]),
                "attention_mask": self._backend.int64(
                    [list(encoding.attention_mask) for encoding in encodings]
                ),
                "token_type_ids": self._backend.int64([list(encoding.type_ids) for encoding in encodings]),
            }
            (logits, *_) = self._session.run(
                None, {name: value for name, value in feeds.items() if name in self._inputs}
            )
            for row in logits.tolist():
                chances = dict(zip(self._labels, softmax(row), strict=True))
                judged.append(
                    Entailment(
                        entailment=chances["entailment"],
                        neutral=chances["neutral"],
                        contradiction=chances["contradiction"],
                    )
                )
        return judged
