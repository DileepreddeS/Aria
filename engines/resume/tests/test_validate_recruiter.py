"""6c Recruiter check, the deterministic part (RESUME_ENGINE §9): it reads like a person wrote it."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from aria_resume.document import (
    DocumentBullet,
    DocumentEntry,
    DocumentSection,
    ResumeDocument,
    SectionKind,
)
from aria_resume.validate import Finding
from aria_resume.validate.recruiter import check_recruiter
from aria_resume.validate.style import Style, load_style

from aria_core.schemas.snapshot import CandidateSnapshot

FACT = uuid.UUID(int=102)


def _page(*bullets: str, summary: tuple[str, ...] = (), fact: uuid.UUID = FACT) -> ResumeDocument:
    sections = []
    if summary:
        sections.append(DocumentSection(kind=SectionKind.SUMMARY, lines=summary))
    entry = DocumentEntry(
        title="Nordvale Consulting",
        detail="Software Engineer",
        bullets=tuple(DocumentBullet(text=text, fact_ids=(fact,)) for text in bullets),
    )
    sections.append(DocumentSection(kind=SectionKind.EXPERIENCE, entries=(entry,)))
    return ResumeDocument(name="Jordan A. Rivera", sections=tuple(sections))


Check = Callable[[ResumeDocument], list[Finding]]


@pytest.fixture
def style() -> Style:
    return load_style()


@pytest.fixture
def check(snapshot: CandidateSnapshot, style: Style) -> Check:
    def run(document: ResumeDocument) -> list[Finding]:
        return check_recruiter(document, snapshot, style)

    return run


def _checks(findings: list[Finding]) -> list[str]:
    return [finding.check for finding in findings]


GOOD = (
    "Built 15+ React screens for the checkout platform",
    "Cut p95 API latency by 30% with Redis caching",
    "Shipped 8 full-stack applications with Node.js",
    "Designed the checkout schema for the platform team",
)


def test_a_page_a_person_would_write_passes(check: Check) -> None:
    assert check(_page(*GOOD, summary=("Software engineer with three years of backend work.",))) == []


class TestWords:
    @pytest.mark.parametrize("word", ["leveraged", "Robust", "seamlessly", "best-in-class"])
    def test_a_banned_word_in_a_bullet(self, check: Check, word: str) -> None:
        findings = check(_page(f"Built 15+ {word} React screens", *GOOD[1:]))
        assert _checks(findings) == ["banned_word"]
        assert word.lower() in findings[0].detail.lower()

    def test_a_banned_word_in_the_summary(self, check: Check) -> None:
        findings = check(_page(*GOOD, summary=("A passionate engineer.",)))
        assert _checks(findings) == ["banned_word"]

    def test_a_banned_word_only_as_a_whole_word(self, check: Check) -> None:
        # "dynamic" is banned; "dynamically" and "aerodynamic" are other words.
        assert check(_page("Built 15+ dynamically loaded React screens", *GOOD[1:])) == []

    def test_a_banned_word_even_when_a_fact_says_it(self, snapshot: CandidateSnapshot, style: Style) -> None:
        # Unlike a vague phrase, a banned word is never excused: the fact can be reworded.
        fact = snapshot.facts[1].model_copy(update={"text": "Built 15+ robust React screens"})
        robust = snapshot.model_copy(update={"facts": (snapshot.facts[0], fact, *snapshot.facts[2:])})
        page = _page("Built 15+ robust React screens", *GOOD[1:], fact=fact.fact_id)
        assert "banned_word" in _checks(check_recruiter(page, robust, style))


class TestOpenings:
    @pytest.mark.parametrize("opening", ["Responsible for", "Worked on", "Helped build"])
    def test_a_weak_opening(self, check: Check, opening: str) -> None:
        findings = check(_page(f"{opening} 15+ React screens", *GOOD[1:]))
        assert "weak_opening" in _checks(findings)

    def test_a_verb_outside_the_voice(self, check: Check) -> None:
        findings = check(_page("Developed 15+ React screens", *GOOD[1:]))
        assert _checks(findings) == ["verb_not_allowed"]
        assert "Developed" in findings[0].detail

    def test_a_two_word_verb(self, check: Check) -> None:
        assert check(_page("Set up 15+ React screens for checkout", *GOOD[1:])) == []

    def test_a_verb_in_the_list_only_as_a_whole_word(self, check: Check) -> None:
        # "Cutover" is not "cut".
        assert _checks(check(_page("Cutover 15+ React screens", *GOOD[1:]))) == ["verb_not_allowed"]


class TestRepetition:
    def test_the_same_verb_twice_in_a_row(self, check: Check) -> None:
        page = _page(GOOD[0], "Built 8 full-stack applications with Node.js", GOOD[1], GOOD[3])
        findings = check(page)
        assert _checks(findings) == ["repeated_verb"]

    def test_the_same_verb_three_times_apart_passes(self, check: Check) -> None:
        page = _page(
            "Built 15+ React screens",
            GOOD[1],
            "Built 8 Node.js services",
            GOOD[3],
            "Built 2 dashboards",
        )
        assert check(page) == []

    def test_a_fourth_bullet_with_the_same_verb_fails(self, check: Check) -> None:
        page = _page(
            "Built 15+ React screens",
            GOOD[1],
            "Built 8 Node.js services",
            GOOD[3],
            "Built 2 dashboards",
            GOOD[2],
            "Built 3 queues",
        )
        findings = check(page)
        assert _checks(findings) == ["verb_overused"]
        assert "built" in findings[0].detail and "4" in findings[0].detail

    def test_the_cap_counts_the_whole_page(self, check: Check) -> None:
        # Across entries, not per entry: a recruiter reads one page.
        entries = tuple(
            DocumentEntry(
                title=f"Employer {index}",
                bullets=(
                    DocumentBullet(text=f"Built {index} React screens", fact_ids=(FACT,)),
                    DocumentBullet(text=f"Cut {index}0% of latency", fact_ids=(FACT,)),
                ),
            )
            for index in range(1, 5)
        )
        page = ResumeDocument(
            name="Jordan A. Rivera", sections=(DocumentSection(kind=SectionKind.EXPERIENCE, entries=entries),)
        )
        assert "verb_overused" in _checks(check(page))


class TestQuantified:
    def test_fewer_than_60_percent_quantified(self, check: Check) -> None:
        page = _page(
            GOOD[0], "Designed the checkout schema", "Migrated the billing service", "Shipped the admin tool"
        )
        findings = check(page)
        assert _checks(findings) == ["too_few_quantified"]
        assert "1 of 4" in findings[0].detail

    def test_exactly_60_percent_passes(self, check: Check) -> None:
        page = _page(
            GOOD[0], GOOD[1], GOOD[2], "Designed the checkout schema", "Migrated the billing service"
        )
        assert check(page) == []

    def test_a_number_in_words_counts(self, check: Check) -> None:
        page = _page(GOOD[0], "Designed a schema for three teams", "Migrated the billing service")
        assert check(page) == []

    def test_a_name_with_a_digit_does_not(self, check: Check) -> None:
        page = _page(GOOD[0], "Designed the p95 dashboard", "Migrated the billing service to S3")
        assert _checks(check(page)) == ["too_few_quantified"]


class TestVaguePhrases:
    def test_a_vague_phrase(self, check: Check) -> None:
        findings = check(_page("Built 15+ digital solutions for checkout", *GOOD[1:]))
        assert _checks(findings) == ["vague_phrase"]

    def test_a_vague_phrase_the_cited_fact_says_itself(self, check: Check, ids: SimpleNamespace) -> None:
        page = _page("Designed cloud-native solutions on AWS", *GOOD[:3], fact=ids.cloud)
        assert check(page) == []

    def test_a_vague_phrase_another_fact_says_is_not_excused(
        self, check: Check, ids: SimpleNamespace
    ) -> None:
        page = _page("Designed cloud-native solutions on AWS", *GOOD[:3], fact=ids.screens)
        assert "vague_phrase" in _checks(check(page))
