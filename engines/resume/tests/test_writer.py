"""Stage 5b, the writer: one bullet per slot, from the slot's facts alone, judged by code.

Every reply here is scripted. Most are written to fail a rule (an invented number, a
technology the facts do not name, a fact the slot was not given, a banned word, a weak
opening, a bullet too long, JSON that breaks the schema) and the writer must answer each with
a rewrite, another fact, or the fact's own words; never with the failing bullet.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from aria_resume.compose.planner import plan
from aria_resume.compose.writer import INSTRUCTIONS, write_page
from aria_resume.jd.reader import read_jd
from aria_resume.llm import LlmPurpose, LlmRequest, ScriptedLlm
from aria_resume.match.embeddings import WordHashEmbedder
from aria_resume.match.matcher import match
from aria_resume.normalize.requirements import normalize
from aria_resume.schemas import EvidenceMap, NormalizedJD, ResumePlan
from aria_resume.validate.style import load_style

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


def _named(snapshot: CandidateSnapshot) -> CandidateSnapshot:
    degree = SnapshotEducation(
        education_id=uuid.UUID(int=7),
        school="Cascadia State University",
        degree="M.S.",
        field_of_study="Computer Science",
        end_month=dt.date(2026, 12, 1),
    )
    return snapshot.model_copy(update={"contact": CONTACT, "education": (degree,)})


Inputs = tuple[CandidateSnapshot, NormalizedJD, EvidenceMap, ResumePlan]


@pytest.fixture
def inputs(snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy) -> Inputs:
    named = _named(snapshot)
    normalized = normalize(
        read_jd((JDS / "larkspur_backend.txt").read_text(encoding="utf-8"), taxonomy), taxonomy
    )
    evidence = match(normalized, named, taxonomy, WordHashEmbedder())
    return named, normalized, evidence, plan(normalized, evidence, named, taxonomy)


def _facts(request: LlmRequest) -> list[dict[str, str]]:
    loaded: list[dict[str, str]] = json.loads(request.inputs["fact_texts"])
    return loaded


def _honest(request: LlmRequest) -> str:
    """Says the first fact in its own words, declaring what it prints."""
    fact = _facts(request)[0]
    metrics = [m for m in json.loads(request.inputs["metrics"]) if m in fact["text"]]
    return json.dumps(
        {"text": fact["text"], "fact_ids": [fact["id"]], "metrics_used": metrics, "tech_used": []}
    )


def _with(**change: Any) -> Any:
    def reply(request: LlmRequest) -> str:
        honest = json.loads(_honest(request))
        return json.dumps(honest | change(request) if callable(change) else honest | change)

    return reply


def _slots(inputs: Inputs) -> int:
    return sum(len(entry.bullets) for section in inputs[3].sections for entry in section.entries)


async def _write(inputs: Inputs, taxonomy: SkillTaxonomy, *replies: Any) -> tuple[Any, Any, ScriptedLlm]:
    snapshot, normalized, evidence, planned = inputs
    llm = ScriptedLlm({LlmPurpose.WRITE_BULLET: list(replies)})
    written, stats = await write_page(planned, snapshot, normalized, evidence, taxonomy, load_style(), llm)
    return written, stats, llm


class TestAnHonestWriter:
    async def test_every_slot_is_written_first_time(self, inputs: Inputs, taxonomy: SkillTaxonomy) -> None:
        count = _slots(inputs)
        written, stats, _ = await _write(inputs, taxonomy, *[_honest] * count)
        assert len(written) == count
        assert (stats.first_try, stats.rewritten, stats.swapped, stats.fallback) == (count, 0, 0, 0)


class TestWhatIsSent:
    async def test_only_fact_text_never_contact_details(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        _, _, llm = await _write(inputs, taxonomy, *[_honest] * _slots(inputs))
        for request in llm.requests:
            sent = request.trusted_instructions + json.dumps(request.inputs)
            for private in (
                str(CONTACT.full_name),
                str(CONTACT.email),
                str(CONTACT.phone),
                "linkedin",
                "Portland",
            ):
                assert private not in sent
            assert request.untrusted_content == ()
            assert request.purpose is LlmPurpose.WRITE_BULLET
            assert request.trusted_instructions == INSTRUCTIONS

    async def test_the_requirement_is_the_taxonomy_s_name_not_the_posting_s_words(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        _, _, llm = await _write(inputs, taxonomy, *[_honest] * _slots(inputs))
        posting_lines = {
            r.text
            for r in read_jd((JDS / "larkspur_backend.txt").read_text(encoding="utf-8"), taxonomy).required
        }
        named = [
            name for request in llm.requests for name in json.loads(request.inputs["target_requirement"])
        ]
        assert named and not set(named) & posting_lines

    async def test_the_next_request_is_told_which_verb_to_avoid(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        _, _, llm = await _write(inputs, taxonomy, *[_honest] * _slots(inputs))
        first, second = llm.requests[0], llm.requests[1]
        opened = json.loads(_honest(first))["text"].split()[0].lower()
        assert opened in json.loads(second.inputs["style_rules"])["avoid_opening_verbs"]


#: A posting that names its skills only by aliases the tagger resolves, none of them a
#: substring of the taxonomy's own name: "Node JS" is Node.js, "CPython" is Python,
#: "ReactJS" is React. ("Postgres" also resolves, but sits inside "PostgreSQL", so it could
#: not tell the two apart; "psql" is an alias the tagger does not read, a taxonomy gap.)
ALIASED_POSTING = """Backend Engineer, Quillon Systems

