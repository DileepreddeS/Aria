"""6b's entailment check: the cited facts must entail what a written bullet says.

The adapter's rules (labels read from the pinned config, telemetry off first, only the inputs
the graph takes, softmax) run on a stand-in backend, as CI runs them. The real model is held to
its publisher's own examples and to bullets set against the synthetic candidate's facts: the
ones a model wrote pass, the overclaims written to fail do not (DECISIONS 2026-11-09). As for
the embedder, it is skipped only where ARIA_MODELS=absent declares there are no models.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aria_resume.models import ModelError, ModelFile, ModelSpec, load_lock, model_directory
from aria_resume.validate.nli import (
    ENTAILMENT_MIN,
    Entailment,
    OnnxEntailer,
    check_entailed,
    in_taxonomy_spelling,
    premise,
    softmax,
)

from aria_core.taxonomy.model import SkillTaxonomy

CONFIG = {"id2label": {"0": "contradiction", "1": "entailment", "2": "neutral"}}


class _Array:
    def __init__(self, rows: Any) -> None:
        self.rows = rows

    def tolist(self) -> Any:
        return self.rows


class _Session:
    """Logits [0, 3, 1] for every pair: the second label is the likeliest."""

    def __init__(self, calls: list[str]) -> None:
        calls.append("session")
        self.fed: list[dict[str, Any]] = []

    def get_inputs(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

    def run(self, _outputs: Any, feeds: dict[str, Any]) -> list[_Array]:
        self.fed.append(feeds)
        return [_Array([[0.0, 3.0, 1.0] for _ in feeds["input_ids"]])]


class _Tokenizer:
    def __init__(self) -> None:
        self.seen: list[Any] = []

    def encode_batch(self, pairs: list[Any]) -> list[SimpleNamespace]:
        self.seen.extend(pairs)
        return [SimpleNamespace(ids=[1, 2], attention_mask=[1, 1], type_ids=[0, 1]) for _ in pairs]


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


def _installed(tmp_path: Path, config: dict[str, Any] = CONFIG) -> tuple[ModelSpec, Path]:
    files = {
        "onnx/model.onnx": b"graph",
        "tokenizer.json": b"{}",
        "config.json": json.dumps(config).encode(),
    }
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(body)
    spec = ModelSpec(
        name="nli",
        repo="example/tiny-nli",
        commit="0" * 40,
        licence="Apache-2.0",
        files=tuple(
            ModelFile(name, hashlib.sha256(body).hexdigest(), len(body)) for name, body in files.items()
        ),
    )
    return spec, tmp_path


class _Fixed:
    """An entailer that gives every pair the same chances, and records what it was asked."""

    model_id = "fixed"

    def __init__(self, entailment: float, neutral: float, contradiction: float) -> None:
        self.scores = Entailment(entailment, neutral, contradiction)
        self.asked: list[tuple[str, str]] = []

    def judge(self, pairs: Any) -> list[Entailment]:
        self.asked.extend(pairs)
        return [self.scores for _ in pairs]


class TestTheRule:
    def test_entailment_at_the_threshold_passes(self, taxonomy: SkillTaxonomy) -> None:
        entailer = _Fixed(ENTAILMENT_MIN, 1 - ENTAILMENT_MIN, 0.0)
        assert (
            check_entailed("Built 15+ React screens", ["Built 15+ React screens"], entailer, taxonomy) == []
        )

    def test_neutral_is_a_claim_beyond_the_facts(self, taxonomy: SkillTaxonomy) -> None:
        (finding,) = check_entailed("Led the team", ["Built a screen"], _Fixed(0.1, 0.8, 0.1), taxonomy)
        assert finding.check == "not_entailed" and "goes beyond" in finding.detail

    def test_contradiction_says_so(self, taxonomy: SkillTaxonomy) -> None:
        (finding,) = check_entailed("Worked alone", ["Led three"], _Fixed(0.1, 0.2, 0.7), taxonomy)
        assert "contradicts" in finding.detail

    def test_both_sides_are_put_in_the_taxonomy_s_spelling(self, taxonomy: SkillTaxonomy) -> None:
        # The model read "retrieval-augmented generation" against a fact's "RAG" as a new claim.
        entailer = _Fixed(0.9, 0.1, 0.0)
        check_entailed(
            "Built a RAG system on Postgres",
            ["Built a closed-loop RAG system", "Moved it to Postgres"],
            entailer,
            taxonomy,
        )
        ((facts, bullet),) = entailer.asked
        assert facts == ("Built a closed-loop retrieval-augmented generation system Moved it to PostgreSQL")
        assert bullet == "Built a retrieval-augmented generation system on PostgreSQL"

    def test_a_spelling_already_the_taxonomy_s_is_left_alone(self, taxonomy: SkillTaxonomy) -> None:
        assert in_taxonomy_spelling("Cut latency with Redis", taxonomy) == "Cut latency with Redis"
        assert premise(["  Built   React screens "], taxonomy) == "Built React screens"

    def test_softmax_is_a_distribution_even_for_large_logits(self) -> None:
        chances = softmax([1000.0, 1001.0, 999.0])
        assert math.fsum(chances) == pytest.approx(1.0)
        assert max(chances) == chances[1]


class TestTheAdapter:
    def test_labels_come_from_the_pinned_config(self, tmp_path: Path) -> None:
        backend = _Backend()
        (scores,) = OnnxEntailer(*_installed(tmp_path), backend=backend).judge([("a", "b")])
        assert scores.entailment > scores.neutral > scores.contradiction
        assert math.fsum((scores.entailment, scores.neutral, scores.contradiction)) == pytest.approx(1.0)

    def test_a_different_label_order_is_followed_not_assumed(self, tmp_path: Path) -> None:
        swapped = {"id2label": {"0": "contradiction", "1": "neutral", "2": "entailment"}}
        (scores,) = OnnxEntailer(*_installed(tmp_path, swapped), backend=_Backend()).judge([("a", "b")])
        assert scores.neutral > scores.entailment > scores.contradiction

    def test_a_config_without_the_three_labels_is_refused(self, tmp_path: Path) -> None:
        two = {"id2label": {"0": "entailment", "1": "not_entailment"}}
        with pytest.raises(ModelError, match="labels"):
            OnnxEntailer(*_installed(tmp_path, two), backend=_Backend())

    def test_telemetry_is_off_before_any_session(self, tmp_path: Path) -> None:
        backend = _Backend()
        OnnxEntailer(*_installed(tmp_path), backend=backend)
        assert backend.calls == ["telemetry off", "session"]

    def test_pairs_are_tokenized_as_pairs_in_batches_with_only_the_graph_s_inputs(
        self, tmp_path: Path
    ) -> None:
        backend = _Backend()
        entailer = OnnxEntailer(*_installed(tmp_path), backend=backend, batch_size=2)
        pairs = [("fact one", "bullet one"), ("fact two", "bullet two"), ("fact three", "bullet three")]
        assert len(entailer.judge(pairs)) == 3
        assert backend.tokenizer_.seen == pairs
        assert [len(feeds["input_ids"]) for feeds in backend.session_.fed] == [2, 1]
        assert all(set(feeds) == {"input_ids", "attention_mask"} for feeds in backend.session_.fed)

    def test_an_altered_file_is_refused_before_anything_loads(self, tmp_path: Path) -> None:
        spec, directory = _installed(tmp_path)
        (directory / "config.json").write_bytes(b'{"id2label": {}}')
        backend = _Backend()
        with pytest.raises(ModelError, match="does not match"):
            OnnxEntailer(spec, directory, backend=backend)
        assert backend.calls == []

    def test_the_lock_pins_the_approved_model(self) -> None:
        spec = load_lock()["nli"]
        assert (spec.repo, spec.commit) == (
            "cross-encoder/nli-deberta-v3-base",
            "6c749ce3425cd33b46d187e45b92bbf96ee12ec7",
        )
        assert {file.path for file in spec.files} == {"onnx/model.onnx", "tokenizer.json", "config.json"}


def _real_model_or_skip() -> tuple[ModelSpec, Path]:
    """As the embedder's tests: skipped only where ARIA_MODELS=absent says so, else it fails."""
    spec = load_lock()["nli"]
    directory = model_directory(spec)
    declared = os.environ.get("ARIA_MODELS")
    if declared == "absent":
        pytest.skip("ARIA_MODELS=absent: this run has no local models on purpose")
    if declared not in (None, ""):
        pytest.fail(f"ARIA_MODELS={declared!r} is not a value this suite knows; use absent or leave it unset")
    if importlib.util.find_spec("onnxruntime") is None:
        pytest.fail("the ml group is missing from this environment (uv sync --all-packages --group ml)")
    if not (directory / "onnx" / "model.onnx").is_file():
        pytest.fail("the ml group is installed but the NLI weights are not (./tasks.ps1 models)")
    return spec, directory


