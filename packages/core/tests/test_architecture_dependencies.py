"""Each workspace member imports only the first-party packages it declares.

Inside the uv workspace every member is installed, so an undeclared import works on
every developer machine and in CI and fails only when the package is installed on its
own. That is how aria_cli came to import aria_api for one header name while declaring
only aria-core: nothing noticed, because nothing ever installs the CLI by itself. This
test reads each member's pyproject.toml and its product code's imports, so the
mismatch is a test failure rather than a support request.

Test code is exempt: a member's tests may use a sibling to build fixtures, and tests
are never installed.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _members() -> list[Path]:
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [REPO_ROOT / member for member in root["tool"]["uv"]["workspace"]["members"]]


def _import_name(distribution: str) -> str:
    """aria-document-parser -> aria_document_parser. First-party names follow this rule."""
    return distribution.replace("-", "_")


def _project(member: Path) -> tuple[str, list[str]]:
    """``(name, dependencies)`` from a member's pyproject.toml."""
    project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return str(project["name"]), [str(requirement) for requirement in project.get("dependencies", [])]


def _first_party_packages() -> set[str]:
    return {_import_name(_project(member)[0]) for member in _members()}


def _declared(member: Path) -> set[str]:
    declared: set[str] = set()
    for requirement in _project(member)[1]:
        name = requirement.split(";")[0]
        for stop in "<>=!~[ ":
            name = name.split(stop)[0]
        declared.add(_import_name(name.strip().lower()))
    return declared


def _imports(package_dir: Path) -> dict[str, list[str]]:
    """Top-level first-party imports per file, as 'file:line'."""
    first_party = _first_party_packages()
    found: dict[str, list[str]] = {}
    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in first_party:
                    where = f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
                    found.setdefault(top, []).append(where)
    return found


@pytest.mark.parametrize("member", _members(), ids=lambda member: member.relative_to(REPO_ROOT).as_posix())
def test_member_imports_only_what_it_declares(member: Path) -> None:
    own = _import_name(_project(member)[0])
    package_dir = member / own
    assert package_dir.is_dir(), f"{member} has no {own}/ package directory"

    undeclared = {
        top: places
        for top, places in _imports(package_dir).items()
        if top != own and top not in _declared(member)
    }
    assert not undeclared, (
        f"{own} imports first-party packages its pyproject.toml does not declare \u2014 this works "
        f"inside the workspace and breaks when {own} is installed alone: "
        + "; ".join(f"{top} at {', '.join(places)}" for top, places in sorted(undeclared.items()))
    )


def test_every_member_is_checked() -> None:
    """The parametrisation is only as good as the member list it reads."""
    names = {member.relative_to(REPO_ROOT).as_posix() for member in _members()}
    assert {"packages/core", "apps/api", "apps/cli", "services/document_parser"} <= names


#: Model-provider SDKs: only the gateway may hold one (CLAUDE.md; DECISIONS 2026-10-20).
PROVIDER_SDKS = frozenset({"anthropic", "openai", "litellm"})


def test_only_the_gateway_uses_a_model_provider_sdk() -> None:
    """Tests included: a test elsewhere that calls a provider is a call from outside the gateway."""
    gateway = REPO_ROOT / "services" / "llm_gateway"
    places: list[str] = []
    for member in _members():
        if member == gateway:
            continue
        places += [f"{member.name}/pyproject.toml" for sdk in PROVIDER_SDKS & _declared(member)]
    roots = [member for member in _members() if member != gateway] + [
        REPO_ROOT / "evals",
        REPO_ROOT / "infra",
    ]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                else:
                    continue
                if any(name.split(".")[0] in PROVIDER_SDKS for name in names):
                    places.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")
    assert not places, f"only services/llm_gateway may use a model provider's SDK: {places}"
