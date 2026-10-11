"""The header: draft contact fields, and the street address that never gets stored.

Two rules are load-bearing here. Every field is a **draft** — a stale phone number in
a resume header is common, and a stale phone number on a submitted application is a
lost interview. And a street address is **dropped and reported**: there is no
``ProfileFieldKind`` for one, so an importer that found one has to discard it, and
discarding part of a candidate's line silently is not acceptable either.
"""

from __future__ import annotations

from aria_core.graph.status import ProfileFieldKind
from aria_core.importer.header import classify_url, parse_header
from aria_core.importer.lines import read_lines


def header_of(text: str, links: tuple[str, ...] = ()) -> dict[str, str]:
    findings = parse_header(read_lines(text).lines, links)
    return {field.kind.value: field.value for field in findings.fields}


class TestContactDetails:
    def test_a_plain_header(self) -> None:
        fields = header_of(
            "Dileep Kumar Salla\n"
            "Dallas, TX | (469) 555-0134 | dileep@example.test\n"
            "linkedin.com/in/dileep | github.com/dileep\n"
        )
        assert fields["full_name"] == "Dileep Kumar Salla"
        assert fields["email"] == "dileep@example.test"
        assert fields["phone"] == "(469) 555-0134"
        assert fields["location_city"] == "Dallas"
        assert fields["location_region"] == "TX"
        assert fields["linkedin_url"] == "https://linkedin.com/in/dileep"
        assert fields["github_url"] == "https://github.com/dileep"

    def test_an_international_phone_number(self) -> None:
        assert header_of("Jane Doe\n+44 20 7946 0958\n")["phone"] == "+44 20 7946 0958"

    def test_an_all_caps_name(self) -> None:
        assert header_of("JANE DOE\nAustin, TX\n")["full_name"] == "JANE DOE"

    def test_a_job_title_is_not_read_as_a_name(self) -> None:
        fields = header_of("Software Engineer\nJane Doe\njane@example.test\n")
        assert fields["full_name"] == "Jane Doe"

    def test_the_source_line_travels_with_each_field(self) -> None:
        findings = parse_header(read_lines("Jane Doe\n\njane@example.test\n").lines)
        by_kind = {field.kind: field.source_line for field in findings.fields}
        assert by_kind[ProfileFieldKind.FULL_NAME] == 1
        assert by_kind[ProfileFieldKind.EMAIL] == 3

    def test_only_the_first_of_each_kind_is_taken(self) -> None:
        # Two emails in a header is a document to look at, not a coin to flip.
        fields = header_of("Jane Doe\nold@example.test\nnew@example.test\n")
        assert fields["email"] == "old@example.test"


class TestStreetAddressesAreDropped:
    HEADER = "Dileep Kumar Salla\n1234 N Maple Street, Apt 4B, Dallas, TX 75201\ndileep@example.test\n"

    def test_the_city_and_region_survive(self) -> None:
        fields = header_of(self.HEADER)
        assert fields["location_city"] == "Dallas"
        assert fields["location_region"] == "TX"

    def test_no_field_holds_the_street(self) -> None:
        for value in header_of(self.HEADER).values():
            assert "Maple" not in value
            assert "1234" not in value

    def test_the_drop_is_reported_with_a_reason(self) -> None:
        findings = parse_header(read_lines(self.HEADER).lines)
        what = {detail.what for detail in findings.dropped}
        assert "street address" in what
        assert "apartment or unit" in what
        for detail in findings.dropped:
            assert detail.source_line == 2
            assert "street address" in detail.reason

    def test_the_schema_has_nowhere_to_put_one(self) -> None:
        # Belt and braces with test_graph_schema.py: if a street-address kind is ever
        # added, this test is the second thing that fails.
        kinds = {kind.value for kind in ProfileFieldKind}
        assert not any("street" in kind or "address_line" in kind for kind in kinds)


class TestLinks:
    def test_an_annotation_url_becomes_a_field(self) -> None:
        # A real resume keeps its LinkedIn URL in a PDF annotation, not in the text.
        fields = header_of("Jane Doe\n", links=("https://www.linkedin.com/in/janedoe",))
        assert fields["linkedin_url"] == "https://www.linkedin.com/in/janedoe"

    def test_a_mailto_annotation_is_read_as_an_email(self) -> None:
        fields = header_of("Jane Doe\n", links=("mailto:jane@example.test",))
        assert fields["email"] == "jane@example.test"
        assert "portfolio_url" not in fields

    def test_an_unknown_host_is_a_portfolio(self) -> None:
        assert classify_url("jane.dev") == (ProfileFieldKind.PORTFOLIO_URL, "https://jane.dev")

    def test_a_social_profile_is_an_other_url(self) -> None:
        classified = classify_url("https://x.com/janedoe")
        assert classified is not None and classified[0] is ProfileFieldKind.OTHER_URL

    def test_a_bare_word_is_not_a_url(self) -> None:
        # "https://" + any word parses happily, which once turned every word of a
        # project description into the candidate's portfolio.
        for word in ("deterministic", "resume", "aria", "a"):
            assert classify_url(word) is None

    def test_a_scheme_aria_will_not_store(self) -> None:
        for raw in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x"):
            assert classify_url(raw) is None
