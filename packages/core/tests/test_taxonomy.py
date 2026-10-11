"""The skill taxonomy resolves, implies and refuses to hide gaps (RESUME_ENGINE §4)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aria_core.taxonomy.loader import DEFAULT_TAXONOMY_PATH, load_default_taxonomy, load_taxonomy
from aria_core.taxonomy.model import SkillCategory, SkillTaxonomy, TaxonomyError, normalise


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "skills.yaml"
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL = """
version: "test.1"
skills:
  - id: javascript
    display: JavaScript
    category: language
    aliases: [js]
  - id: react
    display: React
    category: frontend
    aliases: [react.js, reactjs]
    implies: [javascript]
  - id: nextjs
    display: Next.js
    category: frontend
    implies: [react]
"""


@pytest.fixture
def taxonomy(tmp_path: Path) -> SkillTaxonomy:
    return load_taxonomy(_write(tmp_path, MINIMAL))


class TestResolution:
    def test_a_canonical_id_resolves_to_itself(self, taxonomy: SkillTaxonomy) -> None:
        assert taxonomy.resolve("react") == "react"

    @pytest.mark.parametrize("term", ["React.js", "reactjs", "REACT JS", "  react  ", "React-JS"])
    def test_surface_forms_resolve_to_one_id(self, taxonomy: SkillTaxonomy, term: str) -> None:
        # ATS text is written however the company writes it; matching cannot depend
        # on punctuation or case.
        assert taxonomy.resolve(term) == "react"

    def test_the_display_form_is_also_a_lookup_key(self, taxonomy: SkillTaxonomy) -> None:
        assert taxonomy.resolve("Next.js") == "nextjs"

    def test_an_unknown_term_resolves_to_none(self, taxonomy: SkillTaxonomy) -> None:
        assert taxonomy.resolve("cobol") is None

    def test_unknown_terms_are_reported_not_dropped(self, taxonomy: SkillTaxonomy) -> None:
        # A requirement the taxonomy has not heard of is a gap in the taxonomy.
        # Silently dropping it would lose a real requirement.
        resolved, unknown = taxonomy.resolve_all(["React", "COBOL", "js"])
        assert resolved == {"React": "react", "js": "javascript"}
        assert unknown == ["COBOL"]

    def test_normalisation_keeps_distinctions_that_matter(self) -> None:
        # "c" and "c++" are different skills; "python 3" and "python3" are not.
        assert normalise("C++") != normalise("C")
        assert normalise("Python 3") == normalise("python  3")
        assert normalise("CI/CD") == "ci cd"


class TestImplication:
    def test_implications_are_transitive(self, taxonomy: SkillTaxonomy) -> None:
        assert taxonomy.implications("nextjs") == {"react", "javascript"}

    def test_a_skill_does_not_imply_itself(self, taxonomy: SkillTaxonomy) -> None:
        assert "nextjs" not in taxonomy.implications("nextjs")

    def test_a_leaf_implies_nothing(self, taxonomy: SkillTaxonomy) -> None:
        assert taxonomy.implications("javascript") == frozenset()

    def test_implication_is_directed(self, taxonomy: SkillTaxonomy) -> None:
        # React is evidence for JavaScript. JavaScript is not evidence for React,
        # and reversing that would put a framework on a resume unasked.
        assert "javascript" in taxonomy.implications("react")
        assert "react" not in taxonomy.implications("javascript")


class TestDisplayForms:
    def test_a_job_descriptions_own_spelling_wins(self, taxonomy: SkillTaxonomy) -> None:
        # ATS keyword matching is literal: if the posting says "ReactJS", the resume
        # says "ReactJS" (RESUME_ENGINE §7).
        assert taxonomy.display_form("react", preferred="ReactJS") == "ReactJS"

    def test_our_spelling_is_used_when_the_posting_offers_none(self, taxonomy: SkillTaxonomy) -> None:
        assert taxonomy.display_form("react") == "React"

    def test_a_preferred_form_for_another_skill_is_ignored(self, taxonomy: SkillTaxonomy) -> None:
        assert taxonomy.display_form("react", preferred="Vue") == "React"


class TestTheFileIsValidatedAtLoadTime:
    def test_a_duplicate_canonical_id_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TaxonomyError, match="duplicate canonical id"):
            load_taxonomy(
                _write(
                    tmp_path,
                    """
version: "t"
skills:
  - {id: react, category: frontend}
  - {id: react, category: language}
""",
                )
            )

    def test_an_alias_claimed_by_two_skills_is_refused(self, tmp_path: Path) -> None:
        # Otherwise a requirement resolves to whichever skill loaded last.
        with pytest.raises(TaxonomyError, match="claimed by both"):
            load_taxonomy(
                _write(
                    tmp_path,
                    """
version: "t"
skills:
  - {id: react, category: frontend, aliases: [ui]}
  - {id: vue, category: frontend, aliases: [ui]}
""",
                )
            )

    def test_an_edge_to_a_missing_skill_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TaxonomyError, match="which is not a skill"):
            load_taxonomy(
                _write(
                    tmp_path,
                    """
