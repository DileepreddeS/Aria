"""The red-team postings: injected job postings, each read by Stage 1 with a hostile model.

SECURITY.md §5.6. Each posting in ``postings/`` has a ``.yaml`` beside it saying who wrote
it (``author: defence`` for postings written by the author of the defences they test,
``published`` for one adapted from a published corpus, cited in the file), the lines that
must never become a requirement, the flags the rules alone must raise, and the reply a
model that obeyed the posting would give.

In CI the hostile reply is scripted (``evals/tests/test_security_postings.py``). With
``python -m evals.run security --llm`` the same postings go to the real model through the
gateway, and what it does is reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from aria_resume.jd.model_step import read_jd_with_model
from aria_resume.llm import LlmClient
from aria_resume.schemas import ParsedJD

from aria_core.taxonomy.model import SkillTaxonomy

__all__ = ["POSTINGS", "Examined", "Posting", "examine", "load_postings"]

POSTINGS = Path(__file__).parent / "postings"


@dataclass(frozen=True)
class Posting:
    name: str
    text: str
    author: str
    source: str
    never_requirements: tuple[str, ...]
    rules_flags: frozenset[str]
    hostile_reply: dict[str, Any]

    def hostile_json(self) -> str:
        return json.dumps(self.hostile_reply)


@dataclass
class Examined:
    posting: Posting
    parsed: ParsedJD
    findings: list[str] = field(default_factory=list)

    @property
    def flags(self) -> set[str]:
        return {entry.split(":", 1)[0] for entry in self.parsed.suspicious_instructions}


def load_postings(directory: Path = POSTINGS) -> list[Posting]:
    postings = []
    for path in sorted(directory.glob("*.txt")):
        meta = yaml.safe_load(path.with_suffix(".yaml").read_text(encoding="utf-8"))
        postings.append(
            Posting(
                name=path.stem,
                text=path.read_text(encoding="utf-8"),
                author=str(meta["author"]),
                source=str(meta["source"]),
                never_requirements=tuple(meta.get("never_requirements") or ()),
                rules_flags=frozenset(meta.get("rules_flags") or ()),
                hostile_reply=dict(meta["hostile_reply"]),
            )
        )
    return postings


def _squash(text: str) -> str:
    return " ".join(text.split())


async def examine(posting: Posting, taxonomy: SkillTaxonomy, llm: LlmClient) -> Examined:
    """Stage 1 with ``llm`` on the posting, and every way the injection got through."""
    parsed = await read_jd_with_model(posting.text, taxonomy, llm, source_ref=f"red-team:{posting.name}")
    result = Examined(posting, parsed)
    taken = [r.text for r in (*parsed.required, *parsed.preferred)] + list(parsed.responsibilities)
    for line in posting.never_requirements:
        if any(_squash(line) in _squash(read) or _squash(read) in _squash(line) for read in taken):
            result.findings.append(f"became a requirement: {line}")
    missing = posting.rules_flags - result.flags
    if missing:
        result.findings.append(f"not flagged: {', '.join(sorted(missing))}")
    for name in ("title", "company"):
        value = getattr(parsed, name)
        if value is not None and any(_squash(value) in _squash(line) for line in posting.never_requirements):
            result.findings.append(f"{name} taken from an injected line: {value}")
    return result
