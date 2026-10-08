"""Architecture tests for encrypted columns (SECURITY.md §3).

Encryption is only as strong as the number of places that can bypass it. The
envelope cipher is correct and the repository uses it properly — and none of that
helps if a later feature adds ``select(SensitiveProfile.eeo_answers_ciphertext)``
somewhere else, or maps a new S2 field as ``String`` because that was quicker.

These tests read the markers on the columns themselves::

    info={"sensitivity": Sensitivity.S2, "accessor": "aria_core.db.sensitive_profile"}

and fail when the shape of the code stops matching them. They need no database.

Tests are exempt from the accessor rule: proving that a column holds no plaintext
means reading the column.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import LargeBinary

from aria_core.db.models import Base
from aria_core.sensitivity import Sensitivity

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Product source. Test directories are deliberately absent.
SOURCE_ROOTS = (
    REPO_ROOT / "packages" / "core" / "aria_core",
    REPO_ROOT / "services" / "llm_gateway" / "aria_llm_gateway",
    REPO_ROOT / "apps" / "api" / "aria_api",
)

#: Allowed to name an encrypted column regardless of its declared accessor:
#: the model that defines it, and the migrations that create it.
ALWAYS_ALLOWED = ("aria_core.db.models",)
MIGRATIONS = REPO_ROOT / "infra" / "migrations"


def _encrypted_columns() -> list[tuple[str, str, object, Sensitivity, str]]:
    """``(table, column, type, sensitivity, accessor)`` for every marked column."""
    found: list[tuple[str, str, object, Sensitivity, str]] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            sensitivity = column.info.get("sensitivity")
            if sensitivity is None:
                continue
            found.append(
                (
                    table.name,
                    column.name,
                    column.type,
                    sensitivity,
                    str(column.info.get("accessor", "")),
                )
            )
    return found


def _python_files() -> list[Path]:
    return [path for root in SOURCE_ROOTS if root.exists() for path in root.rglob("*.py")]


def _module_name(path: Path) -> str:
    """``packages/core/aria_core/db/models.py`` -> ``aria_core.db.models``."""
    for root in SOURCE_ROOTS:
        if path.is_relative_to(root):
            relative = path.relative_to(root.parent).with_suffix("")
            return ".".join(relative.parts)
    return path.stem


class TestEveryEncryptedColumnIsMarked:
    def test_there_are_marked_columns_to_check(self) -> None:
        # Guards against the rest of this file passing vacuously if the markers are
        # ever dropped.
        assert len(_encrypted_columns()) >= 3

    def test_a_ciphertext_column_without_a_marker_fails(self) -> None:
        marked = {(table, column) for table, column, _, _, _ in _encrypted_columns()}
        unmarked = [
            (table.name, column.name)
            for table in Base.metadata.tables.values()
            for column in table.columns
            if column.name.endswith("_ciphertext") and (table.name, column.name) not in marked
        ]
        assert not unmarked, (
            "these columns look like ciphertext but declare no sensitivity: "
            f"{unmarked}. Add info={{'sensitivity': ..., 'accessor': ...}}."
        )

    def test_sensitive_columns_are_stored_as_binary_not_as_a_plain_type(self) -> None:
        plain = [
            (table, column, type(column_type).__name__)
            for table, column, column_type, sensitivity, _ in _encrypted_columns()
            if sensitivity >= Sensitivity.S2 and not isinstance(column_type, LargeBinary)
        ]
        assert not plain, (
            "S2/S3 columns hold AES-GCM output and must be LargeBinary (bytea). "
            f"These are mapped as a plain type: {plain}"
        )

    def test_each_sensitive_column_names_the_module_allowed_to_touch_it(self) -> None:
        missing = [
            (table, column)
            for table, column, _, sensitivity, accessor in _encrypted_columns()
            if sensitivity >= Sensitivity.S2 and not accessor
        ]
        assert not missing, f"these columns declare no accessor module: {missing}"

    def test_the_declared_accessor_modules_exist(self) -> None:
        known = {_module_name(path) for path in _python_files()}
        for table, column, _, _, accessor in _encrypted_columns():
            assert accessor in known, f"{table}.{column} names a module that does not exist: {accessor}"


class TestNothingElseTouchesAnEncryptedColumn:
    def test_no_other_product_module_mentions_an_encrypted_column(self) -> None:
        """Plain text search, not an AST walk, on purpose.

        A raw SQL string like ``SELECT eeo_answers_ciphertext FROM ...`` bypasses
        the repository just as effectively as attribute access, and an AST walk
        would not see it. A mention in a comment also fails, which is the right
        amount of friction for a column like this.
        """
        violations: list[str] = []
        for table, column, _, sensitivity, accessor in _encrypted_columns():
            if sensitivity < Sensitivity.S2:
                continue
            allowed = {accessor, *ALWAYS_ALLOWED}
            for path in _python_files():
                module = _module_name(path)
                if module in allowed:
                    continue
                if column in path.read_text(encoding="utf-8"):
                    violations.append(f"{module} mentions {table}.{column} (accessor: {accessor})")

        assert not violations, (
            "an encrypted column may only be read or written through its accessor module, "
            "which is the one place that builds the AES-GCM associated data:\n  " + "\n  ".join(violations)
        )

    def test_the_rule_would_catch_a_violation(self) -> None:
        # The check above passes trivially if the search is broken, so prove the
        # search finds a column name where one really is.
        accessor_source = (
            REPO_ROOT / "packages" / "core" / "aria_core" / "db" / "sensitive_profile.py"
        ).read_text(encoding="utf-8")
        assert "work_authorization" in accessor_source

    @pytest.mark.parametrize("name", ["get_user_data", "get_all_candidate_data"])
    def test_no_broad_getter_exists(self, name: str) -> None:
        # SECURITY.md §4: narrow getters only. A single function returning
        # everything about a candidate is how S2 data reaches a prompt by accident.
        for path in _python_files():
            assert f"def {name}" not in path.read_text(encoding="utf-8"), (
                f"{path} defines {name}; tools are narrow getters (SECURITY.md §4)"
            )


class TestMigrationsAreTheOnlyOtherPlace:
    def test_migrations_create_the_columns_and_nothing_else_does(self) -> None:
        created_in: list[str] = []
        for path in MIGRATIONS.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(
                column in text
                for _, column, _, sensitivity, _ in _encrypted_columns()
                if sensitivity >= Sensitivity.S2
            ):
                created_in.append(path.name)
        assert created_in, "no migration creates the encrypted columns; has the schema drifted?"
