"""Importing a second resume must not double the graph — or merge two claims into one.

The founder imports two base resumes, so the same achievement arrives twice in different
words. The rule that matters most here is the one that stops a merge: **two facts
whose numbers differ are never duplicates.** "Cut latency by 30%" and "cut latency by
60%" are nearly the same sentence and are not the same claim, and merging them would
quietly pick one number for a resume to print.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from aria_core.graph.months import MonthSpan
from aria_core.importer.dedupe import (
    MatchKind,
    achievement_key,
    education_key,
    experience_key,
    find_duplicate_fact,
    match_experience,
    project_key,
    similarity,
)
from aria_core.importer.plan import ProposedExperience, ProposedFact

TODAY = dt.date(2026, 10, 8)


def fact(text: str) -> ProposedFact:
    return ProposedFact(text=text, source_line=1)


def existing(*texts: str) -> list[tuple[uuid.UUID, str]]:
    return [(uuid.uuid4(), text) for text in texts]


class TestIdenticalFacts:
    def test_the_same_sentence_matches(self) -> None:
        rows = existing("Built 20+ React components used by 4 teams")
        match = find_duplicate_fact(fact("Built 20+ React components used by 4 teams"), rows)
        assert match is not None and match.kind is MatchKind.IDENTICAL
        assert match.merge_without_asking

    def test_punctuation_and_case_do_not_matter(self) -> None:
        rows = existing("Built 20+ React components.")
        match = find_duplicate_fact(fact("built 20+ react components"), rows)
        assert match is not None and match.kind is MatchKind.IDENTICAL


class TestNearDuplicates:
    def test_the_same_achievement_in_other_words(self) -> None:
        rows = existing("Built 20+ React components used by 4 teams")
        match = find_duplicate_fact(fact("Developed 20+ React components for 4 teams"), rows)
        assert match is not None and match.kind is MatchKind.NEAR
        assert not match.merge_without_asking

    def test_a_longer_telling_of_the_same_claim(self) -> None:
        rows = existing("Cut p95 API latency by 30%")
        match = find_duplicate_fact(
            fact("Cut p95 API latency by 30% by introducing a Redis cache layer"), rows
        )
        assert match is not None and match.kind is MatchKind.NEAR

    def test_two_unrelated_bullets_do_not_match(self) -> None:
        rows = existing("Built 20+ React components used by 4 teams")
        assert find_duplicate_fact(fact("Wrote the database migration tooling"), rows) is None

    def test_the_best_match_wins(self) -> None:
        rows = existing(
            "Wrote the database migration tooling",
            "Built 20 React components for several teams",
        )
        match = find_duplicate_fact(fact("Built 20 React components used by 4 teams"), rows)
        assert match is not None
        assert "React" in match.existing_text


class TestDifferentNumbersAreDifferentClaims:
    def test_a_differing_metric_blocks_the_match(self) -> None:
        rows = existing("Cut p95 API latency by 30%")
        assert find_duplicate_fact(fact("Cut p95 API latency by 60%"), rows) is None

    def test_even_when_the_wording_is_otherwise_identical(self) -> None:
        rows = existing("Built 20+ React components used by 4 teams")
        assert find_duplicate_fact(fact("Built 40+ React components used by 4 teams"), rows) is None

    def test_the_same_numbers_still_match(self) -> None:
        rows = existing("Cut p95 API latency by 30%")
        match = find_duplicate_fact(fact("Reduced p95 API latency by 30%"), rows)
        assert match is not None

    def test_a_number_on_only_one_side_does_not_block_a_match(self) -> None:
        # One resume quantifies the achievement and the other does not; they are
        # still plausibly the same thing, so it is proposed rather than ruled out.
        rows = existing("Built React components used by several teams")
        match = find_duplicate_fact(fact("Built 20+ React components used by 4 teams"), rows)
        assert match is not None and match.kind is MatchKind.NEAR


class TestSimilarity:
    def test_a_sentence_is_identical_to_itself(self) -> None:
        assert similarity("Built the thing", "Built the thing") == pytest.approx(1.0)

    def test_shared_filler_does_not_make_two_bullets_alike(self) -> None:
        assert (
            similarity(
                "Was on the team that owned the billing service",
                "Is in a group which maintains the search index",
            )
            < 0.5
        )

    def test_a_bullet_too_short_to_judge_is_never_skipped_silently(self) -> None:
        # "Led the team" and "Led a team" reduce to the same two content words.
        # Calling that identical would drop the second without anyone seeing it, and
        # two bullets this short do not carry enough to be sure.
        match = find_duplicate_fact(fact("Led a team"), existing("Led the team"))
        assert match is not None
        assert match.kind is MatchKind.NEAR
        assert not match.merge_without_asking

    def test_a_bullet_with_enough_in_it_is_skipped_without_asking(self) -> None:
        match = find_duplicate_fact(
            fact("Led the billing platform team through the migration"),
            existing("Led the billing platform team through the migration"),
        )
        assert match is not None and match.merge_without_asking

    def test_word_endings_are_folded(self) -> None:
        assert similarity("Migrated the billing service", "Migrating the billing services") > 0.8

    def test_an_empty_string_matches_nothing(self) -> None:
        assert similarity("", "Built the thing") == 0.0


class TestMatchingRoles:
    def test_the_same_employer_over_the_same_months_is_one_role(self) -> None:
        span = MonthSpan(dt.date(2024, 6, 1))
        role_id = uuid.uuid4()
        proposed = ProposedExperience(employer="Accenture", title="SWE", source_line=1, span=span)
        assert match_experience(proposed, [(role_id, "Accenture", span)]) == role_id

    def test_the_title_is_not_part_of_the_identity(self) -> None:
        # A promotion within one continuous span is one row to the calendar, and two
        # resumes often word the same title differently.
        span = MonthSpan(dt.date(2024, 6, 1), dt.date(2025, 6, 1))
        role_id = uuid.uuid4()
        proposed = ProposedExperience(
            employer="Accenture", title="Senior Software Engineer", source_line=1, span=span
        )
        assert match_experience(proposed, [(role_id, "Accenture", span)]) == role_id

    def test_a_different_span_at_the_same_employer_is_a_different_role(self) -> None:
        # Two stints at one company are two roles. Merging them would invent
        # continuous employment across the gap between them.
        first = MonthSpan(dt.date(2020, 1, 1), dt.date(2020, 8, 1))
        second = MonthSpan(dt.date(2024, 6, 1))
        proposed = ProposedExperience(employer="Accenture", title="SWE", source_line=1, span=second)
        assert match_experience(proposed, [(uuid.uuid4(), "Accenture", first)]) is None

    def test_punctuation_in_the_employer_name_does_not_matter(self) -> None:
        span = MonthSpan(dt.date(2024, 6, 1))
        role_id = uuid.uuid4()
        proposed = ProposedExperience(employer="Acme, Inc.", title="SWE", source_line=1, span=span)
        assert match_experience(proposed, [(role_id, "Acme Inc", span)]) == role_id

    def test_a_role_with_no_employer_matches_nothing(self) -> None:
        span = MonthSpan(dt.date(2024, 6, 1))
        proposed = ProposedExperience(employer="", title="SWE", source_line=1, span=span)
        assert match_experience(proposed, [(uuid.uuid4(), "Accenture", span)]) is None

    def test_an_undated_role_does_not_match_a_dated_one(self) -> None:
        proposed = ProposedExperience(employer="Accenture", title="SWE", source_line=1)
        dated = MonthSpan(dt.date(2024, 6, 1))
        assert match_experience(proposed, [(uuid.uuid4(), "Accenture", dated)]) is None

    def test_the_key_is_employer_and_months(self) -> None:
        span = MonthSpan(dt.date(2024, 6, 1))
        assert experience_key("Acme, Inc.", span) == experience_key("acme inc", span)
        assert experience_key("Acme", span) != experience_key("Acme", None)


class TestTheOtherEntitiesFollowTheRoleRule:
    def test_a_project_is_its_name_and_its_months(self) -> None:
        span = MonthSpan(dt.date(2025, 1, 1), dt.date(2025, 6, 1))
        assert project_key("VERA-RAG", span) == project_key("vera rag", span)
        assert project_key("VERA-RAG", None) == project_key("VERA-RAG", None)
        assert project_key("VERA-RAG", span) != project_key("VERA-RAG", None)
        assert project_key("VERA-RAG", span) != project_key("DentScan", span)

    def test_a_degree_is_its_school_its_degree_and_its_months(self) -> None:
        assert education_key("Cascadia State University", "M.S. Computer Science", None) == education_key(
            "CASCADIA STATE UNIVERSITY", "M.S. computer science", None
        )
        assert education_key("Cascadia State University", "B.S. Physics", None) != education_key(
            "Cascadia State University", "M.S. Computer Science", None
        )

    def test_an_achievement_is_its_title_and_its_issuer(self) -> None:
        assert achievement_key("Dean's List", "Cascadia") == achievement_key("DEAN'S LIST", "cascadia")
        assert achievement_key("Dean's List", None) == achievement_key("Dean's List", "")
        assert achievement_key("Dean's List", "Cascadia") != achievement_key("Dean's List", None)
