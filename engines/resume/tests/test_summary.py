"""Stage 5b's summary: two lines from the page's top evidence, judged by code, or none.

Every reply is scripted. The honest one opens with a given identity, adds the counted years
when there are any, and says a proof fact in its own words; the others each break one rule
(invented years, a skill not given, an identity not given, the first person, a banned word,
a fact not among the proof, the schema) and must be refused, and a page whose replies all
fail gets no summary at all.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from aria_resume.compose.fallback import fallback_document
from aria_resume.compose.line_budget import PAGE
from aria_resume.compose.planner import plan
from aria_resume.compose.summary import (
    INSTRUCTIONS,
    RESPONSE_SCHEMA,
    SUMMARY_LINES,
    summary_inputs,
    write_summary,
)
from aria_resume.document import SectionKind
from aria_resume.jd.reader import read_jd
from aria_resume.llm import LlmPurpose, LlmRequest, ScriptedLlm
from aria_resume.match.embeddings import WordHashEmbedder
from aria_resume.match.matcher import match
from aria_resume.normalize.requirements import normalize
from aria_resume.schemas import EvidenceMap, NormalizedJD, ResumePlan
from aria_resume.validate.six_second import ranked_requirements
from aria_resume.validate.style import load_style

from aria_core.graph.counted_years import counted_years
from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotContact, SnapshotEducation
from aria_core.taxonomy.model import SkillTaxonomy

JDS = Path(__file__).parent / "fixtures" / "jds"
CONTACT = SnapshotContact(
    full_name="Jordan A. Rivera",
    email="jordan.rivera@example.com",
    phone="(555) 014-2288",
    location_city="Portland",
    location_region="OR",
    linkedin_url="https://www.linkedin.com/in/jordan-rivera-example",
)

Inputs = tuple[CandidateSnapshot, NormalizedJD, EvidenceMap, ResumePlan]


@pytest.fixture
def inputs(snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy) -> Inputs:
    degree = SnapshotEducation(
        education_id=uuid.UUID(int=7),
        school="Cascadia State University",
        degree="M.S.",
        field_of_study="Computer Science",
        end_month=dt.date(2026, 12, 1),
    )
    named = snapshot.model_copy(update={"contact": CONTACT, "education": (degree,)})
    normalized = normalize(
        read_jd((JDS / "larkspur_backend.txt").read_text(encoding="utf-8"), taxonomy), taxonomy
    )
    evidence = match(normalized, named, taxonomy, WordHashEmbedder())
    planned = plan(normalized, evidence, named, taxonomy, summary_lines=SUMMARY_LINES)
    return named, normalized, evidence, planned


def _honest(request: LlmRequest) -> str:
    identity = json.loads(request.inputs["identities"])[0]
    years = json.loads(request.inputs["years_claim"])
    proof = min(json.loads(request.inputs["proof_facts"]), key=lambda fact: len(fact["text"]))
    text = identity + (f" with {years}" if years else "") + "; " + proof["text"]
    return json.dumps({"text": text, "identity": identity, "fact_ids": [proof["id"]], "skills_used": []})


def _with(change: Any) -> Any:
    def reply(request: LlmRequest) -> str:
        honest = json.loads(_honest(request))
        return json.dumps(honest | change(honest, request))

    return reply


async def _summary(inputs: Inputs, taxonomy: SkillTaxonomy, *replies: Any) -> tuple[Any, ScriptedLlm]:
    snapshot, normalized, evidence, planned = inputs
    llm = ScriptedLlm({LlmPurpose.WRITE_SUMMARY: list(replies)})
    result = await write_summary(planned, snapshot, normalized, evidence, taxonomy, load_style(), llm)
    return result, llm


class TestWhatTheSummaryMaySay:
    def test_identities_are_the_page_s_role_titles_and_nothing_else(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        snapshot, normalized, evidence, planned = inputs
        chosen = summary_inputs(planned, snapshot, normalized, evidence, taxonomy)
        assert chosen is not None
        on_page = {entry.source_id for section in planned.sections for entry in section.entries}
        titles = {role.title for role in snapshot.experiences if role.experience_id in on_page}
        assert chosen.identities and set(chosen.identities) <= titles

    def test_the_years_are_the_counted_figure_s_own_phrase(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        snapshot, normalized, evidence, planned = inputs
        chosen = summary_inputs(planned, snapshot, normalized, evidence, taxonomy)
        assert chosen is not None and chosen.years_claim == counted_years(snapshot).claim()

    def test_proof_is_facts_on_the_page_and_skills_are_evidenced(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        snapshot, normalized, evidence, planned = inputs
        chosen = summary_inputs(planned, snapshot, normalized, evidence, taxonomy)
        assert chosen is not None
        on_page = {
            fact_id
            for section in planned.sections
            for entry in section.entries
            for slot in entry.bullets
            for fact_id in slot.fact_ids
        }
        assert 1 <= len(chosen.proof) <= 3
        assert {fact_id for fact_id, _, _ in chosen.proof} <= on_page
        top = {i for _, ids in ranked_requirements(normalized, evidence, taxonomy) for i in ids}
        allowed = {
            form
            for requirement in normalized.requirements
            if (requirement.must_have or requirement.id in top) and requirement.id in evidence.strong
            for form in requirement.surface_forms
        }
        assert len(chosen.skills) <= 4 and set(chosen.skills) <= allowed

    def test_an_evidenced_nice_to_have_the_test_asks_for_is_offered(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        snapshot, normalized, evidence, planned = inputs
        nice = normalized.model_copy(
            update={
                "requirements": tuple(
                    r.model_copy(update={"must_have": False}) for r in normalized.requirements
                )
            }
        )
        chosen = summary_inputs(planned, snapshot, nice, evidence, taxonomy)
        assert chosen is not None and chosen.skills
        top = {i for _, ids in ranked_requirements(nice, evidence, taxonomy) for i in ids}
        by_form = {form: r for r in nice.requirements for form in r.surface_forms}
        assert all(by_form[skill].id in top for skill in chosen.skills)

    def test_the_six_second_test_s_top_requirements_lead_the_skills(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        """Where the first role proves none of them, the summary is the top third's only chance
        to show them (DECISIONS 2026-11-09). The posting lists them last, so the order is the
        ranking's, not the posting's."""
        snapshot, normalized, evidence, planned = inputs
        backwards = normalized.model_copy(update={"requirements": tuple(reversed(normalized.requirements))})
        chosen = summary_inputs(planned, snapshot, backwards, evidence, taxonomy)
        assert chosen is not None
        by_form = {form: r for r in backwards.requirements for form in r.surface_forms}
        ranked = ranked_requirements(backwards, evidence, taxonomy)
        ranks = [
            next((rank for rank, (_, ids) in enumerate(ranked) if by_form[skill].id in ids), len(ranked))
            for skill in chosen.skills
        ]
        assert len([rank for rank in ranks if rank < len(ranked)]) >= 2
        assert ranks == sorted(ranks)


class TestWhatIsSent:
    async def test_no_contact_details_no_employer_and_the_schema(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        _, llm = await _summary(inputs, taxonomy, _honest)
        (request,) = llm.requests
        sent = request.trusted_instructions + json.dumps(request.inputs)
        for private in (CONTACT.full_name, CONTACT.email, CONTACT.phone, "linkedin", "Portland"):
            assert str(private) not in sent
        for role in inputs[0].experiences:
            assert role.employer not in sent
        assert request.purpose is LlmPurpose.WRITE_SUMMARY
        assert request.trusted_instructions == INSTRUCTIONS
        assert request.untrusted_content == ()
        assert json.loads(request.inputs["response_schema"]) == RESPONSE_SCHEMA


class TestAnHonestSummary:
    async def test_passes_first_time(self, inputs: Inputs, taxonomy: SkillTaxonomy) -> None:
        result, _ = await _summary(inputs, taxonomy, _honest)
        assert result.text is not None
        assert (result.requests, result.repairs) == (1, 0)

    def test_it_is_the_first_section_of_the_page(self, inputs: Inputs) -> None:
        snapshot, _, _, planned = inputs
        document = fallback_document(planned, snapshot, summary="Software Engineer; Built a thing")
        assert document.sections[0].kind is SectionKind.SUMMARY
        assert document.sections[0].lines == ("Software Engineer; Built a thing",)
        assert all(
            section.kind is not SectionKind.SUMMARY
            for section in fallback_document(planned, snapshot).sections
        )


def _invented_years(honest: dict[str, Any], request: LlmRequest) -> dict[str, Any]:
    return {"text": honest["identity"] + " with 9+ years" + honest["text"][len(honest["identity"]) :]}


@pytest.mark.parametrize(
    ("name", "change", "check"),
    [
        ("invented years", _invented_years, "claims '9+ years'"),
        (
            "a skill not given",
            lambda h, r: {"skills_used": ["Kubernetes"]},
            "names 'Kubernetes', which is not among skills",
        ),
        (
            "an identity not given",
            lambda h, r: {
                "identity": "Staff Engineer",
                "text": "Staff Engineer" + h["text"][len(h["identity"]) :],
            },
            "must open with one of",
        ),
        (
            "the first person",
            lambda h, r: {"text": h["text"] + "; I lead the team"},
            "speaks in the first person",
        ),
        (
            "a banned word",
            lambda h, r: {"text": h["text"] + ", a passionate engineer"},
            "uses the banned word",
        ),
        (
            "a fact not among the proof",
            lambda h, r: {"fact_ids": [str(uuid.UUID(int=999))]},
            "which is not among proof_facts",
        ),
        (
            "too long",
            lambda h, r: {"text": h["text"] + "; " + "and more " * 18},
            "lines; the summary holds 2",
        ),
    ],
)
async def test_a_reply_that_breaks_a_rule_is_refused_then_repaired(
    inputs: Inputs, taxonomy: SkillTaxonomy, name: str, change: Any, check: str
) -> None:
    result, llm = await _summary(inputs, taxonomy, _with(change), _honest)
    assert result.text is not None and result.repairs == 1, name
    feedback = json.loads(llm.requests[1].inputs["style_rules"])["fix_these"]
    assert any(check in detail for detail in feedback), (name, feedback)
    assert result.text == json.loads(_honest(llm.requests[1]))["text"]


async def test_a_reply_that_is_not_the_schema_is_refused(inputs: Inputs, taxonomy: SkillTaxonomy) -> None:
    fenced = lambda request: "```json\n" + _honest(request) + "\n```"  # noqa: E731
    result, llm = await _summary(inputs, taxonomy, fenced, _honest)
    assert (result.text is not None, result.repairs) == (True, 1)
    assert "schema" in json.loads(llm.requests[1].inputs["style_rules"])["fix_these"][0]


class TestNoSummary:
    async def test_three_failed_replies_leave_the_page_without_one(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        result, llm = await _summary(inputs, taxonomy, *[_with(_invented_years)] * 3)
        assert result.text is None
        assert len(llm.requests) == 3
        assert any("failed" in note for note in result.notes)

    async def test_an_unavailable_writer_leaves_none(self, inputs: Inputs, taxonomy: SkillTaxonomy) -> None:
        result, _ = await _summary(inputs, taxonomy)  # nothing scripted: LlmUnavailable
        assert result.text is None
        assert any("unavailable" in note for note in result.notes)


class TestThePlanKeepsRoom:
    def test_the_reserve_is_in_the_estimate_and_within_the_page(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        snapshot, normalized, evidence, with_room = inputs
        assert with_room.estimated_height <= PAGE.usable_height
        crowded = plan(normalized, evidence, snapshot, taxonomy, summary_lines=40)
        bullets = sum(len(entry.bullets) for section in crowded.sections for entry in section.entries)
        assert bullets < sum(
            len(entry.bullets) for section in with_room.sections for entry in section.entries
        )
