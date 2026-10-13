"""A plain-text notes file is a list of skills, and nothing more.

The founder asked for their ARIA v1 skills notes to be importable as a ``.txt`` source. The
risk is that running the resume parser over it would find the word "experience" in one
of his own headings and start inventing roles out of a word list.

So the notes importer produces declared skills and **no facts at all**. A note saying
"I know Python" does not say what was built with it, and a declared skill is not
evidence — the two are kept apart on purpose (RESUME_ENGINE §4).
"""

from __future__ import annotations

import pytest

from aria_core.graph.status import GraphSource
from aria_core.importer.notes import parse_skills_notes, parse_skills_notes_text
from aria_core.importer.plan import QuestionKind
from aria_core.schemas.untrusted import UntrustedContent, UntrustedSourceKind
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_core.taxonomy.model import SkillTaxonomy

NOTES = """Languages
Python, TypeScript, Java, C++

Frontend
React, Next.js, Tailwind CSS

Backend and data
FastAPI, PostgreSQL, Redis, dbt

ML
PyTorch, scikit-learn, YOLO, Hugging Face

Things I want to learn
Rust, Elixir, Zig

Notes to self
I spent about two years doing backend work at Accenture and want to keep doing it.
"""


@pytest.fixture(scope="module")
def taxonomy() -> SkillTaxonomy:
    return load_default_taxonomy()


class TestNotesBecomeDeclaredSkills:
    def test_the_skills_resolve(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_skills_notes_text(NOTES, taxonomy)
        canonical = {skill.canonical for skill in plan.declared_skills}
        assert {"python", "typescript", "cpp", "react", "fastapi", "postgresql", "pytorch"} <= canonical

    def test_no_facts_are_invented(self, taxonomy: SkillTaxonomy) -> None:
        # A word list is not evidence. Manufacturing a fact from one would create a
        # claim with nothing behind it.
        plan = parse_skills_notes_text(NOTES, taxonomy)
        assert plan.all_facts == ()
        assert plan.experiences == ()
        assert plan.educations == ()
        assert plan.projects == ()

    def test_the_source_is_recorded_as_a_notes_import(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_skills_notes_text(NOTES, taxonomy)
        assert plan.source is GraphSource.SKILLS_NOTES_IMPORT

    def test_each_skill_remembers_its_line(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_skills_notes_text(NOTES, taxonomy)
        python = next(skill for skill in plan.declared_skills if skill.canonical == "python")
        assert python.source_line == 2

    def test_a_lowercase_term_still_resolves_from_a_list(self, taxonomy: SkillTaxonomy) -> None:
        # The prose capitalisation rule does not apply here: the candidate wrote
        # these down as skills, so "sql" means SQL.
        plan = parse_skills_notes_text("sql, css, go", taxonomy)
        assert {skill.canonical for skill in plan.declared_skills} == {"sql", "css", "golang"}


class TestWhatItWillNotDo:
    def test_a_sentence_is_reported_rather_than_chopped_into_skills(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_skills_notes_text(NOTES, taxonomy)
        reported = [block.sample for block in plan.unparsed]
        assert any("two years doing backend work" in sample for sample in reported)

    def test_the_sentence_does_not_become_a_skill(self, taxonomy: SkillTaxonomy) -> None:
        # "I spent about two years doing backend work at Accenture" must not declare
        # a skill, and must certainly not declare two years of it.
        plan = parse_skills_notes_text(NOTES, taxonomy)
        for skill in plan.declared_skills:
            assert "Accenture" not in skill.surface_form
            assert len(skill.surface_form) < 40

    def test_a_heading_the_candidate_wrote_does_not_start_a_section(self, taxonomy: SkillTaxonomy) -> None:
        # "Languages" and "ML" are the candidate's own groupings. The notes importer
        # has no sections, so they are simply terms that do not resolve.
        plan = parse_skills_notes_text(NOTES, taxonomy)
        assert "Things I want to learn" not in plan.unknown_skills

    def test_unresolved_terms_are_reported(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_skills_notes_text("Python, Fortran, COBOL", taxonomy)
        assert set(plan.unknown_skills) == {"Fortran", "COBOL"}
        assert any(q.kind is QuestionKind.UNKNOWN_SKILLS for q in plan.all_questions)


class TestTheBoundary:
    def test_it_takes_untrusted_content(self, taxonomy: SkillTaxonomy) -> None:
        content = UntrustedContent(
            source_kind=UntrustedSourceKind.UPLOADED_DOCUMENT,
            source_ref="upload:notes",
            text="Python, React, Docker",
        )
        plan = parse_skills_notes(content, taxonomy)
        assert len(plan.declared_skills) == 3

    def test_parsing_is_deterministic(self, taxonomy: SkillTaxonomy) -> None:
        first = parse_skills_notes_text(NOTES, taxonomy)
        second = parse_skills_notes_text(NOTES, taxonomy)
        assert first.declared_skills == second.declared_skills
        assert first.unknown_skills == second.unknown_skills

    def test_an_empty_file_is_an_empty_plan(self, taxonomy: SkillTaxonomy) -> None:
        plan = parse_skills_notes_text("", taxonomy)
        assert plan.declared_skills == ()
        assert plan.unparsed == ()
