"""The line budget's estimate, held against the print on the platform the test runs on.

CI runs this on Linux, where production prints; ./tasks.ps1 check runs it on Windows. The
width margin was calibrated on Linux (DECISIONS 2026-10-29): Chromium sets Carlito narrower
there, so a margin that suits Windows leaves room unused on every Linux page.
"""

from __future__ import annotations

import pytest
from aria_resume.compose.line_budget import PAGE
from evals.line_budget.measure import CALIBRATION_SEED, HELD_OUT_SEED, corpus, measure, print_corpus

pytestmark = pytest.mark.chromium


@pytest.fixture(scope="module", params=[CALIBRATION_SEED, HELD_OUT_SEED], ids=["calibration", "held_out"])
def printed(request: pytest.FixtureRequest) -> tuple[list[str], bytes]:
    texts = corpus(request.param)
    return texts, print_corpus(texts)


def test_no_bullet_is_estimated_shorter_than_it_prints(printed: tuple[list[str], bytes]) -> None:
    """Short means the page runs long: the fit loop catches it, but at the cost of a print."""
    texts, pdf = printed
    [result], _ = measure(texts, pdf, [PAGE.width_margin])
    assert result.short == 0, result


def test_the_estimate_is_exact_for_nearly_every_bullet(printed: tuple[list[str], bytes]) -> None:
    """Long means room left unused: the old 97% margin was exact for 89% of bullets on Linux."""
    texts, pdf = printed
    [result], _ = measure(texts, pdf, [PAGE.width_margin])
    assert result.exact_share >= 0.95, result