Requirements:
- Building services in Node JS
- Deep CPython internals
- ReactJS front ends
"""
ALIASES = ("node js", "cpython", "reactjs")


class TestTheWritersInjectionBoundary:
    """What the posting says reaches the writer only as the taxonomy's name for it (DECISIONS 2026-10-23)."""

    async def test_a_posting_s_own_spelling_never_reaches_a_writer_request(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        named = _named(snapshot)
        normalized = normalize(read_jd(ALIASED_POSTING, taxonomy), taxonomy)
        evidence = match(normalized, named, taxonomy, WordHashEmbedder())
        planned = plan(normalized, evidence, named, taxonomy)
        count = sum(len(entry.bullets) for section in planned.sections for entry in section.entries)
        llm = ScriptedLlm({LlmPurpose.WRITE_BULLET: [_honest] * count})
        await write_page(planned, named, normalized, evidence, taxonomy, load_style(), llm)

        served = {
            name for request in llm.requests for name in json.loads(request.inputs["target_requirement"])
        }
        assert {"Node.js", "Python", "React"} <= served
        for request in llm.requests:
            sent = (request.trusted_instructions + json.dumps(request.inputs)).lower()
            assert not [alias for alias in ALIASES if alias in sent]


class TestAFailingBulletIsRepaired:
    async def test_an_invented_number_is_rewritten_with_the_finding(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        invented = _with(text="Cut p95 API latency by 45% with Redis caching")
        count = _slots(inputs)
        written, stats, llm = await _write(inputs, taxonomy, invented, *[_honest] * count)
        assert stats.rewritten == 1 and stats.fallback == 0
        feedback = json.loads(llm.requests[1].inputs["style_rules"])["fix_these"]
        assert any("45%" in item for item in feedback)
        assert all("45%" not in bullet.text for bullet in written.values())

    @pytest.mark.parametrize(
        "bad",
        [
            {"text": "Cut p95 API latency by 30% with Redis and Kubernetes caching", "metrics_used": ["30%"]},
            {"text": "Leveraged Redis caching to cut p95 API latency by 30%", "metrics_used": ["30%"]},
            {"text": "Responsible for cutting p95 API latency by 30% with Redis", "metrics_used": ["30%"]},
            {
                # Three printed lines, but under the schema's 400 characters, so only the
                # line budget can refuse it.
                "text": "Cut p95 API latency by 30% with Redis caching" + " across services" * 13,
                "metrics_used": ["30%"],
            },
            {"fact_ids": [str(uuid.UUID(int=999))]},
        ],
        ids=["unlisted_tech", "banned_word", "weak_opening", "too_long", "fact_not_given"],
    )
    async def test_a_bullet_that_breaks_a_rule_is_never_kept(
        self, inputs: Inputs, taxonomy: SkillTaxonomy, bad: dict[str, Any]
    ) -> None:
        # The first slot fails every time it is asked; the rest are honest.
        count = _slots(inputs)
        written, stats, _ = await _write(inputs, taxonomy, *[_with(**bad)] * 3, *[_honest] * count)
        first = written[next(iter(written))]
        assert stats.fallback + stats.swapped >= 1
        assert "Kubernetes" not in first.text and "Leveraged" not in first.text
        assert not first.text.startswith("Responsible")

    async def test_json_that_breaks_the_schema_is_asked_again(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        count = _slots(inputs)
        _, stats, llm = await _write(inputs, taxonomy, '{"text": "Cut p95', *[_honest] * count)
        assert stats.rewritten == 1
        assert "schema" in json.loads(llm.requests[1].inputs["style_rules"])["fix_these"][0]

    async def test_after_two_repairs_the_fact_s_own_words_stand(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        # The spec's limit, written here rather than read from the module: a first attempt
        # and two repairs, three requests a slot at most (RESUME_ENGINE section 9).
        per_slot = 3
        snapshot = inputs[0]
        always_bad = _with(text="Leveraged everything")
        count = _slots(inputs)
        written, stats, llm = await _write(inputs, taxonomy, *[always_bad] * (count * per_slot))
        assert stats.first_try == stats.rewritten == 0
        assert stats.fallback + stats.swapped == count
        for bullet in written.values():
            assert bullet.text == " ".join(snapshot.fact(bullet.fact_ids[0]).text.split())
        assert stats.requests == len(llm.requests) == count * per_slot


class TestNoModel:
    async def test_an_unavailable_writer_is_asked_once_and_the_page_keeps_its_facts(
        self, inputs: Inputs, taxonomy: SkillTaxonomy
    ) -> None:
        written, stats, llm = await _write(inputs, taxonomy)  # nothing scripted: LlmUnavailable
        assert len(llm.requests) == 1
        assert stats.fallback == _slots(inputs) == len(written)
        assert any("writer unavailable" in note for note in stats.notes)
