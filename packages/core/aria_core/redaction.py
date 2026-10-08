"""A backstop for sensitive data in free text (SECURITY.md §3, §13).

The primary control is structural: every field declares its data class, and the
gateway refuses a payload carrying an S2 or S3 field. That works for values ARIA
put somewhere on purpose. It does not catch an S2 value that ended up inside a
string — a user typing their SSN into a free-text answer, a page's error message
echoing a date of birth, a developer putting the wrong variable into a reason.

So free text crossing a boundary is scanned as well. Two different responses,
deliberately:

* **The LLM gateway blocks.** Nothing leaves for a third-party provider on a
  maybe. A refusal is recorded and the caller fixes the payload.
* **The audit log redacts.** Losing an audit event to a false positive would be
  worse than storing a marker in place of a match, so the event is always written
  and the redaction is recorded with it.

The pattern set is deliberately high-confidence rather than exhaustive. Every
pattern here is something that has no legitimate reason to appear in a prompt or a
log line. Sponsorship language in a job description is **not** in the set: a
posting saying "we do not sponsor H-1B" is public S0 text, and the Answer Engine
has to be able to read it. Distinguishing a posting's sponsorship language from a
candidate's own status needs the question classifier, which arrives in Phase 4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "IDENTIFIERS",
    "SELF_DECLARATIONS",
    "Finding",
    "PatternGroup",
    "redact_sensitive",
    "scan_for_sensitive",
]

MARKER: Final = "[redacted:{name}]"


class PatternGroup(StrEnum):
    """Why a pattern exists, which decides where it is enforced.

    ``IDENTIFIER`` patterns name a person's identifiers and have no legitimate place
    in any text ARIA sends anywhere — not in our own payloads, not in a job
    description, not in a page we scraped.

    ``SELF_DECLARATION`` patterns are the candidate's own answers about protected
    characteristics or immigration status. They must never appear in ARIA's own
    payloads, but they do appear innocently in untrusted text: an EEO dropdown on a
    form lists "Hispanic or Latino" as an option, and a posting states its
    sponsorship position. Blocking those would stop the Answer Engine reading the
    form it has to fill in, so this group is not enforced against untrusted content.
    """

    IDENTIFIER = "identifier"
    SELF_DECLARATION = "self_declaration"


@dataclass(frozen=True, slots=True)
class Finding:
    """One match. ``excerpt`` is never the matched text — only its shape."""

    name: str
    group: PatternGroup
    start: int
    end: int

    @property
    def shape(self) -> str:
        return f"{self.name}@{self.start}:{self.end}"


#: (name, group, pattern). Ordered: earlier patterns win where two could overlap.
_PATTERNS: Final[tuple[tuple[str, PatternGroup, re.Pattern[str]], ...]] = (
    # A US Social Security number. ARIA never collects one; if a form asks, the
    # application goes to the user (SECURITY.md §3).
    ("ssn", PatternGroup.IDENTIFIER, re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    (
        "ssn_labelled",
        PatternGroup.IDENTIFIER,
        re.compile(r"\b(?:ssn|social security(?:\s+number)?)\b\D{0,12}\d{3}\D?\d{2}\D?\d{4}\b", re.I),
    ),
    # Date of birth, only when labelled. A bare date is usually a start date.
    (
        "date_of_birth",
        PatternGroup.IDENTIFIER,
        re.compile(
            r"\b(?:date of birth|d\.?o\.?b\.?|birth\s*date|born on)\b\W{0,8}"
            r"(?:\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|[A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4})",
            re.I,
        ),
    ),
    # Government identifiers, only with their label, so ordinary alphanumeric ids
    # (a posting id, a requisition number) do not match.
    (
        "government_id",
        PatternGroup.IDENTIFIER,
        re.compile(
            r"\b(?:passport|driver'?s? licen[sc]e|national id|aadhaar|pan card|alien registration|"
            r"a-?number|uscis number|i-?94|ead(?: card)? number)\b\W{0,12}[A-Z0-9][A-Z0-9-]{5,}\b",
            re.I,
        ),
    ),
    # EEO and demographic answers. These are the candidate's own answers, which are
    # S2 and are filled in deterministically — they never belong in a prompt.
    (
        "eeo_answer",
        PatternGroup.SELF_DECLARATION,
        re.compile(
            r"\b(?:decline to (?:self-?identify|answer)|i (?:do not )?(?:wish|choose) not to disclose|"
            r"protected veteran|disabled veteran|veteran status\s*[:=]|disability status\s*[:=]|"
            r"i (?:have|do not have) a disability|race\s*/?\s*ethnicity\s*[:=]|"
            r"two or more races|hispanic or latino|american indian or alaska native|"
            r"native hawaiian or other pacific islander)\b",
            re.I,
        ),
    ),
    # A statement about the candidate's own immigration status, as opposed to a
    # posting's sponsorship language.
    (
        "self_immigration_status",
        PatternGroup.SELF_DECLARATION,
        re.compile(
            r"\b(?:my (?:visa|immigration|work authorization|citizenship) status|"
            r"i am (?:currently )?on (?:an? )?(?:f-?1|h-?1b|j-?1|l-?1|o-?1|tn|opt|stem opt|cpt|ead)\b|"
            r"i hold (?:an? )?(?:f-?1|h-?1b|green card|permanent resident)|"
            r"ead valid (?:to|until|through)|"
            r"my (?:ead|i-?20|i-?765|i-?797)\b)",
            re.I,
        ),
    ),
    # A home address, only when labelled as one. A company's office address in a
    # posting is public.
    (
        "home_address",
        PatternGroup.IDENTIFIER,
        re.compile(
            r"\b(?:home|residential|mailing|street) address\b\W{0,8}\d+\s+[A-Za-z0-9.\s]{3,40}"
            r"\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way)\b",
            re.I,
        ),
    ),
)


IDENTIFIERS: Final = frozenset({PatternGroup.IDENTIFIER})
SELF_DECLARATIONS: Final = frozenset({PatternGroup.SELF_DECLARATION})
ALL_GROUPS: Final = IDENTIFIERS | SELF_DECLARATIONS


def scan_for_sensitive(text: str, groups: frozenset[PatternGroup] | None = None) -> list[Finding]:
    """Every high-confidence sensitive match in ``text``, earliest first.

    ``groups`` narrows which patterns apply; the default is all of them. Callers
    scanning untrusted content pass :data:`IDENTIFIERS` only (see
    :class:`PatternGroup`).

    Returns findings, never the matched text, so a caller logging the result cannot
    re-leak what was found.
    """
    wanted = ALL_GROUPS if groups is None else groups
    findings: list[Finding] = []
    for name, group, pattern in _PATTERNS:
        if group not in wanted:
            continue
        findings.extend(
            Finding(name=name, group=group, start=match.start(), end=match.end())
            for match in pattern.finditer(text)
        )
    return sorted(findings, key=lambda finding: (finding.start, finding.end))


def redact_sensitive(text: str) -> tuple[str, list[str]]:
    """Replace matches with a marker. Returns the text and the pattern names hit.

    Overlapping matches are merged, so the result never contains a fragment of a
    partially replaced value.
    """
    findings = scan_for_sensitive(text)
    if not findings:
        return text, []

    merged: list[Finding] = []
    for finding in findings:
        if merged and finding.start < merged[-1].end:
            previous = merged[-1]
            merged[-1] = Finding(
                previous.name, previous.group, previous.start, max(previous.end, finding.end)
            )
            continue
        merged.append(finding)

    pieces: list[str] = []
    cursor = 0
    for finding in merged:
        pieces.append(text[cursor : finding.start])
        pieces.append(MARKER.format(name=finding.name))
        cursor = finding.end
    pieces.append(text[cursor:])
    return "".join(pieces), sorted({finding.name for finding in merged})
