"""implied_skills.yaml: the implications a skills line may list, one reviewable list."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from aria_resume.validate.implied import IMPLIED_PATH, ImpliedSkillsError, load_implied_skills

from aria_core.taxonomy.model import SkillTaxonomy


def test_the_shipped_list_loads_against_the_shipped_taxonomy(taxonomy: SkillTaxonomy) -> None:
    implied = load_implied_skills(taxonomy)
    assert implied.version
    assert implied.pairs


@pytest.mark.parametrize(
    ("source", "target"),
    [("react", "javascript"), ("nextjs", "react"), ("postgresql", "sql"), ("kubernetes", "containers")],
)
def test_the_examples_the_rule_was_made_with_are_allowed(
    taxonomy: SkillTaxonomy, source: str, target: str
) -> None:
    assert target in load_implied_skills(taxonomy).pairs[source]


def test_rag_implies_nothing_listable(taxonomy: SkillTaxonomy) -> None:
    # RAG -> LLMs -> ... statistics: inferences about the candidate, not facts about RAG.
    assert "rag" not in load_implied_skills(taxonomy).pairs


def test_what_a_candidate_may_list(taxonomy: SkillTaxonomy) -> None:
    implied = load_implied_skills(taxonomy)
    assert implied.listable({"nextjs"}) == {"nextjs", "react"}
    # One step: Next.js -> React is listable, React -> JavaScript from Next.js is not.
    assert "javascript" not in implied.listable({"nextjs"})


def _write(tmp_path: Path, **changes: object) -> Path:
    data = yaml.safe_load(IMPLIED_PATH.read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / "implied_skills.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        # A real implication, but two steps away.
        ({"pairs": {"rag": ["statistics"]}}, "not a direct implication"),
        # Backwards: SQL does not imply PostgreSQL.
        ({"pairs": {"sql": ["postgresql"]}}, "not a direct implication"),
        ({"pairs": {"cobol": ["java"]}}, "not a skill"),
        ({"pairs": {"react": ["javascript", "javascript"]}}, "twice"),
        ({"pairs": {"react": []}}, "non-empty"),
        ({"version": ""}, "version"),
        ({"surprise": 1}, "exactly"),
    ],
)
def test_a_list_that_widens_the_taxonomy_is_refused(
    tmp_path: Path, taxonomy: SkillTaxonomy, changes: dict[str, object], says: str
) -> None:
    with pytest.raises(ImpliedSkillsError, match=says):
        load_implied_skills(taxonomy, _write(tmp_path, **changes))
