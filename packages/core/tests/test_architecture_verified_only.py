"""Only approved facts can reach a resume or an answer (RESUME_ENGINE §5).

The same idea as `test_architecture_s2.py`: a rule that only lives in a reviewer's
head is a rule that holds until someone is in a hurry. The fact repository filters on
status; this test fails if any other module queries the fact tables at all, so a new
feature that wants facts has to go through the one function that filters them.

Without this, the realistic failure is not malice. It is a Phase 2 composer doing
`select(Fact).where(Fact.candidate_id == ...)` because that was the obvious thing to
write, and quietly putting an unreviewed imported bullet on a resume.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

SOURCE_ROOTS = (
    REPO_ROOT / "packages" / "core" / "aria_core",
    REPO_ROOT / "services",
    REPO_ROOT / "apps",
    REPO_ROOT / "engines",
)

#: The repository that owns fact reads, and the models that define the tables.
FACT_REPOSITORY = "aria_core.graph.facts"
MODELS_PREFIX = "aria_core.db.models"

#: ORM classes whose rows carry a fact's claim. Reading any of them outside the
#: repository means reading a claim without its approval status.
FACT_TABLE_CLASSES = ("Fact", "FactRevision")

#: The functions the rest of the codebase may use to read facts. Named here so the
#: list is visible in review: adding one is a deliberate act.
APPROVED_READ_PATHS = (
    "approved_facts",
    "draft_facts",
    "rejected_facts",
    "get_fact",
    "facts_for_deduplication",
    "fact_skill_canonicals",
    "draft_facts_from_upload",
    "fact_ids_under",
    "fact_status_counts",
    "approved_fact_evidence",
)

#: Read paths that filter on status, and must say so in their body. The others are
#: deliberately different and are checked by name instead: get_fact shows the approval
#: flow one fact whatever its state, facts_for_deduplication returns the status with
#: every row so the importer can treat the four states differently, and
#: fact_skill_canonicals, fact_ids_under and fact_status_counts return ids or counts and no text.
STATUS_FILTERED_READ_PATHS = (
    "approved_facts",
    "draft_facts",
    "rejected_facts",
    "draft_facts_from_upload",
    "approved_fact_evidence",
)


def _python_files() -> list[Path]:
    return [
        path
        for root in SOURCE_ROOTS
        if root.exists()
        for path in root.rglob("*.py")
        if "tests" not in path.parts
    ]


def _module_name(path: Path) -> str:
    for root in SOURCE_ROOTS:
        if path.is_relative_to(root):
            if root.name == "aria_core":
                return ".".join(path.relative_to(root.parent).with_suffix("").parts)
            # services/<name>/<package>/... and apps/<name>/<package>/...
            parts = path.relative_to(root).with_suffix("").parts
            return ".".join(parts[1:]) if len(parts) > 1 else parts[0]
    return path.stem


class TestOnlyTheRepositoryReadsFacts:
    def test_there_are_files_to_check(self) -> None:
        # Guards against the rest of this file passing because the glob found nothing.
        assert len(_python_files()) > 20

    def test_no_other_module_references_the_fact_tables(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            module = _module_name(path)
            if module == FACT_REPOSITORY or module.startswith(MODELS_PREFIX):
                continue

            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # `from aria_core.db.models import Fact`
                if isinstance(node, ast.ImportFrom) and node.module == MODELS_PREFIX:
                    for alias in node.names:
                        if alias.name in FACT_TABLE_CLASSES:
                            violations.append(f"{module} imports {alias.name} (line {node.lineno})")
                # `models.Fact` / `Fact.candidate_id`
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id in FACT_TABLE_CLASSES:
                        violations.append(f"{module} uses {node.value.id}.{node.attr} (line {node.lineno})")

        assert not violations, (
            "facts may only be read through aria_core.graph.facts, which filters on approval "
            "status \u2014 otherwise an unreviewed imported bullet can reach a resume:\n  "
            + "\n  ".join(violations)
        )

    def test_no_raw_sql_names_the_fact_tables(self) -> None:
        # An f-string of SQL bypasses the ORM and the AST check above.
        violations: list[str] = []
        for path in _python_files():
            module = _module_name(path)
            if module == FACT_REPOSITORY or module.startswith(MODELS_PREFIX):
                continue
            text = path.read_text(encoding="utf-8").lower()
            for table in ("from facts", "join facts", "into facts", "from fact_revisions"):
                if table in text:
                    violations.append(f"{module} contains {table!r}")
        assert not violations, "raw SQL against the fact tables bypasses the status filter: " + str(
            violations
        )


class TestTheRepositoryItselfFilters:
    def test_every_read_function_filters_on_status(self) -> None:
        source = (REPO_ROOT / "packages" / "core" / "aria_core" / "graph" / "facts.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        read_functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name in APPROVED_READ_PATHS
        }
        assert set(read_functions) == set(APPROVED_READ_PATHS), (
            f"expected read paths {APPROVED_READ_PATHS}, found {sorted(read_functions)}"
        )

        for name in STATUS_FILTERED_READ_PATHS:
            body = ast.unparse(read_functions[name])
            assert "ApprovalStatus." in body, f"{name} does not name a status"

    def test_approved_facts_asks_for_approved(self) -> None:
        source = (REPO_ROOT / "packages" / "core" / "aria_core" / "graph" / "facts.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "approved_facts"
        )
        assert "ApprovalStatus.APPROVED" in ast.unparse(function)

    @pytest.mark.parametrize("forbidden", ["status=", "approve=", "approved_by="])
    def test_create_draft_fact_cannot_be_asked_to_approve(self, forbidden: str) -> None:
        # If this grew a way to create an approved fact, an importer could verify its
        # own guesses and the whole lifecycle would be decoration.
        source = (REPO_ROOT / "packages" / "core" / "aria_core" / "graph" / "facts.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_draft_fact"
        )
        parameters = {argument.arg for argument in function.args.kwonlyargs + function.args.args}
        assert forbidden.rstrip("=") not in parameters

    def test_the_rule_would_catch_a_violation(self) -> None:
        # Prove the AST matcher finds what it is looking for, so a passing run above
        # means something.
        tree = ast.parse("from aria_core.db.models import Fact\nx = Fact.candidate_id\n")
        imported = [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names
        ]
        attributes = [
            node.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        ]
        assert "Fact" in imported
        assert "Fact" in attributes