version: "t"
skills:
  - {id: react, category: frontend, implies: [javascript]}
""",
                )
            )

    def test_a_cycle_is_refused(self, tmp_path: Path) -> None:
        # A cycle would make an implied match indistinguishable from a direct one.
        with pytest.raises(TaxonomyError, match="implication cycle"):
            load_taxonomy(
                _write(
                    tmp_path,
                    """
version: "t"
skills:
  - {id: a, category: language, implies: [b]}
  - {id: b, category: language, implies: [c]}
  - {id: c, category: language, implies: [a]}
""",
                )
            )

    def test_a_self_edge_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TaxonomyError, match="implies itself"):
            load_taxonomy(
                _write(tmp_path, 'version: "t"\nskills:\n  - {id: a, category: language, implies: [a]}\n')
            )

    def test_an_unknown_category_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TaxonomyError, match="expected one of"):
            load_taxonomy(_write(tmp_path, 'version: "t"\nskills:\n  - {id: a, category: wizardry}\n'))

    def test_an_uppercase_canonical_id_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TaxonomyError, match="lowercase"):
            load_taxonomy(_write(tmp_path, 'version: "t"\nskills:\n  - {id: React, category: frontend}\n'))

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(TaxonomyError, match="no taxonomy at"):
            load_taxonomy(tmp_path / "absent.yaml")

    def test_invalid_yaml_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(TaxonomyError, match="not valid YAML"):
            load_taxonomy(_write(tmp_path, "version: [unclosed\n"))

    def test_a_file_without_a_version_is_refused(self, tmp_path: Path) -> None:
        # The version is recorded in every resume's artifacts, so a resume can be
        # explained with the taxonomy that produced it (RESUME_ENGINE §9).
        with pytest.raises(TaxonomyError, match="'version' and 'skills'"):
            load_taxonomy(_write(tmp_path, "skills: []\n"))

    def test_an_empty_skill_list_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TaxonomyError, match="non-empty list"):
            load_taxonomy(_write(tmp_path, 'version: "t"\nskills: []\n'))


class TestTheShippedSeed:
    def test_it_loads(self) -> None:
        assert len(load_default_taxonomy()) > 200

    def test_it_is_cached_so_the_file_is_read_once(self) -> None:
        assert load_default_taxonomy() is load_default_taxonomy()

    @pytest.mark.parametrize(
        ("term", "expected"),
        [
            # The founder's stack, as a JD or a resume might spell it.
            ("React.js", "react"),
            ("Next.js 14", None),  # a version suffix is not an alias; resolved by the importer
            ("postgres", "postgresql"),
            ("Postgres", "postgresql"),
            ("k8s", "kubernetes"),
            ("FastAPI", "fastapi"),
            ("PyTorch", "pytorch"),
            ("YOLOv8", "yolo"),
            ("a11y", "accessibility"),
            ("WCAG", "accessibility"),
            ("CI/CD", "ci_cd"),
            ("RAG", "rag"),
            ("sentence transformers", "embeddings"),
            ("DeBERTa", "nli"),
            # Common in new-grad JDs, not in his stack: these must resolve so a gap
            # report names a skill instead of an unknown term.
            ("Spring Boot", "spring_boot"),
            ("Kafka", "kafka"),
            ("Terraform", "terraform"),
            ("Snowflake", "snowflake"),
            ("gRPC", "grpc"),
            ("C#", "csharp"),
            ("Golang", "golang"),
            ("Unreal Engine", "gaming"),
            ("HIPAA", "healthcare_it"),
        ],
    )
    def test_terms_that_have_to_resolve(self, term: str, expected: str | None) -> None:
        assert load_default_taxonomy().resolve(term) == expected

    def test_every_category_is_populated(self) -> None:
        taxonomy = load_default_taxonomy()
        present = {skill.category for skill in taxonomy}
        assert present == set(SkillCategory)

    def test_every_implication_target_exists_and_the_graph_is_shallow_enough_to_read(self) -> None:
        taxonomy = load_default_taxonomy()
        for skill in taxonomy:
            implications = taxonomy.implications(skill.canonical)
            assert all(target in taxonomy for target in implications)
            # Not a correctness rule, a readability one: a chain this long usually
            # means an edge is doing something other than "is evidence for".
            assert len(implications) < 12, f"{skill.canonical} implies {len(implications)} skills"

    def test_the_seed_file_is_where_the_loader_looks(self) -> None:
        assert DEFAULT_TAXONOMY_PATH.exists()
        assert DEFAULT_TAXONOMY_PATH.is_relative_to(Path(__file__).resolve().parents[3])

    def test_the_seed_is_plain_data_with_no_surprises(self) -> None:
        # Parsed with safe_load, and nothing in it should need anything else.
        raw = yaml.safe_load(DEFAULT_TAXONOMY_PATH.read_text(encoding="utf-8"))
        assert set(raw) == {"version", "skills"}
        assert all(
            set(entry) <= {"id", "display", "category", "aliases", "implies"} for entry in raw["skills"]
        )
