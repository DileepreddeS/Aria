"""ARIA Resume Engine (RESUME_ENGINE.md).

One job description and one :class:`~aria_core.schemas.snapshot.CandidateSnapshot` in;
a one-page, ATS-safe resume out, every claim traceable to an approved fact. The engine
is pure (DECISIONS 2026-10-16): it reads no database and calls no model provider. The
snapshot holds approved rows only, and model calls go through the
:class:`~aria_resume.llm.LlmClient` the caller supplies.
"""

from __future__ import annotations

__all__ = ["ENGINE_VERSION"]

#: Recorded in every resume's artifacts, beside the taxonomy, template, prompt and model
#: versions, so a resume can be reproduced and explained later (RESUME_ENGINE §1 rule 10).
ENGINE_VERSION = "0.1.0"
