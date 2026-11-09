"""6c's 6-second test: the top third must show at least two of the job's top three requirements.

The model's replies are scripted. What counts is decided here: a requirement is shown only
with a quote the top third holds word for word, so a confident "yes" with an invented quote
counts for nothing, and a page that shows fewer than two is a finding naming what is missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aria_resume.document import (
    ContactItem,
    DocumentBullet,
    DocumentEntry,
    DocumentSection,
    ResumeDocument,
    SectionKind,
)
from aria_resume.jd.reader import read_jd
from aria_resume.llm import LlmPurpose, LlmRequest, ScriptedLlm
from aria_resume.normalize.requirements import normalize
from aria_resume.schemas import NormalizedJD, RequirementKind
from aria_resume.validate.six_second import (
    INSTRUCTIONS,
    RESPONSE_SCHEMA,
    six_second_check,
    top_requirements,
    top_third,
)

from aria_core.taxonomy.model import SkillTaxonomy

JDS = Path(__file__).parent / "fixtures" / "jds"
NAMED = (RequirementKind.SKILL, RequirementKind.DOMAIN, RequirementKind.SOFT)
BULLETS = (
    "Cut p95 API latency by 30% with Redis caching",
    "Built 15+ React screens for the checkout platform",
    "Shipped 8 full-stack applications with Node.js and PostgreSQL",
    "Wrote the on-call runbook adopted by three teams",
)


def _document(
    summary: str = "Software Engineer with 2+ years of Python and PostgreSQL work",
) -> ResumeDocument:
    entry = DocumentEntry(
        title="Nordvale Consulting",
        detail="Software Engineer",
        dates="2024",
        bullets=tuple(DocumentBullet(text=text) for text in BULLETS),
    )
    return ResumeDocument(
        name="Jordan A. Rivera",
        contact=(ContactItem(text="jordan.rivera@example.com", url="mailto:jordan.rivera@example.com"),),
        sections=(
            DocumentSection(kind=SectionKind.SUMMARY, lines=(summary,)),
            DocumentSection(kind=SectionKind.EXPERIENCE, entries=(entry,)),
            DocumentSection(
                kind=SectionKind.SKILLS, lines=("Languages: Python, SQL, TypeScript", "Tools: Docker")
            ),
        ),
    )


@pytest.fixture
def normalized(taxonomy: SkillTaxonomy) -> NormalizedJD:
    return normalize(read_jd((JDS / "larkspur_backend.txt").read_text(encoding="utf-8"), taxonomy), taxonomy)


def _answer(*seen: tuple[str, bool, str]) -> Any:
    def reply(request: LlmRequest) -> str:
        return json.dumps(
            {
                "requirements": [
                    {"requirement": name, "shown": shown, "quote": quote} for name, shown, quote in seen
                ]
            }
        )

    return reply


async def _check(
    normalized: NormalizedJD, taxonomy: SkillTaxonomy, *replies: Any, document: ResumeDocument | None = None
) -> Any:
    llm = ScriptedLlm({LlmPurpose.SIX_SECOND_CHECK: list(replies)})
    result = await six_second_check(document or _document(), normalized, taxonomy, llm)
    return result, llm


class TestWhatIsAsked:
    def test_the_top_three_are_must_haves_first_by_the_taxonomy_s_name(
        self, normalized: NormalizedJD, taxonomy: SkillTaxonomy
    ) -> None:
        asked = top_requirements(normalized, taxonomy)
        assert 1 <= len(asked) <= 3
        named = [r for r in normalized.requirements if r.kind in NAMED]
        names = {taxonomy.display_form(r.canonical) for r in named}
        assert set(asked) <= names
        must = [taxonomy.display_form(r.canonical) for r in named if r.must_have]
        assert asked[0] in must

    def test_a_must_have_outranks_a_heavier_nice_to_have(
        self, normalized: NormalizedJD, taxonomy: SkillTaxonomy
    ) -> None:
        named = [r for r in normalized.requirements if r.kind in NAMED]
        nice = next(r for r in named if not r.must_have).model_copy(update={"weight": 1.0})
        musts = [r.model_copy(update={"weight": 0.1}) for r in named if r.must_have][:3]
        assert len(musts) == 3
        reweighted = normalized.model_copy(update={"requirements": (nice, *musts)})
        assert top_requirements(reweighted, taxonomy) == tuple(
            taxonomy.display_form(r.canonical) for r in musts
        )

    def test_the_top_third_is_the_summary_three_bullets_and_one_skills_line(self) -> None:
        assert top_third(_document()).splitlines() == [
            "Software Engineer with 2+ years of Python and PostgreSQL work",
            *BULLETS[:3],
            "Languages: Python, SQL, TypeScript",
        ]

    async def test_the_request_carries_the_page_not_the_contact_details(
        self, normalized: NormalizedJD, taxonomy: SkillTaxonomy
    ) -> None:
        _, llm = await _check(normalized, taxonomy, _answer())
        (request,) = llm.requests
        assert request.purpose is LlmPurpose.SIX_SECOND_CHECK
        assert request.trusted_instructions == INSTRUCTIONS
        assert request.untrusted_content == ()
        assert "jordan.rivera@example.com" not in json.dumps(request.inputs)
        assert BULLETS[3] not in request.inputs["rendered_top_third"]
        assert json.loads(request.inputs["response_schema"]) == RESPONSE_SCHEMA


class TestWhatCounts:
    async def test_two_shown_with_true_quotes_pass(
        self, normalized: NormalizedJD, taxonomy: SkillTaxonomy
    ) -> None:
        asked = top_requirements(normalized, taxonomy)
        page = top_third(_document())
        seen = [(name, True, page.splitlines()[0]) for name in asked[:2]] + [
            (name, False, "") for name in asked[2:]
        ]
        result, _ = await _check(normalized, taxonomy, _answer(*seen))
        assert result.ran and result.findings == []
        assert set(result.shown) == set(asked[:2])

    async def test_a_yes_with_a_quote_the_page_does_not_hold_counts_for_nothing(
        self, normalized: NormalizedJD, taxonomy: SkillTaxonomy
    ) -> None:
        asked = top_requirements(normalized, taxonomy)
        seen = [(name, True, "Led a team of 40 engineers on Kubernetes") for name in asked]
        result, _ = await _check(normalized, taxonomy, _answer(*seen))
        assert result.shown == {}
        assert [finding.check for finding in result.findings] == ["six_second"]
        assert any("does not hold" in note for note in result.notes)

    async def test_one_of_three_is_a_finding_naming_the_missing(
        self, normalized: NormalizedJD, taxonomy: SkillTaxonomy
    ) -> None:
        asked = top_requirements(normalized, taxonomy)
        assert len(asked) == 3
        quote = BULLETS[0]
        result, _ = await _check(
            normalized,
            taxonomy,
            _answer((asked[0], True, quote), (asked[1], False, ""), (asked[2], False, "")),
        )
        (finding,) = result.findings
        assert "shows 1 of the top 3" in finding.detail
        assert asked[1] in finding.detail and asked[2] in finding.detail

    async def test_a_quote_is_found_whatever_its_spacing_and_case(
        self, normalized: NormalizedJD, taxonomy: SkillTaxonomy
    ) -> None:
        asked = top_requirements(normalized, taxonomy)
        seen = [(name, True, "  CUT p95 api   latency ") for name in asked[:2]]
        result, _ = await _check(normalized, taxonomy, _answer(*seen))
        assert result.findings == []

    async def test_a_reply_that_is_not_the_schema_shows_nothing(
        self, normalized: NormalizedJD, taxonomy: SkillTaxonomy
    ) -> None:
        result, _ = await _check(normalized, taxonomy, lambda request: "```json\n{}\n```")
        assert result.shown == {} and [finding.check for finding in result.findings] == ["six_second"]


class TestNotRun:
    async def test_an_unavailable_model_is_a_note_not_a_finding(
        self, normalized: NormalizedJD, taxonomy: SkillTaxonomy
    ) -> None:
        result, _ = await _check(normalized, taxonomy)  # nothing scripted: LlmUnavailable
        assert (result.ran, result.findings) == (False, [])
        assert any("not run" in note for note in result.notes)