#: The synthetic candidate's facts, with bullets a model wrote from them (the live run of
#: 2026-11-09) and overclaims written to fail.
FACTS = {
    "redis": "Cut p95 API latency by 30% with Redis caching",
    "aws": "Designed cloud-native solutions for the checkout platform on AWS",
    "python": "Led three engineers moving the billing service to Python 3.12 in March 2023",
    "rag": "Built a closed-loop RAG system with hybrid retrieval",
    "node": "Shipped 8 full-stack applications with Node.js and PostgreSQL",
}
WRITTEN = [
    ("redis", "Cut p95 API latency by 30% by adding Redis caching"),
    ("aws", "Designed the cloud architecture for the checkout platform on AWS"),
    ("python", "Led three engineers in moving the billing service to Python 3.12 in March 2023"),
    ("rag", "Built a closed-loop retrieval-augmented generation system with hybrid retrieval."),
]
OVERCLAIMED = [
    ("redis", "Led the team that cut p95 API latency by 30% with Redis caching"),
    ("redis", "Cut p95 API latency by 30% by rewriting the database layer"),
    ("aws", "Designed and led the migration of the checkout platform to AWS"),
    ("python", "Single-handedly moved the billing service to Python 3.12 in March 2023"),
    ("node", "Shipped 8 full-stack applications used by millions of customers"),
    ("rag", "Built a production RAG system adopted across the company"),
]


