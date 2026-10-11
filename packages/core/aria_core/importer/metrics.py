"""Pulling numbers off a resume bullet without inventing achievements.

A metric on a resume is a claim an interviewer can ask about, so the bar here is not
"find the numbers" — it is **never report a number that is not a result**. The ways a
resume contains numbers that are not results are specific and enumerable:

==========================  =====================================================
``YOLO11m``, ``GPT-4o``     a model name; digits welded to letters
``Llama-3-8B``              likewise, and the ``8B`` is parameters, not a result
``Python 3.12``, ``v2.1``   a version
``WCAG 2.1``                a standard's number
``2024``, ``2021-2023``     a year
``p95``, ``H100``, ``S3``   an identifier with a numeric part
``3.8/4.0``                 a GPA, which belongs to education
``(555) 123-4567``          a phone number
==========================  =====================================================

The rule that covers almost all of it: **a number is a result only when something says
what it measures.** A unit (``%``, ``ms``, ``x``, ``GB``), a currency symbol, a ``+``,
or a noun immediately after it. A bare number with none of those measures nothing, and
a dotted number with no unit is a version. That is a decidable test rather than a list
of model names to keep up to date — and the list above is in the test suite, because a
version number presented as a result is the most likely route by which an untrue number
reaches a resume (``docs/DECISIONS.md``).

Baselines are captured when the text states one. "Reduced latency from 1.2s to 300ms"
is a stronger and more defensible claim than "300ms", and the baseline also stops
``1.2s`` being reported as an achievement of its own.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace

from aria_core.graph.status import Theme
from aria_core.importer.dates import parse_date_range
from aria_core.schemas.base import utc_now

__all__ = ["Metric", "detect_themes", "extract_metrics"]

#: Unit and magnitude suffixes that may be welded to a number. Order matters: the
#: longest match is stripped first, so "ms" is not read as "m" plus a stray "s".
_UNITS: tuple[str, ...] = (
    "%",
    "ms",
    "qps",
    "rps",
    "fps",
    "bps",
    "gb",
    "mb",
    "tb",
    "kb",
    "pb",
    "hrs",
    "hr",
    "sec",
    "secs",
    "min",
    "mins",
    "x",
    "k",
    "m",
    "b",
    "bn",
    "s",
    "h",
)

#: Words after a number that name what it measures, so "1.5 million" and "30 percent"
#: are results even though the number carries no symbol.
_UNIT_WORDS = frozenset(
    {
        "percent",
        "percentage",
        "thousand",
        "million",
        "billion",
        "basis",
        "bps",
        "seconds",
        "second",
        "milliseconds",
        "millisecond",
        "minutes",
        "minute",
        "hours",
        "hour",
        "days",
        "day",
        "weeks",
        "week",
        "months",
        "month",
        "years",
        "year",
        "times",
        "fold",
    }
)

#: Words that end the phrase a number measures. A connective introduces a new clause,
#: and running past one turns "20+ components used by 4 teams" into a single blurred
#: claim about neither.
_CONNECTIVES = frozenset(
    {
        "and",
        "across",
        "after",
        "as",
        "at",
        "before",
        "but",
        "by",
        "during",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "over",
        "per",
        "providing",
        "that",
        "through",
        "to",
        "under",
        "using",
        "via",
        "which",
        "while",
        "with",
        "within",
        "without",
        "used",
        "serving",
        "supporting",
        "reducing",
        "improving",
        "saving",
        "cutting",
        "enabling",
        "resulting",
        # Auxiliaries and copulas. A number followed by a verb has nothing to count:
        # "Option 3 was chosen" states no achievement, and reading "3 was" as a
        # measurement is how a stray number reaches a resume.
        "was",
        "were",
        "is",
        "are",
        "be",
        "been",
        "has",
        "had",
        "have",
        "will",
        "would",
        "should",
        "became",
        "chosen",
    }
)

#: Leading verbs trimmed from a phrase taken from *before* a number. "Cut p95 latency
#: by 30%" measures "p95 latency", not "Cut p95 latency".
_LEADING_VERBS = frozenset(
    {
        "achieved",
        "added",
        "automated",
        "boosted",
        "built",
        "created",
        "cut",
        "decreased",
        "delivered",
        "deployed",
        "designed",
        "developed",
        "drove",
        "grew",
        "handled",
        "implemented",
        "improved",
        "increased",
        "launched",
        "led",
        "lowered",
        "maintained",
        "migrated",
        "optimised",
        "optimized",
        "processed",
        "raised",
        "reduced",
        "refactored",
        "removed",
        "saved",
        "scaled",
        "secured",
        "booked",
        "won",
        "generated",
        "returned",
        "closed",
        "landed",
        "recovered",
        "avoided",
        "unlocked",
        "captured",
        "negotiated",
        "served",
        "shipped",
        "supported",
        "trained",
        "wrote",
    }
)

#: Phrases that introduce a baseline for the number before them.
_BASELINE_BEFORE = re.compile(
    r"\b(?:down\s+from|up\s+from|vs\.?|versus|compared\s+(?:to|with)|previously|was|from\s+a\s+baseline\s+of)\b",
    re.IGNORECASE,
)

#: "from A to B": B is the result, A is where it started.
_FROM_TO = re.compile(r"\bfrom\b", re.IGNORECASE)

#: What may sit between the two numbers of a "from A to B". The unit is usually
#: repeated — "from 3 nodes to 40 nodes" — so a couple of words either side of the
#: "to" are allowed. The "to" itself is required, which is what stops "from 3 services
#: and 2 databases" from being read as a before-and-after.
_FROM_TO_GAP = re.compile(r"\s*(?:\w+\s+){0,2}(?:to|\u2192|[-\u2013])\s*(?:\w+\s+){0,2}", re.IGNORECASE)

#: One numeric mention. Written with escapes rather than the characters themselves:
#: a currency symbol and a curly apostrophe are indistinguishable from their ASCII
#: neighbours in a character class, and a regex is the worst place to be guessing
#: which one is on the page.
_NUMBER = re.compile(
    r"(?:(?P<qualifier>[A-Za-z]{0,2})(?P<currency>[$€£¥])\s?)?"
    r"(?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    # No apostrophes: a curly or straight apostrophe after a number is never a unit,
    # so leaving it out of the class makes "the 90s" fall through to the bare-number
    # rules instead of being read as ninety seconds.
    r"(?P<tail>[A-Za-z%+/]*)"
)

#: Currency symbols, named once. A qualifier may precede one ("C$", "US$"), which is
#: the only case where letters in front of a number do not make it a name.
_CURRENCY_SYMBOLS = "$\u20ac\u00a3\u00a5"

_YEAR_RANGE = (1900, 2100)

#: Words that mark the nearby number as a grade rather than an achievement.
_GPA_WORDS = frozenset({"gpa", "cgpa", "cumulative", "grade"})


@dataclass(frozen=True, slots=True)
class Metric:
    """One number a resume presents as a result."""

    #: The number exactly as written, so a resume never restates it differently.
    exact_value: str
    #: What it measures, in the resume's own words. ``None`` when the text does not
    #: say — which is itself worth seeing in the approval view.
    measures: str | None = None
    #: Where it started, when the text states it.
    baseline: str | None = None
    #: Offset into the line, used to keep the order stable and to dedupe.
    position: int = -1


@dataclass(frozen=True, slots=True)
class _Mention:
    """A numeric mention and what we concluded about it."""

    text: str
    start: int
    end: int
    digits: str
    tail: str
    currency: str

    @property
    def dotted(self) -> bool:
        return "." in self.digits

    @property
    def digit_count(self) -> int:
        return len(self.digits.replace(",", "").replace(".", ""))

    @property
    def plus(self) -> bool:
        return self.tail.endswith("+")

    def unit(self) -> str | None:
        """The unit welded to the number, or ``None``.

        Returns ``None`` for a tail that is not a unit at all, and the caller treats
        leftover letters as proof the token is an identifier rather than a quantity.
        """
        tail = self.tail.rstrip("+").lower()
        if not tail:
            return None
        for suffix in sorted(_UNITS, key=len, reverse=True):
            if tail == suffix:
                return suffix
        return None

    #: The whitespace-delimited token the number sits in, e.g. "YOLO11m" for "11".
    token: str = ""

    @property
    def is_identifier(self) -> bool:
        """True when the number is part of a name: YOLO11m, GPT-4o, Llama-3-8B, p95.

        Tested on the whole token rather than on what follows the digits, because the
        letters that give a name away can be on either side: ``YOLO11m`` has them in
        front, ``GPT-4o`` behind, and ``Llama-3-8B`` wears a plausible unit ("8B")
        that is only a parameter count because of the word welded to it.

        Side matters, though. **A unit follows a number and never precedes one**, so
        any letter in front of the digits makes this a name — which is the whole
        difference between ``100h`` (a hundred hours) and ``H100`` (a GPU).
        """
        digits_at = self.token.find(self.digits)
        if digits_at == -1:
            return False
        before, after = self.token[:digits_at], self.token[digits_at + len(self.digits) :]

        # A currency qualifier is the one thing in front of a number that is not a
        # name: C$, US$, A$, R$ all say which dollar. Strip the symbol and anything
        # before it, so "C$500K" is five hundred thousand dollars and "H100" is
        # still a GPU.
        for symbol in _CURRENCY_SYMBOLS:
            if symbol in before:
                before = before.split(symbol)[-1]
                break

        if any(character.isalpha() for character in before):
            return True

        trailing = "".join(character for character in after if character.isalpha())
        return bool(trailing) and trailing.lower() not in _UNITS


def _enclosing_token(text: str, start: int, end: int) -> str:
    """The whitespace-delimited token a match sits inside, trimmed of punctuation."""
    left = text.rfind(" ", 0, start) + 1
    right = text.find(" ", end)
    token = text[left : right if right != -1 else len(text)]
    return token.strip('(),;:.[]\u2013\u2014\u201c\u201d"')


def _tokens_before(text: str, index: int) -> list[str]:
    return text[:index].split()


def _word_after(text: str, index: int, *, look_ahead: int = 2) -> str:
    """The first word after ``index`` that could name what the number measures.

    Looks past one intervening token, because "3 H100 nodes" counts three nodes and
    the model name sits between the number and the noun it belongs to.
    """
    rest = text[index:].lstrip(" \t:;-\u2013")
    for word in rest.split()[:look_ahead]:
        bare = word.strip(".,;:()[]").lower()
        if bare and bare.replace("/", "").replace("-", "").isalpha():
            return bare
    return ""


def _with_magnitude_word(mention: _Mention, line: str) -> _Mention:
    """Fold a following magnitude word into the value: "1.5 million", not "1.5"."""
    rest = line[mention.end :]
    stripped = rest.lstrip(" \t")
    if not stripped:
        return mention
    word = stripped.split()[0]
    if word.strip(".,;:").lower() not in _UNIT_WORDS:
        return mention

    offset = mention.end + (len(rest) - len(stripped)) + len(word)
    return replace(
        mention,
        text=line[mention.start : offset].strip(),
        end=offset,
    )


def _is_result(mention: _Mention, line: str, *, today: dt.date) -> bool:
    """Whether this numeric mention is a result the resume is claiming."""
    # A name with digits in it: YOLO11m, GPT-4o, Llama-3-8B, p95, H100, S3.
    if mention.is_identifier:
        return False

    # A fraction against a scale, next to the word GPA.
    if "/" in mention.tail:
        return False
    if any(word.strip(".,:").lower() in _GPA_WORDS for word in _tokens_before(line, mention.start)[-3:]):
        return False

    unit = mention.unit()
    following = _word_after(line, mention.end)

    if mention.currency or unit or mention.plus or following in _UNIT_WORDS:
        return True

    # A dotted number with nothing saying what it measures is a version: Python 3.12,
    # WCAG 2.1, v2.1. This is the rule that does not need a list of product names.
    if mention.dotted:
        return False

    value = int(mention.digits.replace(",", ""))
    if _YEAR_RANGE[0] <= value <= _YEAR_RANGE[1] and value <= today.year + 1:
        return False

    # A long bare run of digits is an identifier or a phone number unless something
    # names it: "employee 1234567" is not an achievement.
    if mention.digit_count >= 7 and not following:
        return False

    # A bare integer is a count only if a noun follows it. "4 teams" counts; a stray
    # "4" measures nothing and must not become a claim.
    noun = following.replace("/", "").replace(".", "").replace("-", "")
    return len(noun) >= 3 and noun.isalpha() and following not in _CONNECTIVES


def _phrase_after(line: str, index: int, *, limit: int = 6, skip_connective: bool = False) -> str:
    """The noun phrase a number quantifies, cut at the first clause boundary.

    ``skip_connective`` steps over one leading preposition, for "$500K in savings".
    It is off by default because the word after a connective is usually how the
    result was achieved rather than what was measured.
    """
    rest = line[index:].lstrip(" \t:-\u2013")
    clause = re.split(r"[,;:()\u2014]|\u2013\s", rest, maxsplit=1)[0]
    words: list[str] = []
    for position, word in enumerate(clause.split()):
        bare = word.strip(".,;:()[]").lower()
        if bare in _CONNECTIVES and words:
            break
        if bare in _CONNECTIVES and not words:
            if skip_connective and position == 0:
                continue
            return ""
        words.append(word.strip(".,;:"))
        if len(words) >= limit:
            break
    return " ".join(words).strip()


def _phrase_before(line: str, index: int, *, limit: int = 6) -> str:
    """The phrase a number refers back to, for "cut latency by 30%".

    Collected backwards and stopped at the nearest connective, not at a word count:
    the connective is where the clause about this number begins. "Saved $2M annually
    and grew signups 18%" leaves "signups", not five words of a different claim.
    """
    head = re.split(r"[,;:()\u2014]", line[:index].rstrip(" 	:-\u2013,;"))[-1]
    words = head.split()
    while words and words[-1].strip(".,;:").lower() in _CONNECTIVES:
        words.pop()

    collected: list[str] = []
    for word in reversed(words):
        if word.strip(".,;:").lower() in _CONNECTIVES or len(collected) >= limit:
            break
        collected.append(word.strip(".,;:"))
    collected.reverse()

    while collected and collected[0].lower() in _LEADING_VERBS:
        collected.pop(0)
    return " ".join(collected).strip(" .,;:")


def _measures(line: str, mention: _Mention, *, skip_to: int | None = None) -> str | None:
    """What the number measures: in front of it, then behind it, then in front again.

    The order is what matters. "20+ React components" is answered by looking forward.
    "Cut p95 latency by 30%" is answered by looking back, because what follows the
    number is the mechanism ("by adding a cache"), not the measure. And "Drove $500K
    in savings" is answered by looking forward *past the preposition* -- but only
    after the backward look has come up empty, so a mechanism can never win against
    a real measure.
    """
    forward = _phrase_after(line, mention.end)
    if forward:
        return forward

    start = skip_to if skip_to is not None and skip_to < mention.start else mention.start
    backward = _phrase_before(line, start)
    if backward:
        return backward

    return _phrase_after(line, mention.end, skip_connective=True) or None


def extract_metrics(line: str, *, today: dt.date | None = None) -> tuple[Metric, ...]:
    """Every number in ``line`` that the resume presents as a result.

    Numbers inside a date range are skipped: the dates of a role are not an
    achievement of it, and the entry parser has already read them.
    """
    now = today or utc_now().date()

    date_range = parse_date_range(line, today=now)
    date_window: tuple[int, int] | None = None
    if date_range is not None and date_range.position >= 0:
        date_window = (date_range.position, date_range.position + len(date_range.text))

    mentions: list[_Mention] = []
    for match in _NUMBER.finditer(line):
        mention = _Mention(
            text=match.group(0).strip(),
            start=match.start(),
            end=match.end(),
            digits=match.group("digits"),
            tail=match.group("tail") or "",
            currency=match.group("currency") or "",
            token=_enclosing_token(line, match.start("digits"), match.end()),
        )
        if date_window and mention.start >= date_window[0] and mention.end <= date_window[1]:
            continue
        if _is_result(mention, line, today=now):
            mentions.append(_with_magnitude_word(mention, line))

    if not mentions:
        return ()

    baselines: dict[int, str] = {}
    consumed: set[int] = set()
    skip_to: dict[int, int] = {}

    # "from A to B": B is the claim and A is where it started.
    for from_match in _FROM_TO.finditer(line):
        pair = [index for index, mention in enumerate(mentions) if mention.start >= from_match.end()]
        if len(pair) < 2:
            continue
        first, second = pair[0], pair[1]
        between = line[mentions[first].end : mentions[second].start]
        if _FROM_TO_GAP.fullmatch(between):
            baselines[second] = mentions[first].text
            consumed.add(first)
            skip_to[second] = from_match.start()

    # "30%, down from 55%": the first is the claim, the second is the baseline.
    for marker in _BASELINE_BEFORE.finditer(line):
        before = [index for index, mention in enumerate(mentions) if mention.end <= marker.start()]
        after = [index for index, mention in enumerate(mentions) if mention.start >= marker.end()]
        if not before or not after:
            continue
        claim, baseline = before[-1], after[0]
        if claim in baselines or baseline in consumed:
            continue
        baselines[claim] = mentions[baseline].text
        consumed.add(baseline)
        skip_to[claim] = marker.start()

    metrics: list[Metric] = []
    for index, mention in enumerate(mentions):
        if index in consumed:
            continue
        metrics.append(
            Metric(
                exact_value=mention.text,
                measures=_measures(line, mention, skip_to=skip_to.get(index)),
                baseline=baselines.get(index),
                position=mention.start,
            )
        )
    return tuple(metrics)


#: Keyword to theme. Deterministic and deliberately sparse: a theme steers which
#: evidence a resume shows, so a wrong one is a wasted bullet rather than an untruth,
#: but a theme nobody can see the reason for is worse than no theme.
#:
#: Each keyword is matched at a **word boundary**, and a keyword without a trailing
#: boundary matches as a prefix ("migrat" finds migrated and migration). Substring
#: matching was wrong in a way that is easy to miss: "port" found "report", and
#: "script" found "TypeScript", so a dashboard written in TypeScript came back tagged
#: as automation work.
_THEME_WORDS: tuple[tuple[Theme, tuple[str, ...]], ...] = (
    (Theme.PERFORMANCE, ("latency", "p95", "p99", "throughput", "faster", "speed", "optimis", "optimiz")),
    (Theme.SCALE, ("scale", "scaling", "concurrent", "million", "traffic", "load", "shard")),
    (
        Theme.RELIABILITY,
        ("uptime", "reliab", "availab", "outage", "incident", "failover", "retry", "retries"),
    ),
    (
        Theme.ACCESSIBILITY,
        ("accessib", "wcag", "screen reader", "a11y", "aria-label", "keyboard navigation"),
    ),
    (Theme.SECURITY, ("security", "vulnerab", "auth", "encrypt", "owasp", "csrf", "xss", "secrets")),
    (Theme.LEADERSHIP, ("led", "leading", "owned", "drove", "spearhead")),
    (Theme.MENTORING, ("mentor", "onboard", "taught", "coached", "trained the team")),
    (Theme.COLLABORATION, ("cross-functional", "collaborat", "partnered", "stakeholder", "paired")),
    (
        Theme.ML_RESEARCH,
        ("model", "models", "training", "inference", "dataset", "fine-tun", "accuracy", "f1"),
    ),
    (Theme.DATA, ("pipeline", "etl", "warehouse", "analytics", "query", "queries", "sql", "ingest")),
    (Theme.AUTOMATION, ("automat", "ci/cd", "scripted", "script to", "manual process", "by hand")),
    (Theme.COST_SAVING, ("cost", "costs", "spend", "budget", "savings", "cheaper")),
    (Theme.USER_IMPACT, ("users", "customers", "adoption", "engagement", "conversion", "retention")),
    (Theme.MIGRATION, ("migrat", "replatform", "rewrote", "upgraded", "ported")),
    (Theme.OWNERSHIP, ("end-to-end", "from scratch", "sole owner", "on-call", "owner of")),
)

#: Compiled once: each keyword anchored at a word boundary.
_THEME_PATTERNS: tuple[tuple[Theme, re.Pattern[str]], ...] = tuple(
    (theme, re.compile("|".join(r"\b" + re.escape(word) for word in words), re.IGNORECASE))
    for theme, words in _THEME_WORDS
)


def detect_themes(text: str) -> tuple[Theme, ...]:
    """Themes a bullet demonstrates, by keyword. Used to spread evidence, not to claim.

    A theme never appears on a resume by itself: it decides which approved fact gets
    shown, so a miss costs relevance and never truth.
    """
    return tuple(theme for theme, pattern in _THEME_PATTERNS if pattern.search(text))
