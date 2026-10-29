"""The line budget's estimate against Chromium's print, bullet by bullet (DECISIONS 2026-10-29).

The planner estimates how many lines each bullet wraps to from Carlito's advance widths
(:mod:`aria_resume.compose.line_budget`) before anything is printed. This prints a corpus of
bullets through the real template and the real printer, reads each bullet's lines back
from the PDF, and counts how often the estimate is exact, short (the page runs long and the
fit loop must plan again) or long (room left unused). It also reports how wide Chromium
sets a full line against the advance widths of the same text, which is what the width
margin is calibrated from.

Chromium's line breaking differs by platform: the same page wraps to fewer lines on Linux,
where CI and production run, than on Windows (GitHub CI run #28). Run this on each:

    uv run python -m evals.line_budget.measure [--margin 1.0 --margin 1.005 ...]

The corpora are synthetic: words from the synthetic eval postings in random order, at
lengths from one to four lines, from two fixed seeds. The calibration seed chose the
margin; the held-out seed checks it. evals/tests/test_line_budget.py holds the result.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import random
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pdfplumber
from aria_resume.compose.line_budget import PAGE, Page, bullet_lines
from aria_resume.compose.metrics import carlito
from aria_resume.document import (
    DocumentBullet,
    DocumentEntry,
    DocumentSection,
    ResumeDocument,
    SectionKind,
)
from aria_resume.render.html import render_html
from aria_resume.render.pdf import ChromiumPrinter

__all__ = ["CALIBRATION_SEED", "HELD_OUT_SEED", "Measurement", "corpus", "measure", "print_corpus"]

POSTINGS = Path(__file__).resolve().parents[1] / "resume" / "synthetic"
#: The seed the width margin was chosen on, and the one it was checked on.
CALIBRATION_SEED = 20261029
HELD_OUT_SEED = 7
#: The page's left edge in points (resume.css: 0.6in margins).
LEFT = 0.6 * 72
#: A bullet's wrapped lines start under its text, right of the bullet character.
CONTINUATION_X = LEFT + PAGE.bullet_padding - 1.0
BULLET = "\u2022"


def corpus(seed: int, size: int = 600) -> list[str]:
    words: list[str] = []
    for path in sorted(POSTINGS.glob("*.txt")):
        words += re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#%/-]*", path.read_text(encoding="utf-8"))
    rng = random.Random(seed)  # noqa: S311  (a fixed, reproducible corpus; nothing secret)
    texts = []
    for _ in range(size):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(4, 62)))
        texts.append(text[:400].rstrip())
    return texts


def _document(texts: Sequence[str]) -> ResumeDocument:
    entries = tuple(
        DocumentEntry(
            title=f"Role {start // 20 + 1}",
            detail="Engineer",
            dates="2024",
            bullets=tuple(DocumentBullet(text=text) for text in texts[start : start + 20]),
        )
        for start in range(0, len(texts), 20)
    )
    return ResumeDocument(
        name="Jordan A. Rivera", sections=(DocumentSection(kind=SectionKind.EXPERIENCE, entries=entries),)
    )


def print_corpus(texts: Sequence[str]) -> bytes:
    """The corpus as bullets on as many pages as it takes, through the production printer."""
    return asyncio.run(ChromiumPrinter().print_pdf(render_html(_document(texts))))


def printed_lines(pdf: bytes) -> list[list[tuple[str, float]]]:
    """Each bullet's printed lines, in order, as (text, width in points)."""
    bullets: list[list[tuple[str, float]]] = []
    with pdfplumber.open(BytesIO(pdf)) as opened:
        for page in opened.pages:
            rows: dict[int, list[dict[str, object]]] = {}
            for word in page.extract_words():
                rows.setdefault(round(float(word["top"])), []).append(word)
            for top in sorted(rows):
                words = sorted(rows[top], key=lambda word: float(word["x0"]))  # type: ignore[arg-type]
                text = " ".join(str(word["text"]) for word in words)
                width = float(words[-1]["x1"]) - float(words[0]["x0"])  # type: ignore[arg-type]
                if text.startswith(BULLET):
                    bullets.append([(text, width)])
                elif float(words[0]["x0"]) > CONTINUATION_X and bullets:  # type: ignore[arg-type]
                    bullets[-1].append((text, width))
    return bullets


@dataclass(frozen=True, slots=True)
class Measurement:
    margin: float
    bullets: int
    exact: int
    #: Estimated fewer lines than printed: the page runs long, and the fit loop plans again.
    short: int
    #: Estimated more lines than printed: room left unused on the page.
    long: int
    estimated_lines: int
    printed_lines: int

    @property
    def exact_share(self) -> float:
        return self.exact / self.bullets


def measure(
    texts: Sequence[str], pdf: bytes, margins: Sequence[float]
) -> tuple[list[Measurement], list[float]]:
    """Per margin, how the estimate compares with the print; and each full line's printed
    width over its advance width (a bullet's last line is short by nature, so it is left out)."""
    bullets = printed_lines(pdf)
    if len(bullets) != len(texts):
        raise ValueError(f"read {len(bullets)} bullets from the print, expected {len(texts)}")
    for text, lines in zip(texts, bullets, strict=True):
        # Spaces aside: Chromium also breaks after a hyphen ("end-to-" / "end"), which the
        # estimate never does, so a line can end inside a word. That only ever makes the
        # print shorter than the estimate.
        read = "".join(line for line, _ in lines).removeprefix(BULLET)
        if "".join(read.split()) != "".join(text.split()):
            raise ValueError(f"the print's bullet does not read back as the corpus text: {text[:60]!r}")
    regular = carlito("Regular")
    ratios = [
        width / regular.width(line.replace(f"{BULLET} ", f"{BULLET}\u00a0"), PAGE.size)
        for lines in bullets
        for line, width in lines[:-1]
    ]
    printed = [len(lines) for lines in bullets]
    results = []
    for margin in margins:
        page: Page = dataclasses.replace(PAGE, width_margin=margin)
        estimated = [bullet_lines(text, page) for text in texts]
        pairs = list(zip(estimated, printed, strict=True))
        results.append(
            Measurement(
                margin=margin,
                bullets=len(texts),
                exact=sum(e == p for e, p in pairs),
                short=sum(e < p for e, p in pairs),
                long=sum(e > p for e, p in pairs),
                estimated_lines=sum(estimated),
                printed_lines=sum(printed),
            )
        )
    return results, ratios


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--margin", type=float, action="append", help="width margins to compare")
    parser.add_argument("--out", type=Path, help="keep each corpus's PDF in this directory")
    args = parser.parse_args(argv)
    margins = args.margin or [PAGE.width_margin]
    for name, seed in (("calibration", CALIBRATION_SEED), ("held out", HELD_OUT_SEED)):
        texts = corpus(seed)
        pdf = print_corpus(texts)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / f"line-budget-{seed}.pdf").write_bytes(pdf)
        results, ratios = measure(texts, pdf, margins)
        print(
            f"{name} corpus (seed {seed}): {len(texts)} bullets. Printed width over advance width, "
            f"{len(ratios)} full lines: median {statistics.median(ratios):.4f}, "
            f"range {min(ratios):.4f}-{max(ratios):.4f}"
        )
        for result in results:
            print(
                f"  margin {result.margin:.3f}: exact {result.exact_share:6.1%}  short {result.short:3d}  "
                f"long {result.long:3d}  lines estimated {result.estimated_lines} / printed "
                f"{result.printed_lines}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
