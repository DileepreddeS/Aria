"""6c Recruiter check (RESUME_ENGINE §9), the deterministic part: it reads like a person wrote it.

The rules come from style.yaml (§1 rules 4-5). Over the page, in reading order:

- no banned word, in a bullet or the summary, ever: a fact that uses one is reworded;
- every bullet opens with an allowed verb, and none with a weak opening;
- no verb opens two bullets in a row, nor more than ``max_bullets_per_verb`` on the page
  (a fourth "Built" fails, DECISIONS 2026-10-16), counted across entries because a
  recruiter reads one page;
- at least ``min_quantified_share`` of bullets carry a number, digits or words;
- no vague phrase, unless a fact the bullet cites says it in those words.

The 6-second test, which asks a model what the top third of the page shows, is a later
step.
"""

from __future__ import annotations

from collections import Counter

from aria_core.schemas.snapshot import CandidateSnapshot
from aria_resume.document import ResumeDocument, SectionKind
from aria_resume.validate import Finding
from aria_resume.validate.style import Style
from aria_resume.validate.text import has_phrase, number_mentions, number_words, phrase_spans

__all__ = ["check_recruiter"]


def _opening(text: str, phrases: tuple[str, ...]) -> str | None:
    """The longest phrase ``text`` opens with, as whole words."""
    for phrase in sorted(phrases, key=len, reverse=True):
        spans = phrase_spans(text, phrase)
        if spans and spans[0][0] == len(text) - len(text.lstrip()):
            return phrase
    return None


def check_recruiter(document: ResumeDocument, snapshot: CandidateSnapshot, style: Style) -> list[Finding]:
    findings: list[Finding] = []
    facts = {fact.fact_id: fact.text for fact in snapshot.facts}

    lines = [
        line for section in document.sections if section.kind is SectionKind.SUMMARY for line in section.lines
    ]
    bullets = [
        bullet for section in document.sections for entry in section.entries for bullet in entry.bullets
    ]
    for text in lines + [bullet.text for bullet in bullets]:
        for word in style.banned:
            if has_phrase(text, word):
                findings.append(Finding("banned_word", f"{text!r} uses the banned word {word!r}"))

    counts: Counter[str] = Counter()
    previous: str | None = None
    quantified = 0
    for bullet in bullets:
        text = bullet.text
        verb = None
        if (weak := _opening(text, style.weak_openings)) is not None:
            findings.append(Finding("weak_opening", f"{text!r} opens with {weak!r}"))
        elif (verb := _opening(text, style.allowed_verbs)) is None:
            first = text.split()[0] if text.split() else ""
            findings.append(
                Finding(
                    "verb_not_allowed", f"{text!r} opens with {first!r}, which is not in the voice's verbs"
                )
            )
        if verb is not None:
            if verb == previous:
                findings.append(
                    Finding("repeated_verb", f"{text!r} opens with {verb!r}, as the bullet before it does")
                )
            counts[verb] += 1
        previous = verb

        if number_mentions(text) or number_words(text):
            quantified += 1

        cited = [facts[fact_id] for fact_id in bullet.fact_ids if fact_id in facts]
        for phrase in style.vague_phrases:
            if has_phrase(text, phrase) and not any(has_phrase(fact, phrase) for fact in cited):
                findings.append(
                    Finding("vague_phrase", f"{text!r} says {phrase!r}, which no cited fact says")
                )

    for verb, count in counts.items():
        if count > style.max_bullets_per_verb:
            findings.append(
                Finding(
                    "verb_overused", f"{verb!r} opens {count} bullets; at most {style.max_bullets_per_verb}"
                )
            )
    if bullets and quantified < style.min_quantified_share * len(bullets):
        findings.append(
            Finding(
                "too_few_quantified",
                f"{quantified} of {len(bullets)} bullets carry a number; at least "
                f"{style.min_quantified_share:.0%} should",
            )
        )
    return findings
