"""The page as printed decides: re-plan until the resume prints on one page, or say it does not.

The line budget is an estimate (:mod:`.line_budget`): Carlito's widths, without kerning, at
97% of the line. RESUME_ENGINE §8 5a says to re-check after rendering, and this is the
re-check. ``build`` plans for a page and prints the result; when the print runs past one
page, how far the second page runs is measured from the printed text, the budget shrinks
by that much and one line more, and the plan is made again, for a few rounds. The planner's
own rules decide what goes (lowest value first, never one bullet on a role). A plan that
still does not fit comes back with ``fits`` false: the caller returns ``needs_review``,
never a two-page "one-page" resume.

A page set in the wrong font (Carlito failed to load) is not this loop's to fix: 6d refuses
it as ``font_substituted``, because no re-plan makes a wrong font's widths right.
"""

from __future__ import annotations

import dataclasses
import io
from collections.abc import Callable
from dataclasses import dataclass

import pdfplumber
from pypdf import PdfReader

from aria_resume.compose.line_budget import PAGE, Page
from aria_resume.schemas import ResumePlan

__all__ = ["Fitted", "fit_to_page", "overflow"]

MAX_ROUNDS = 6


@dataclass(frozen=True, slots=True)
class Fitted:
    plan: ResumePlan
    pdf: bytes
    #: How many times the page was printed.
    rounds: int
    fits: bool


def overflow(pdf: bytes) -> float:
    """Points of text past the first page: from the top margin to the lowest line on page two
    and beyond. 0 for a one-page print. Measured against the paper, which never shrinks."""
    pages = len(PdfReader(io.BytesIO(pdf)).pages)
    if pages <= 1:
        return 0.0
    total = 0.0
    with pdfplumber.open(io.BytesIO(pdf)) as opened:
        for extra in opened.pages[1:]:
            words = extra.extract_words()
            margin = (float(extra.height) - PAGE.height) / 2
            total += max((float(word["bottom"]) for word in words), default=margin + PAGE.line) - margin
    return total


def fit_to_page(
    build: Callable[[Page], tuple[ResumePlan, bytes]],
    *,
    page: Page = PAGE,
    max_rounds: int = MAX_ROUNDS,
) -> Fitted:
    current = page
    for round_number in range(1, max_rounds + 1):
        plan, pdf = build(current)
        over = overflow(pdf)
        if over <= 0:
            return Fitted(plan, pdf, round_number, fits=True)
        # The plan used what it estimated, and the print ran ``over`` past the page: so the room
        # it may fill next time is what it used, less the spill and a line more. (Shrinking the
        # budget instead would change nothing while the budget sat above what the plan used.)
        usable = plan.estimated_height - over - current.line
        current = dataclasses.replace(current, height=usable + current.slack_lines * current.line)
    return Fitted(plan, pdf, max_rounds, fits=False)
