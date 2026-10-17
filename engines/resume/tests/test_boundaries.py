"""The engine stays pure: no database, no app, no model provider, no network client.

RESUME_ENGINE: "a separate package with no knowledge of crawling, browsers or applying",
and DECISIONS 2026-10-16: it never reads the database, so the only candidate data it can
use is the approved-only snapshot it is handed. CLAUDE.md: only services/llm_gateway calls
a model provider. Each of those is an import this test refuses.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "aria_resume"

#: Module prefixes the engine's product code may not import, and why.
FORBIDDEN: dict[str, str] = {
    "sqlalchemy": "database code",
    "asyncpg": "database code",
    "alembic": "database code",
    "aria_core.db": "database code: the engine gets a snapshot, never a session",
    "aria_core.graph.facts": "a database read path; the snapshot is the engine's input",
    "aria_core.graph.entities": "a database read path; the snapshot is the engine's input",
    "aria_core.graph.questions": "a database read path; the snapshot is the engine's input",
    "aria_core.graph.derive": "a database read path; the snapshot carries what it derives",
    "aria_core.graph.snapshot": "the snapshot's builder reads the database; the engine is handed its result",
    "aria_core.importer.apply": "the importer's writer",
    "aria_api": "an app",
    "aria_cli": "an app",
    "aria_llm_gateway": "the gateway is reached through LlmClient, not imported",
    "anthropic": "a model provider: only the gateway calls one",
    "openai": "a model provider: only the gateway calls one",
    "litellm": "a model provider: only the gateway calls one",
    "httpx": "a network client: the engine makes no requests of its own",
    "requests": "a network client: the engine makes no requests of its own",
    "aiohttp": "a network client: the engine makes no requests of its own",
}

#: The parser is imported only for its geometric reader, which the ATS check (6a) uses to
#: read our own printed PDF back. Its worker, client and container code stay out.
PARSER_ALLOWED = ("aria_document_parser.pdf", "aria_document_parser.errors")


def _imports(source: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


def _violations(source: str, name: str) -> list[str]:
    return [
        f"{name}:{line} imports {module} ({why})"
        for module, line in _imports(source)
        for prefix, why in FORBIDDEN.items()
        if module == prefix or module.startswith(prefix + ".")
    ] + [
        f"{name}:{line} imports {module} (the parser beyond its geometric reader)"
        for module, line in _imports(source)
        if module.split(".")[0] == "aria_document_parser" and module not in PARSER_ALLOWED
    ]


def test_there_is_product_code_to_check() -> None:
    assert list(PACKAGE.rglob("*.py"))


@pytest.mark.parametrize("path", sorted(PACKAGE.rglob("*.py")), ids=lambda path: path.name)
def test_the_engine_imports_nothing_that_breaks_its_boundary(path: Path) -> None:
    assert _violations(path.read_text(encoding="utf-8"), path.relative_to(PACKAGE.parent).as_posix()) == []


def test_the_rule_would_catch_a_violation() -> None:
    # Proves the matcher finds what it looks for, so the passing runs above mean something.
    probe = "\n".join(
        [
            "from aria_core.db.models import Fact",
            "import httpx",
            "import anthropic.types",
            "import aria_core.schemas",
        ]
    )
    assert len(_violations(probe, "probe.py")) == 3


def test_loading_the_whole_engine_loads_no_database_code() -> None:
    # The import rules above see only the engine's own imports. A package __init__ that
    # re-exports a database module would pass them and still load SQLAlchemy; this runs
    # in a fresh interpreter and looks at what actually got loaded.
    modules = sorted(
        "aria_resume." + ".".join(path.relative_to(PACKAGE).with_suffix("").parts)
        for path in PACKAGE.rglob("*.py")
        if path.name != "__init__.py"
    )
    probe = (
        "import importlib, sys\n"
        f"for name in {modules!r}: importlib.import_module(name)\n"
        "prefixes = ('sqlalchemy', 'asyncpg', 'alembic', 'aria_core.db')\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(prefixes))\n"
        "print(','.join(loaded))\n"
    )
    # The interpreter running these tests, and a probe built from this repo's module names.
    result = subprocess.run(  # noqa: S603  (fixed arguments)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
