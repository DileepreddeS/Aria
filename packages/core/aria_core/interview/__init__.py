"""Onboarding interviews: filling the gaps the resume did not cover.

Two interviews, both template-driven (``docs/DECISIONS.md``):

:mod:`enrichment`
    Targeted questions about gaps that are measurably there — a role with no
    achievements under it, a bullet with no number, a listed skill no fact mentions,
    a missing contact detail. Answers become **new draft facts** beside the
    originals (RESUME_ENGINE §5), never edits of them.

:mod:`motivations`
    Six fixed questions about what the candidate wants, which the Answer Engine needs
    and must never invent.

Neither writes to the graph. Both read it, decide what to ask, and put questions on
the one queue in :mod:`aria_core.graph.questions`; the answers are applied there, so
every answer in the product follows the same lifecycle.
"""

from __future__ import annotations

from aria_core.interview.enrichment import Gap, find_gaps, run_enrichment
from aria_core.interview.motivations import answered_kinds, run_motivations_interview
from aria_core.interview.templates import (
    ENRICHMENT_PROMPTS,
    MOTIVATION_PROMPTS,
    PROFILE_FIELD_PROMPTS,
    Prompt,
    QuestionTemplate,
)

__all__ = [
    "ENRICHMENT_PROMPTS",
    "MOTIVATION_PROMPTS",
    "PROFILE_FIELD_PROMPTS",
    "Gap",
    "Prompt",
    "QuestionTemplate",
    "answered_kinds",
    "find_gaps",
    "run_enrichment",
    "run_motivations_interview",
]
