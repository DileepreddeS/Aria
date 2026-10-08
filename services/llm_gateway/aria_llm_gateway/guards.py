"""What the gateway refuses to send (SECURITY.md §3, §13).

Two checks, in order of how much they can be trusted.

**Structural.** Every message field declares its data class, so a payload carrying
an S2 or S3 field is refused without looking at any value. This is reliable: it
depends on an annotation, not on a regular expression.

**Pattern.** Free text is scanned as a backstop, for the S2 value that ended up
inside a string — a user typing their SSN into an open-ended answer, a page echoing
a date of birth. Unlike the audit log, which redacts, the gateway blocks: nothing
goes to a third-party provider on a maybe.

The two kinds of text are scanned differently. ARIA's own payload must be clean of
everything. Untrusted content is scanned for identifiers only, because an EEO
dropdown legitimately lists "Hispanic or Latino" and a posting legitimately states
its sponsorship position — blocking those would stop the Answer Engine reading the
form it has to fill in (see :class:`aria_core.redaction.PatternGroup`).

**A refusal records the pattern name and that it was blocked. It never records the
matched value, the surrounding text, or an excerpt.** A log of what was too
sensitive to send would be a strange place to keep it.
"""

from __future__ import annotations

from dataclasses import dataclass

from aria_core.redaction import IDENTIFIERS, scan_for_sensitive
from aria_core.schemas.base import AriaMessage
from aria_core.sensitivity import Sensitivity
from aria_llm_gateway.schemas import CompletionRequest, RefusalReason

__all__ = ["Refusal", "inspect_request", "reject_sensitive_fields"]


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a request was not dispatched. Carries names, never values."""

    reason: RefusalReason
    #: Field names, or pattern names — whichever the check produced.
    details: tuple[str, ...]
    #: Where it was found: "inputs", "trusted_instructions", "untrusted_content".
    location: str

    @property
    def code(self) -> str:
        """A stable string for the ``llm_requests`` row and the audit payload."""
        return f"{self.reason.value}:{','.join(self.details)}"

    def __str__(self) -> str:
        return f"refused ({self.reason.value}) in {self.location}: {', '.join(self.details)}"


def reject_sensitive_fields(message: AriaMessage) -> Refusal | None:
    """Refuse a message carrying any field classified S2 or S3.

    Works on any ARIA message, not just the gateway's own request type, so the rule
    keeps holding as new payload shapes are added.
    """
    present = message.fields_at_or_above(Sensitivity.S2)
    if not present:
        return None
    return Refusal(
        reason=RefusalReason.SENSITIVE_FIELD,
        details=tuple(sorted(present)),
        location=type(message).__name__,
    )


def inspect_request(request: CompletionRequest) -> Refusal | None:
    """The whole check. ``None`` means the request may be dispatched."""
    structural = reject_sensitive_fields(request)
    if structural is not None:
        return structural

    # ARIA's own text: every pattern applies.
    own_text = {
        "trusted_instructions": request.trusted_instructions,
        **{f"inputs.{key}": value for key, value in request.inputs.items()},
    }
    for location, text in own_text.items():
        findings = scan_for_sensitive(text)
        if findings:
            return Refusal(
                reason=RefusalReason.SENSITIVE_PATTERN,
                details=tuple(sorted({finding.name for finding in findings})),
                location=location,
            )

    # Untrusted content: identifiers only.
    for index, content in enumerate(request.untrusted_content):
        findings = scan_for_sensitive(content.text, IDENTIFIERS)
        if findings:
            return Refusal(
                reason=RefusalReason.SENSITIVE_PATTERN,
                details=tuple(sorted({finding.name for finding in findings})),
                location=f"untrusted_content[{index}]:{content.source_kind.value}",
            )

    return None
