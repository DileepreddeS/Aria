"""The metric extractor must never report a number that is not a result.

The founder's review note drives this file: the extractor "must not treat years or version
numbers (YOLO11m, Llama-3-8B, WCAG 2.1, GPT-4o) as metrics and must capture baselines".
Each of those four is a named test below, because a version number presented as an
achievement is the shortest path from a real resume to an untrue one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aria_core.graph.status import Theme
from aria_core.importer.metrics import detect_themes, extract_metrics

TODAY = dt.date(2026, 10, 8)


def values(line: str) -> list[str]:
    return [metric.exact_value for metric in extract_metrics(line, today=TODAY)]


class TestNumbersThatAreNotResults:
    @pytest.mark.parametrize(
        "line",
        [
            "Fine-tuned YOLO11m for defect detection",
            "Served Llama-3-8B behind a queue",
            "Brought checkout to WCAG 2.1 AA",
            "Compared GPT-4o with Claude for answer quality",
            "Upgraded the service to Python 3.12",
            "Shipped v2.1 of the importer",
            "Ran inference on an H100",
            "Stored artifacts in S3",
            "Watched the p95 on every endpoint",
            "ES2021 syntax throughout",
        ],
        ids=[
            "model-name-with-digits",
            "parameter-count-welded-to-a-name",
            "standard-number",
            "model-generation",
            "language-version",
            "release-version",
            "gpu-model",
            "service-name",
            "percentile-label",
            "spec-year-in-a-name",
        ],
    )
    def test_a_name_or_a_version_is_not_a_metric(self, line: str) -> None:
        assert values(line) == [], f"{line!r} has no result in it"

    @pytest.mark.parametrize("line", ["Graduated in 2024", "Worked there 2021-2023", "Since 2019"])
    def test_a_year_is_not_a_metric(self, line: str) -> None:
        assert values(line) == []

    def test_a_gpa_is_not_a_metric(self) -> None:
        # A GPA belongs to the education row. Reporting it as an achievement would put
        # "3.8" on a resume as though it measured work.
        assert values("BS Computer Science, GPA 3.8/4.0, 2024") == []

    def test_a_phone_number_is_not_a_metric(self) -> None:
        assert values("Reach me on (555) 123-4567") == []

    def test_a_bare_number_with_nothing_to_measure_is_not_a_metric(self) -> None:
        # Nothing says what the 3 counts, so there is no claim to make.
        assert values("Option 3 was chosen") == []

    def test_a_dotted_number_without_a_unit_is_treated_as_a_version(self) -> None:
        assert values("Adopted the 2.1 spec") == []
        # ...but the same shape with a unit is a real measurement.
        assert values("Held p99 at 2.1s") == ["2.1s"]


class TestNumbersThatAreResults:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("Built 20+ React components used by 4 teams", ["20+", "4"]),
            ("Cut p95 API latency by 30%", ["30%"]),
            ("Saved $2M a year", ["$2M"]),
            ("Handled 5,000 req/s", ["5,000"]),
            ("Trained on 1.5 million images", ["1.5 million"]),
            ("Reviewed 150+ pull requests", ["150+"]),
            ("Cut the job to 300ms", ["300ms"]),
            ("Reached 94.2% accuracy", ["94.2%"]),
            ("Mentored 2 interns", ["2"]),
            ("Made it 3x faster", ["3x"]),
        ],
    )
    def test_a_unit_or_a_counted_noun_makes_a_result(self, line: str, expected: list[str]) -> None:
        assert values(line) == expected

    def test_a_magnitude_word_belongs_to_the_value(self) -> None:
        # The claim is "1.5 million images", not "1.5" of something called
        # "million images".
        (metric,) = extract_metrics("Trained on 1.5 million images", today=TODAY)
        assert metric.exact_value == "1.5 million"
        assert metric.measures == "images"

    def test_the_value_is_kept_exactly_as_written(self) -> None:
        # Never restated: "5,000" does not become "5000" on a resume, and "20+" does
        # not become "20".
        assert values("Handled 5,000 requests and 20+ jobs") == ["5,000", "20+"]

    def test_a_number_inside_the_date_range_is_not_an_achievement(self) -> None:
        assert values("Software Engineer, Jun 2024 - Present") == []

    def test_an_identifier_between_the_number_and_its_noun_does_not_hide_it(self) -> None:
        assert values("Ran it on 3 H100 nodes") == ["3"]


class TestWhatTheNumberMeasures:
    def test_it_looks_forward_first(self) -> None:
        (first, _) = extract_metrics("Built 20+ React components used by 4 teams", today=TODAY)
        assert first.measures == "React components"

    def test_it_looks_back_when_nothing_follows(self) -> None:
        (metric,) = extract_metrics("Cut p95 API latency by 30%", today=TODAY)
        assert metric.measures == "p95 API latency"

    def test_it_stops_at_the_nearest_connective(self) -> None:
        # Not "and grew signups": the clause about this number starts after "and".
        metrics = extract_metrics("Saved $2M and grew signups 18%", today=TODAY)
        assert metrics[-1].measures == "signups"

    def test_measures_may_be_absent(self) -> None:
        (metric,) = extract_metrics("Cut it by 30%", today=TODAY)
        assert metric.measures in (None, "it")


class TestBaselines:
    def test_from_a_to_b_makes_b_the_claim_and_a_the_baseline(self) -> None:
        (metric,) = extract_metrics("Reduced p95 latency from 1.2s to 300ms", today=TODAY)
        assert (metric.exact_value, metric.baseline) == ("300ms", "1.2s")

    def test_the_baseline_is_not_also_reported_as_an_achievement(self) -> None:
        # Without this, "1.2s" would appear on a resume as something accomplished.
        assert values("Reduced p95 latency from 1.2s to 300ms") == ["300ms"]

    def test_a_repeated_unit_between_the_two_numbers_is_tolerated(self) -> None:
        (metric,) = extract_metrics("Scaled the cluster from 3 nodes to 40 nodes", today=TODAY)
        assert (metric.exact_value, metric.baseline) == ("40", "3")

    def test_down_from_puts_the_baseline_after_the_claim(self) -> None:
        metrics = extract_metrics("Cut churn to 18%, down from 55%", today=TODAY)
        assert [(metric.exact_value, metric.baseline) for metric in metrics] == [("18%", "55%")]

    @pytest.mark.parametrize("phrase", ["vs", "versus", "compared to", "previously"])
    def test_other_baseline_phrasings(self, phrase: str) -> None:
        (metric,) = extract_metrics(f"Held latency at 300ms {phrase} 900ms", today=TODAY)
        assert metric.baseline == "900ms"

    def test_a_list_is_not_a_before_and_after(self) -> None:
        # "from 3 services and 2 databases to one API" states two counts, not a
        # baseline: pairing them would invent a transition the text never claimed.
        metrics = extract_metrics("Migrated from 3 services and 2 databases to one API", today=TODAY)
        assert [metric.baseline for metric in metrics] == [None, None]


class TestThemes:
    def test_a_keyword_matches_only_at_a_word_boundary(self) -> None:
        # "port" in "report" and "script" in "TypeScript" both used to match, which
        # tagged a dashboard as migration and automation work.
        assert Theme.MIGRATION not in detect_themes("Automated a manual claims report")
        assert Theme.AUTOMATION not in detect_themes("Shipped a dashboard in React and TypeScript")

    def test_a_prefix_keyword_still_matches_its_family(self) -> None:
        assert Theme.MIGRATION in detect_themes("Migrated the billing service")
        assert Theme.ACCESSIBILITY in detect_themes("Made the form accessible")

    def test_themes_are_stable_for_the_same_text(self) -> None:
        line = "Cut p95 latency by 30% and kept uptime at 99.9%"
        assert detect_themes(line) == detect_themes(line)
        assert Theme.PERFORMANCE in detect_themes(line)
        assert Theme.RELIABILITY in detect_themes(line)


class TestCurrency:
    """Money is a result, and a currency qualifier is not a name.

    The unit-follows-number rule is what rejects H100 and YOLO11m: letters in front
    of the digits mean the token is an identifier. A currency qualifier is the one
    exception — "C$", "US$", "A$" all say which dollar — so the rule looks past a
    currency symbol before deciding.
    """

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("Saved $120K a year", "$120K"),
            ("Drove $500K in savings", "$500K"),
            ("Cut spend by $2M", "$2M"),
            ("Raised \u00a3120K from investors", "\u00a3120K"),
            ("Secured \u20ac2.5M in funding", "\u20ac2.5M"),
            ("Returned $120k to the business", "$120k"),
            ("Booked C$500K of revenue", "C$500K"),
            ("Won US$1.2M in contracts", "US$1.2M"),
        ],
    )
    def test_money_is_a_metric(self, line: str, expected: str) -> None:
        assert values(line) == [expected]

    def test_the_qualifier_is_kept_in_the_value(self) -> None:
        # "C$500K" is not "$500K". A resume must not restate a figure in another
        # currency, which is what dropping the qualifier would do.
        (metric,) = extract_metrics("Booked C$500K of revenue", today=TODAY)
        assert metric.exact_value == "C$500K"

    def test_what_the_money_measures_is_found_past_the_preposition(self) -> None:
        # Nothing follows the number but a preposition, and nothing useful sits
        # behind it either, so the phrase after "in" is the measure.
        (metric,) = extract_metrics("Drove $500K in savings", today=TODAY)
        assert metric.measures == "savings"

    def test_a_mechanism_never_wins_against_a_real_measure(self) -> None:
        # The order of the three lookups matters: what follows "30%" here is how it
        # was done, not what was measured. The phrase behind the number must win.
        (metric,) = extract_metrics("Cut p95 API latency by 30% by adding Redis caching", today=TODAY)
        assert metric.measures == "p95 API latency"

    @pytest.mark.parametrize("line", ["Ran inference on an H100", "Stored it in S3", "Watched the p95"])
    def test_the_identifier_rule_still_holds(self, line: str) -> None:
        # The currency exception is narrow: it must not reopen the hole it sits next to.
        assert values(line) == []
