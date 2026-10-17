"""What the engine's stages hand each other.

Only the writer's bullet so far: the truth check (6b) judges its declarations. The other
stage types (ParsedJD, EvidenceMap, ResumePlan, ResumeResult) arrive with their stages.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import Field

from aria_core.schemas.base import AriaMessage
from aria_core.sensitivity import Sensitivity

__all__ = ["Bullet"]


class Bullet(AriaMessage):
    """One bullet as Stage 5b writes it (RESUME_ENGINE §8), with what it says it used.

    Every declaration is checked, never trusted (§9 6b): the facts must be ones the slot
    was given, the metrics exact strings from those facts, the technologies on the
    allowed list. The model proposes; :func:`aria_resume.validate.truth.check_written`
    decides.
    """

    text: Annotated[str, Sensitivity.S1] = Field(min_length=1, max_length=400)
    fact_ids: Annotated[tuple[uuid.UUID, ...], Sensitivity.S0] = Field(min_length=1)
    #: Exact strings from the cited facts' metrics: "30%", "500K+".
    metrics_used: Annotated[tuple[str, ...], Sensitivity.S1] = ()
    tech_used: Annotated[tuple[str, ...], Sensitivity.S1] = ()
    requirement_ids: Annotated[tuple[str, ...], Sensitivity.S0] = ()
