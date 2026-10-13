"""Which URLs in a resume header are the candidate's, and which are not."""

from __future__ import annotations

from aria_core.graph.status import ProfileFieldKind
from aria_core.importer.header import parse_header
from aria_core.importer.lines import read_lines


def test_an_email_s_domain_is_not_a_portfolio() -> None:
    # Regression: the URL pattern matched "example.com" inside the email address and
    # proposed it as the candidate's portfolio — for a Gmail address, gmail.com.

    lines = read_lines(
        "Jordan A. Rivera\n(555) 014-2288 | jordan.rivera@gmail.com | linkedin.com/in/jordan\n"
    ).lines
    kinds = {field.kind for field in parse_header(lines).fields}
    assert ProfileFieldKind.PORTFOLIO_URL not in kinds
    assert {ProfileFieldKind.EMAIL, ProfileFieldKind.LINKEDIN_URL} <= kinds
