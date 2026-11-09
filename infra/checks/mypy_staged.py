"""Type-check what is being committed, as ``make typecheck`` checks a checkout of it.

The hook used to run mypy on the working tree. pre-commit stashes unstaged changes to tracked
files but leaves untracked files where they are, so mypy saw the index's tracked files beside
files that were not being committed: an untracked router that imported code the index did not
yet hold failed three commits in a row, and they stayed staged until one passed and all were
committed as one (DECISIONS 2026-11-09).

So the index is exported, alone, to a directory of its own, and each workspace member is
checked there exactly as the Makefile checks it. The export lives in the git directory at a
fixed path, so mypy's cache there stays warm from one commit to the next. `git commit <paths>`
commits a temporary index, which git names in GIT_INDEX_FILE; git reads it from there itself.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The members ``make typecheck`` checks, one invocation each; a test holds the two together.
MEMBERS = (
    "packages/core",
    "services/llm_gateway",
    "services/document_parser",
    "apps/api",
    "apps/cli",
    "engines/resume",
    "evals",
)

#: What mypy reads: sources, stubs, the markers of typed packages and the configuration.
_READ_BY_MYPY = (".py", ".pyi")
_READ_BY_NAME = ("py.typed", "pyproject.toml")


def _git(*args: str, stdin: bytes | None = None, cwd: Path = ROOT) -> bytes:
    # git with this module's own arguments, run in this repository or a test's.
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607  (git is expected on PATH)
        cwd=cwd,
        input=stdin,
        check=True,
        capture_output=True,
    ).stdout


def _read_by_mypy(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return name.endswith(_READ_BY_MYPY) or base in _READ_BY_NAME


def export_index(root: Path, tree: Path) -> list[str]:
    """Write the index's copy of every file mypy reads under ``tree``, and nothing else."""
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    listed = _git("ls-files", "-z", "--cached", cwd=root).decode("utf-8")
    names = [name for name in listed.split("\0") if name and _read_by_mypy(name)]
    prefix = tree.resolve().as_posix().rstrip("/") + "/"
    _git("checkout-index", "-z", "--stdin", f"--prefix={prefix}", stdin="\0".join(names).encode(), cwd=root)
    return names


def main() -> int:
    place = ROOT / _git("rev-parse", "--git-path", "aria-mypy-staged").decode("utf-8").strip()
    tree, cache = place / "tree", place / "cache"
    export_index(ROOT, tree)
    failed = []
    for member in MEMBERS:
        print(f"mypy {member} (as committed)", flush=True)
        # Fixed arguments: this interpreter, constants, and directories inside the git directory.
        status = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "mypy", "--cache-dir", str(cache), member], cwd=tree, check=False
        ).returncode
        if status != 0:
            failed.append(member)
    if failed:
        print(f"mypy failed for {', '.join(failed)}, as the index holds them", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
