"""Data classification (SECURITY.md §3).

Every field of every ARIA message declares the class of data it carries::

    class CandidateContact(AriaMessage):
        full_name: Annotated[str, Sensitivity.S1]
        visa_status: Annotated[str, Sensitivity.S2]

The annotation is the single source of truth. The log redactor, the API response
serializer and the LLM gateway all read it from here instead of keeping their own
lists of field names, because three lists drift and one does not.

``AriaMessage`` refuses to be defined if a field omits its class, so "I forgot to
classify this column" is a failure at import time rather than a leak at runtime.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import IntEnum
from typing import Annotated, Any, get_args, get_origin

__all__ = ["Sensitivity", "sensitivity_of_annotation"]


class Sensitivity(IntEnum):
    """How sensitive one field is. Ordered, so ``max()`` is meaningful.

    The handling rules per class are in SECURITY.md §3; the ones enforced in code:

    ===== ============================================ ========= ==========
    class examples                                     to an LLM in logs
    ===== ============================================ ========= ==========
    S0    job postings, company data, taxonomy         yes       yes
    S1    name, email, phone, work history, facts      minimum   as an id
    S2    visa status, EEO answers, DOB, home address  never     never
    S3    passwords, OAuth tokens, cookies, API keys   never     never
    ===== ============================================ ========= ==========
    """

    S0 = 0
    S1 = 1
    S2 = 2
    S3 = 3

    @property
    def may_reach_llm(self) -> bool:
        """S2 and S3 never leave for a third-party model (SECURITY.md §3, §13)."""
        return self <= Sensitivity.S1

    @property
    def may_appear_in_logs(self) -> bool:
        """S1 is reduced to an id by the log processor; S2/S3 are dropped entirely."""
        return self <= Sensitivity.S1

    @property
    def may_reach_frontend(self) -> bool:
        """S2 needs an explicit purpose and re-authentication; S3 never leaves the broker."""
        return self <= Sensitivity.S1

    def __str__(self) -> str:
        return self.name


def _flatten(annotation: Any) -> Iterator[Any]:
    """Yield an annotation and everything nested inside it (``list[X]``, ``X | None``, ...)."""
    yield annotation
    for arg in get_args(annotation):
        if arg is type(None):
            continue
        yield from _flatten(arg)


def sensitivity_of_annotation(annotation: Any) -> Sensitivity | None:
    """Return the ``Sensitivity`` declared anywhere inside a type annotation.

    Looks through ``Annotated``, ``Optional``, containers and unions, so
    ``list[Annotated[str, Sensitivity.S2]] | None`` classifies as S2. When several
    classes appear, the strictest wins — a container is as sensitive as the most
    sensitive thing it can hold.
    """
    found = [part for part in _flatten(annotation) if isinstance(part, Sensitivity)]
    return max(found) if found else None


def annotated_metadata(annotation: Any) -> tuple[Any, ...]:
    """The extra arguments of an ``Annotated[...]``, or an empty tuple."""
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[1:]
    return ()