@pytest.fixture(scope="module")
def entailer() -> OnnxEntailer:
    """Loaded once: the model is 739 MB."""
    return OnnxEntailer(*_real_model_or_skip())


@pytest.mark.models
class TestTheRealModel:
    def test_it_labels_the_publisher_s_own_examples(self, entailer: OnnxEntailer) -> None:
        eats, driving = entailer.judge(
            [
                ("A man is eating pizza", "A man eats something"),
                (
                    "A black race car starts up in front of a crowd of people.",
                    "A man is driving down a lonely road.",
                ),
            ]
        )
        assert eats.entailment > max(eats.neutral, eats.contradiction)
        assert driving.contradiction > max(driving.entailment, driving.neutral)

    @pytest.mark.parametrize(("fact", "bullet"), WRITTEN)
    def test_what_a_model_wrote_from_the_facts_passes(
        self, entailer: OnnxEntailer, taxonomy: SkillTaxonomy, fact: str, bullet: str
    ) -> None:
        assert check_entailed(bullet, [FACTS[fact]], entailer, taxonomy) == []

    @pytest.mark.parametrize(("fact", "bullet"), OVERCLAIMED)
    def test_a_claim_beyond_the_facts_fails(
        self, entailer: OnnxEntailer, taxonomy: SkillTaxonomy, fact: str, bullet: str
    ) -> None:
        assert [f.check for f in check_entailed(bullet, [FACTS[fact]], entailer, taxonomy)] == [
            "not_entailed"
        ]

    def test_the_facts_own_words_pass(self, entailer: OnnxEntailer, taxonomy: SkillTaxonomy) -> None:
        for fact in FACTS.values():
            assert check_entailed(fact, [fact], entailer, taxonomy) == []

    def test_judging_opens_no_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket

        def refused(*_: object, **__: object) -> None:
            raise AssertionError("the entailer tried to open a network connection")

        spec, directory = _real_model_or_skip()
        monkeypatch.setattr(socket.socket, "connect", refused)
        monkeypatch.setattr(socket, "create_connection", refused)
        (scores,) = OnnxEntailer(spec, directory).judge([(FACTS["redis"], FACTS["redis"])])
        assert scores.entailment >= ENTAILMENT_MIN
