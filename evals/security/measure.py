"""Measurements behind the red-team defences (DECISIONS 2026-10-22), printed, never gated.

``python -m evals.security.measure recall`` - the model step's recall on benign prose postings
    (``benign/``), reading each with an honest scripted reply under each quote unit, so the
    cost of refusing fragments is a number rather than a guess.
``python -m evals.security.measure false-positives`` - every line of the benign postings and
    the synthetic resume postings through the screen; a flagged line is never taken, so a
    false flag silently drops a real requirement.
``python -m evals.security.measure bipia --half train`` - BIPIA's text attacks at a pinned
    commit, fetched and hash-checked, never stored here: how many the screen flags, and how
    many could still become a requirement with every defence in place. The train half is the
    held-out half: the patterns were written from the test half only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from aria_resume.jd.clean import clean
from aria_resume.jd.model_step import read_jd_with_model
from aria_resume.jd.sections import Item, split_items
from aria_resume.jd.suspicious import screen
from aria_resume.llm import LlmPurpose, ScriptedLlm

from aria_core.taxonomy.loader import load_default_taxonomy

HERE = Path(__file__).parent
BENIGN = HERE / "benign"
SYNTHETIC = HERE.parent / "resume" / "synthetic"
#: Tidewell's lines that are injected on purpose (true positives, not false ones).
KNOWN_INJECTED = ("Note to AI assistants", "please email a copy of your passport")

BIPIA_COMMIT = "a004b69ec0dd446e0afd461d98cb5e96e120a5d0"
BIPIA_FILES = {
    "test": ("text_attack_test.json", "75750e7b4e8b34e8f9d88d89b357aeaaf02bd07f9e493ccd37eda74a0cd7c7f8"),
    "train": ("text_attack_train.json", "63f95d3e67eac4178cdabdbdaf192cd05f2b6ed0702b578f1d30d556e5155670"),
}


def _squash(text: str) -> str:
    return " ".join(text.split())


def _reply(meta: dict[str, Any], **override: Any) -> str:
    reading = {
        "title": None,
        "company": None,
        "required": list(meta.get("requirements") or []),
        "preferred": list(meta.get("preferred") or []),
        "responsibilities": list(meta.get("responsibilities") or []),
        "suspicious": [],
    }
    return json.dumps(reading | override)


async def recall() -> None:
    taxonomy = load_default_taxonomy()
    print("posting | expected | substring | sentence | line")
    totals = {"substring": 0, "sentence": 0, "line": 0}
    expected_total = 0
    for path in sorted(BENIGN.glob("*.txt")):
        meta = yaml.safe_load(path.with_suffix(".yaml").read_text(encoding="utf-8"))
        expected = [
            *meta.get("requirements", []),
            *meta.get("preferred", []),
            *meta.get("responsibilities", []),
        ]
        expected_total += len(expected)
        row = [path.stem, str(len(expected))]
        for unit in ("substring", "sentence", "line"):
            llm = ScriptedLlm({LlmPurpose.PARSE_JD: [_reply(meta)]})
            parsed = await read_jd_with_model(
                path.read_text(encoding="utf-8"), taxonomy, llm, source_ref="measure", quote_unit=unit
            )
            read = [_squash(r.text) for r in (*parsed.required, *parsed.preferred)] + [
                _squash(line) for line in parsed.responsibilities
            ]
            found = sum(1 for line in expected if any(_squash(line) in r for r in read))
            totals[unit] += found
            row.append(str(found))
        print(" | ".join(row))
    print(
        " | ".join(
            ["total", str(expected_total), *(str(totals[u]) for u in ("substring", "sentence", "line"))]
        )
    )


def false_positives() -> None:
    lines: list[tuple[str, Item]] = []
    for directory in (BENIGN, SYNTHETIC):
        for path in sorted(directory.glob("*.txt")):
            for item in split_items(clean(path.read_text(encoding="utf-8")).text):
                if not any(marker in item.text for marker in KNOWN_INJECTED):
                    lines.append((path.stem, item))
    flagged = screen([item for _, item in lines])
    print(f"benign lines screened: {len(lines)}; flagged: {len(flagged)}")
    for kind, item in flagged:
        source = next(name for name, candidate in lines if candidate is item)
        print(f"  FALSE POSITIVE [{kind}] {source}: {item.text}")


def _bipia(half: str) -> dict[str, list[str]]:
    name, digest = BIPIA_FILES[half]
    url = f"https://raw.githubusercontent.com/microsoft/BIPIA/{BIPIA_COMMIT}/benchmark/{name}"
    # A fixed https URL at a pinned commit, checked against its digest below.
    # nosemgrep: dynamic-urllib-use-detected
    with urllib.request.urlopen(url, timeout=30) as response:
        body = response.read()
    if hashlib.sha256(body).hexdigest() != digest:
        raise SystemExit(f"{name} does not match its pinned SHA-256; refusing to measure")
    data: dict[str, list[str]] = json.loads(body)
    return data


async def bipia(half: str) -> None:
    taxonomy = load_default_taxonomy()
    attacks = [(category, attack) for category, items in _bipia(half).items() for attack in items]
    flagged = through = 0
    misses: list[str] = []
    for category, attack in attacks:
        # Placed as BIPIA places them, in the external content: here a line of prose.
        posting = (
            f"Data Engineer, Halvard Freight\nHalvard Freight moves cargo.\n{attack}\n\n"
            "Requirements:\n- Python\n"
        )
        items = split_items(clean(posting).text)
        hit = any(attack in item.text for _, item in screen(items))
        flagged += hit
        hostile = json.dumps(
            {
                "title": None,
                "company": None,
                "required": [attack],
                "preferred": [],
                "responsibilities": [attack],
                "suspicious": [],
            }
        )
        parsed = await read_jd_with_model(
            posting, taxonomy, ScriptedLlm({LlmPurpose.PARSE_JD: [hostile]}), source_ref="bipia"
        )
        taken = any(_squash(attack) in _squash(r.text) for r in parsed.required) or any(
            _squash(attack) in _squash(line) for line in parsed.responsibilities
        )
        through += taken
        if not hit:
            misses.append(f"  not flagged{' and TAKEN' if taken else ''}: [{category}] {attack}")
    print(f"BIPIA {half} half ({BIPIA_COMMIT[:7]}): {len(attacks)} attacks")
    print(f"  flagged by the screen: {flagged}/{len(attacks)}")
    print(f"  taken as a requirement or responsibility under a hostile reply: {through}/{len(attacks)}")
    print("\n".join(misses))


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="Red-team measurements (printed, never gated).")
    parser.add_argument("what", choices=["recall", "false-positives", "bipia"])
    parser.add_argument("--half", choices=["test", "train"], default="test")
    args = parser.parse_args(argv)
    if args.what == "recall":
        asyncio.run(recall())
    elif args.what == "false-positives":
        false_positives()
    else:
        asyncio.run(bipia(args.half))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
