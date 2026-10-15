"""Recognising that a second resume describes the same work as the first.

The founder imports two base resumes, and most people have three or four versions of
theirs. The same achievement appears in all of them with different wording, so an
importer without this produces a graph where every role exists twice and the months
are counted twice with it.

Two different jobs, with different confidence:

* **Roles** are matched automatically. The same employer over the same months is the
  same job — that is not a judgement call, and splitting it in two would break every
  figure derived from the calendar. The second import attaches its bullets to the
  role that is already there.
* **Facts** are only ever *proposed* as duplicates. "Built 20+ React components" and
  "Developed 20 components in React" are probably one achievement, but which wording
  to keep is the candidate's call, so the importer asks.

**Two facts whose numbers differ are never duplicates.** "Cut latency by 30%" and "cut
latency by 60%" are similar sentences and different claims, and merging them would
silently pick one number for a resume to say. The numeric check runs before the text
similarity for exactly that reason, and a test covers it.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from enum import StrEnum

from aria_core.graph.months import MonthSpan
from aria_core.importer.metrics import extract_metrics
from aria_core.importer.plan import ProposedExperience, ProposedFact

__all__ = [
    "DuplicateFact",
    "MatchKind",
    "achievement_key",
    "education_key",
    "experience_key",
    "find_duplicate_fact",
    "match_experience",
    "project_key",
    "similarity",
]

#: Words carried by almost every resume bullet. Keeping them would make any two
#: bullets look 40% alike before saying anything about the work.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "by",
        "at",
        "from",
        "as",
        "that",
        "which",
        "it",
        "its",
        "was",
        "were",
        "is",
        "are",
        "be",
        "been",
        "into",
        "over",
        "using",
        "used",
        "via",
        "per",
        "our",
        "my",
        "their",
        "new",
        "across",
        "within",
        "while",
        "also",
    }
)

#: Verb endings folded so "built" and "building" count as the same word. Crude on
#: purpose: a stemmer would be a dependency, and the only job here is to stop two
#: wordings of one achievement looking unrelated.
_VERB_SUFFIXES = ("ing", "ed")

#: Short words keep their endings: stemming "led" or "ads" destroys them.
_MIN_STEM_LENGTH = 5

#: Above this, two bullets are the same sentence in different clothes.
NEAR_DUPLICATE_THRESHOLD = 0.62

#: Above this, one bullet is contained in the other: a longer version of the same
#: claim, which is the usual shape of the same achievement in two resumes.
CONTAINMENT_THRESHOLD = 0.80

#: Content words needed before two bullets can be called the same thing. Containment
#: is meaningless on one or two words -- every short bullet is "contained in" every
#: other -- and a bullet dropped as a duplicate is a bullet nobody reads again.
MIN_COMPARABLE_TOKENS = 3


class MatchKind(StrEnum):
    IDENTICAL = "identical"
    """The same sentence once normalised. Safe to skip without asking."""
    NEAR = "near"
    """Probably the same achievement. Proposed as a merge, never applied silently."""


def _stem(word: str) -> str:
    """Fold a word to a comparison form: plural first, then verb ending.

    Order matters, and getting it wrong is subtle. Trying "es" before "s" turned
    "services" into "servic" while leaving "service" whole, so a bullet about the
    billing service and a bullet about the billing services looked like different
    achievements. A plural "s" comes off first, which lands both on "service".
    """
    if len(word) > _MIN_STEM_LENGTH and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    for suffix in _VERB_SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _tokens(text: str) -> frozenset[str]:
    folded = unicodedata.normalize("NFKC", text).lower()
    words = re.findall(r"[a-z0-9+#.]+", folded)
    return frozenset(_stem(word.strip(".")) for word in words if word.strip(".") and word not in _STOPWORDS)


def _numbers(text: str) -> frozenset[str]:
    """The result-bearing numbers in a line, normalised for comparison."""
    return frozenset(
        metric.exact_value.lower().replace(",", "").replace(" ", "") for metric in extract_metrics(text)
    )


def similarity(left: str, right: str) -> float:
    """How alike two bullets are: 0.0 to 1.0.

    Jaccard overlap of stemmed content words, raised to the containment ratio when one
    bullet is a longer telling of the other — otherwise a detailed version of the same
    achievement scores low purely for having more words in it.
    """
    first, second = _tokens(left), _tokens(right)
    if not first or not second:
        return 0.0
    shared = len(first & second)
    if not shared:
        return 0.0

    jaccard = shared / len(first | second)
    smaller = min(len(first), len(second))
    if smaller < MIN_COMPARABLE_TOKENS:
        return jaccard

    containment = shared / smaller
    return max(jaccard, containment) if containment >= CONTAINMENT_THRESHOLD else jaccard


@dataclass(frozen=True, slots=True)
class DuplicateFact:
    """A proposed fact that looks like one already in the graph."""

    existing_fact_id: uuid.UUID
    existing_text: str
    kind: MatchKind
    score: float

    @property
    def merge_without_asking(self) -> bool:
        return self.kind is MatchKind.IDENTICAL


def _numbers_agree(left: frozenset[str], right: frozenset[str]) -> bool:
    """Whether two bullets state compatible results.

    One telling of an achievement often quantifies more of it than the other: "20
    components used by 4 teams" and "20 components used by several teams" agree about
    every number they both state. So a subset counts as agreement, and only a real
    conflict -- 30% against 60% -- marks two different claims.
    """
    if not left or not right:
        return True
    return left <= right or right <= left


def find_duplicate_fact(
    proposed: ProposedFact, existing: list[tuple[uuid.UUID, str]]
) -> DuplicateFact | None:
    """The best match for ``proposed`` among existing facts, or ``None``.

    Numbers are compared first: if the two lines disagree about a result, they are
    different claims whatever the words do, and no match is returned.
    """
    proposed_numbers = _numbers(proposed.text)
    best: DuplicateFact | None = None

    proposed_tokens = _tokens(proposed.text)
    for fact_id, text in existing:
        if proposed_tokens == _tokens(text):
            # Identical means "skip it without asking", which is only safe when
            # there is enough in the sentence to be sure. Two three-word bullets
            # that reduce to the same single word are not the same claim.
            kind = MatchKind.IDENTICAL if len(proposed_tokens) >= MIN_COMPARABLE_TOKENS else MatchKind.NEAR
            return DuplicateFact(fact_id, text, kind, 1.0)

        existing_numbers = _numbers(text)
        if not _numbers_agree(proposed_numbers, existing_numbers):
            continue

        score = similarity(proposed.text, text)
        if score >= NEAR_DUPLICATE_THRESHOLD and (best is None or score > best.score):
            best = DuplicateFact(fact_id, text, MatchKind.NEAR, score)

    return best


def experience_key(employer: str, span: MonthSpan | None) -> tuple[str, str]:
    """A role's identity: who paid, and when. Enough to recognise the same job.

    The title is deliberately not part of the key. A promotion within one employer
    over one continuous span is the same row to the calendar, and two resumes often
    word the same title differently ("SWE" and "Software Engineer").
    """
    return _folded(employer), _when(span)


def _folded(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _when(span: MonthSpan | None) -> str:
    return f"{span.start.isoformat()}:{span.end.isoformat() if span.end else 'open'}" if span else ""


def project_key(name: str, span: MonthSpan | None) -> tuple[str, str]:
    """A project's identity, by the role rule: what it is called, and when.

    The same name over different months is two projects, as two stints at one employer
    are two roles. A project with no months matches only another with none.
    """
    return _folded(name), _when(span)


def education_key(school: str, degree: str, span: MonthSpan | None) -> tuple[str, str, str]:
    """A degree's identity: where, what, and when. Two degrees from one school are two."""
    return _folded(school), _folded(degree), _when(span)


def achievement_key(title: str, issuer: str | None) -> tuple[str, str]:
    """An award or certification's identity: what it is, and who gave it."""
    return _folded(title), _folded(issuer or "")


def match_experience(
    proposed: ProposedExperience, existing: list[tuple[uuid.UUID, str, MonthSpan | None]]
) -> uuid.UUID | None:
    """The existing role this proposal describes, or ``None``.

    Matched on employer and months. An employer match with a *different* span is not
    a match: two stints at the same company are two roles, and merging them would
    invent continuous employment across the gap.
    """
    if not proposed.employer:
        return None
    key = experience_key(proposed.employer, proposed.span)
    for experience_id, employer, span in existing:
        if experience_key(employer, span) == key:
            return experience_id
    return None
