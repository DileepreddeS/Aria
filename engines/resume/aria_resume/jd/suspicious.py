"""The rules' half of the suspicious-instruction classifier (SECURITY.md §5, layer 4).

A posting asks for skills and experience. Anything else it asks of the reader is a signal:
documents (passport, social security number, bank details), a payment, a move to a
messaging app, or words addressed to an AI that reads the page. These are quoted and
reported, never followed, and a line that carries one is never read as a requirement.
The model step adds what patterns miss; this half needs no model and cannot be talked
out of its answer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from aria_resume.jd.sections import Item

__all__ = ["KINDS", "screen"]

KINDS: dict[str, re.Pattern[str]] = {
    "addressed_to_ai": re.compile(
        r"\b(?:ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions|"
        r"(?:note|message|instructions?)\s+to\s+"
        r"(?:ai|llms?|chatgpt|language models?|assistants?|ai assistants?)|"
        r"if\s+you\s+are\s+an?\s+(?:ai|bot|language model|llm|assistant)|"
        r"as\s+an?\s+(?:ai|language model)|system\s+prompt|"
        r"rate\s+(?:this|the)\s+(?:applicant|candidate)|perfect\s+match)\b",
        re.IGNORECASE,
    ),
    "asks_for_documents": re.compile(
        r"\b(?:passport|social\s+security(?:\s+number)?|ssn|bank\s+(?:account|details|information|statement)|"
        r"routing\s+number|driver'?s\s+licen[cs]e|date\s+of\s+birth)\b",
        re.IGNORECASE,
    ),
    "asks_for_payment": re.compile(
        r"\b(?:pay\s+(?:a|the|an)\s+(?:\w+\s+)?(?:fee|deposit)|processing\s+fee|training\s+fee|"
        r"gift\s+cards?|wire\s+transfer|western\s+union|bitcoin|crypto(?:currency)?\s+payment)\b",
        re.IGNORECASE,
    ),
    "moves_off_site": re.compile(
        r"\b(?:telegram|whatsapp|signal\s+app|text\s+(?:us|me)\s+at)\b", re.IGNORECASE
    ),
}


def screen(items: Sequence[Item]) -> list[tuple[str, Item]]:
    """``(kind, item)`` for every item that asks something unrelated to applying."""
    flagged: list[tuple[str, Item]] = []
    for item in items:
        for kind, pattern in KINDS.items():
            if pattern.search(item.text):
                flagged.append((kind, item))
                break
    return flagged
