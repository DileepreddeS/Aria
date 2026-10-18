"""Stage 1, the rules (RESUME_ENGINE §3): three synthetic postings read without a model.

Each posting is fictional and exercises one shape: Larkspur has conventional headings and
bullets, Tidewell has capitalised headings, a citizenship line and an injection attempt,
Corvane is prose under unusual headings with no location line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aria_resume.jd.reader import read_jd
from aria_resume.schemas import JDSection, ParsedJD, Seniority, WorkMode

from aria_core.taxonomy.model import SkillTaxonomy

JDS = Path(__file__).parent / "fixtures" / "jds"


def _read(name: str, taxonomy: SkillTaxonomy) -> ParsedJD:
    return read_jd((JDS / f"{name}.txt").read_text(encoding="utf-8"), taxonomy)


class TestLarkspur:
    @pytest.fixture
    def jd(self, taxonomy: SkillTaxonomy) -> ParsedJD:
        return _read("larkspur_backend", taxonomy)

    def test_sections(self, jd: ParsedJD) -> None:
        assert [item.text for item in jd.required] == [
            "BS in Computer Science or a related field, or equivalent experience",
            "1+ years of professional software engineering experience",
            "Experience with Python or Go",
            "2+ years of SQL, ideally Postgres",
            "Familiarity with Docker and Kubernetes",
        ]
        assert {item.section for item in jd.required} == {JDSection.REQUIRED}
        assert [item.text for item in jd.preferred] == [
            "Experience with Kafka or RabbitMQ",
            "MariaDB in production",
            "Exposure to Zorblang or other niche DSLs",
        ]
        assert len(jd.responsibilities) == 3
        assert jd.responsibilities[0].startswith("Build REST APIs in Node")

    def test_the_facts_rules_can_read(self, jd: ParsedJD) -> None:
        assert jd.min_years == 1
        assert jd.education == ("BS in Computer Science or a related field, or equivalent experience",)
        assert jd.location == "Portland, OR (Hybrid, 3 days in office)"
        assert jd.work_mode is WorkMode.HYBRID
        assert jd.seniority is Seniority.NEW_GRAD
        assert jd.sponsorship_language is not None
        assert jd.sponsorship_language.startswith("Larkspur is unable to sponsor")
        assert "authorized to work in the United States" in jd.sponsorship_language
        assert jd.clearance_or_citizenship is None
        assert jd.suspicious_instructions == ()

    def test_what_a_model_must_read_is_left_unread(self, jd: ParsedJD) -> None:
        # Rules could guess both from the first lines; a guess is not a reading.
        assert jd.title is None and jd.company is None

    def test_keywords_keep_the_posting_s_spelling(self, jd: ParsedJD) -> None:
        for surface in ("PostgreSQL", "Postgres", "SQL", "Python", "Go", "Docker", "Kubernetes", "MariaDB"):
            assert surface in jd.keywords
        # Benefits are not requirements, and "401(k)" is not a skill.
        assert not any("401" in keyword for keyword in jd.keywords)


class TestTidewell:
    @pytest.fixture
    def jd(self, taxonomy: SkillTaxonomy) -> ParsedJD:
        return _read("tidewell_frontend", taxonomy)

    def test_capitalised_headings_and_the_longer_heading_wins(self, jd: ParsedJD) -> None:
        assert [item.text for item in jd.required] == [
            "5+ years of experience building web applications",
            "3+ years with React and TypeScript",
            "Experience with Zustand, MobX or Redux",
        ]
        assert [item.text for item in jd.preferred] == [
            "Experience with HIPAA-regulated products",
            "Playwright or Cypress for end-to-end tests",
        ]

    def test_the_facts_rules_can_read(self, jd: ParsedJD) -> None:
        assert jd.min_years == 5
        assert jd.location == "Remote (US only)"
        assert jd.work_mode is WorkMode.REMOTE
        assert jd.seniority is Seniority.SENIOR
        assert jd.clearance_or_citizenship == (
            "This position requires U.S. citizenship due to federal contract requirements."
        )

    def test_the_injection_and_the_document_request_are_flagged_and_never_requirements(
        self, jd: ParsedJD
    ) -> None:
        kinds = [entry.split(":", 1)[0] for entry in jd.suspicious_instructions]
        assert kinds == ["addressed_to_ai", "asks_for_documents"]
        everything = [item.text for item in jd.required + jd.preferred] + list(jd.responsibilities)
        assert not any("passport" in text or "AI assistants" in text for text in everything)

    def test_hidden_characters_are_removed_and_reported(self, taxonomy: SkillTaxonomy) -> None:
        text = (JDS / "tidewell_frontend.txt").read_text(encoding="utf-8")
        hidden = text.replace("React and TypeScript", "React\u200b and Type\u2060Script\u202e")
        jd = read_jd(hidden, taxonomy)
        assert "3+ years with React and TypeScript" in [item.text for item in jd.required]
        assert any(entry.startswith("hidden_text:") for entry in jd.suspicious_instructions)


class TestCorvane:
    @pytest.fixture
    def jd(self, taxonomy: SkillTaxonomy) -> ParsedJD:
        return _read("corvane_ai", taxonomy)

    def test_prose_is_split_into_sentences_under_unusual_headings(self, jd: ParsedJD) -> None:
        assert jd.responsibilities[0] == "Build RAG pipelines over millions of court documents."
        assert len(jd.responsibilities) == 3
        assert [item.text for item in jd.required][:2] == [
            "You have 2-4 years of experience shipping machine learning systems to production.",
            "You are fluent in Python and have used PyTorch or Hugging Face transformers.",
        ]
        assert [item.text for item in jd.preferred] == [
            "Experience fine-tuning LLMs with LoRA.",
            "Familiarity with LangGraph.",
        ]

    def test_the_facts_rules_can_read(self, jd: ParsedJD) -> None:
        # "2-4 years of experience ..." is the general floor, not a machine learning one.
        assert jd.min_years == 2
        assert jd.location == "Austin, TX"
        assert jd.work_mode is WorkMode.ONSITE
        # The first line is a sentence, not a title: no seniority read from it.
        assert jd.seniority is Seniority.UNKNOWN
        assert jd.sponsorship_language == "We sponsor H-1B visas for this role."
        assert jd.education == (
            "A Master's degree in Computer Science, Statistics or a related field is preferred but "
            "not required.",
        )


def test_a_posting_with_no_headings_reads_nothing_as_a_requirement(taxonomy: SkillTaxonomy) -> None:
    # Which lines are required is for the model step to say; the rules do not guess.
    jd = read_jd("We need a Python developer. Kafka is a plus.", taxonomy)
    assert jd.required == () and jd.preferred == ()
    assert "Python" in jd.keywords


def test_an_overlong_posting_is_cut_and_says_so(taxonomy: SkillTaxonomy) -> None:
    jd = read_jd("Requirements:\n- Python\n" + "x " * 40_000, taxonomy)
    assert any(entry.startswith("oversized:") for entry in jd.suspicious_instructions)
