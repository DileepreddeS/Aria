"""Stage 6: the validators (RESUME_ENGINE §9). Code first; a model only where the spec says so."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Finding"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One failed check, said so a person (or the repair loop) can act on it."""

    check: str
    detail: str
