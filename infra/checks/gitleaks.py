"""Run gitleaks locally exactly as CI's ``secrets`` job does, from its pinned container image.

CI installs the gitleaks 8.30.1 release and checks its digest. Locally the same release
runs from the project's own image, pinned by digest, so no machine needs a gitleaks
binary on its PATH. Until this existed, ``./tasks.ps1 secrets`` and the pre-commit hook
called a binary no machine had, and CI's secrets job failed for a finding nobody had seen
locally (GitHub CI run #28).

    tree     the files CI's checkout would hold: everything git tracks or would track
             (``git ls-files -co --exclude-standard``), copied out first, so .env and
             private/ are never scanned and a local secret cannot fail the check
    history  every commit, as CI's "Scan the history" step does
    staged   what is about to be committed (the pre-commit hook)

The image's version must match ``GITLEAKS_VERSION`` in .github/workflows/ci.yml; a test
holds the two together. Needs Docker, which the database and the parse sandbox already do.

The Docker CLI has no timeout of its own, and pre-commit shows nothing until a hook ends, so
an engine that never answers once made the hook wait without end (DECISIONS 2026-11-07).
The engine is asked first, each scan has a time limit, and a check that cannot run exits 3
with the reason; a finding exits 1, as gitleaks does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: gitleaks 8.30.1, the release ci.yml installs, as published by the gitleaks project.
VERSION = "8.30.1"
IMAGE = (
    "ghcr.io/gitleaks/gitleaks:v8.30.1"
    "@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
)
#: The flags every scan uses, as CI passes them.
FLAGS = ["--redact", "--verbose", "--exit-code", "1"]

ROOT = Path(__file__).resolve().parents[2]

#: Seconds the engine has to answer ``docker version``; a healthy one answers in under one.
ENGINE_TIMEOUT = 20
#: Seconds one scan may take, a first pull of the image included. Here the tree takes about
#: 11s, the history under a minute and the staged changes under one second.
SCAN_TIMEOUT = 300
#: The exit status of a check that could not run, as against a finding (1).
CANNOT_RUN = 3


class CannotRun(Exception):
    """The scan could not run; the message says why and what to do."""


def _check_engine() -> None:
    command = ["docker", "version", "--format", "{{.Server.Version}}"]
    try:
        answer = subprocess.run(  # noqa: S603  (fixed arguments)
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=ENGINE_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise CannotRun("docker is not on PATH: the scan runs in Docker (Docker Desktop)") from None
    except subprocess.TimeoutExpired:
        raise CannotRun(
            f"Docker's engine did not answer `docker version` in {ENGINE_TIMEOUT}s. Docker Desktop"
            " can show its engine as running while its Windows side no longer answers: quit and"
            " restart Docker Desktop (if that is not enough, `wsl --shutdown` in between), then"
            " rerun."
        ) from None
    if answer.returncode != 0:
        raise CannotRun(f"Docker's engine is not running: {answer.stderr.strip()}")


def _index_inside() -> list[str]:
    """The index git is committing, as the container sees it.

    `git commit <paths>` commits a temporary index and a plain commit its locked one, and git
    names either in GIT_INDEX_FILE (relative to the top of the work tree) for the hooks.
    Unpassed, the container would read .git/index, which is not what is being committed.
    """
    index = os.environ.get("GIT_INDEX_FILE")
    if not index:
        return []
    path = Path(index).resolve()
    try:
        inside = path.relative_to(ROOT)
    except ValueError:
        raise CannotRun(f"git's index {path} is outside {ROOT}, so the container cannot read it") from None
    return ["-e", f"GIT_INDEX_FILE=/scan/{inside.as_posix()}"]


def _docker(mount: Path, *args: str, env: tuple[str, ...] = ()) -> int:
    # Named, so that a scan stopped from here can be removed: --rm removes a container only
    # when it exits, and killing the CLI leaves it running in the engine.
    name = f"aria-gitleaks-{os.getpid()}-{args[0]}"
    command = ["docker", "run", "--rm", "--name", name, *env, "-v", f"{mount}:/scan:ro", IMAGE, *args]
    try:
        # Fixed arguments: constants, this repository's path, and an index inside it.
        return subprocess.run(  # noqa: S603
            command, stdin=subprocess.DEVNULL, timeout=SCAN_TIMEOUT, check=False
        ).returncode
    except subprocess.TimeoutExpired:
        _remove(name)
        raise CannotRun(f"gitleaks did not finish in {SCAN_TIMEOUT}s; its container is removed") from None
    except KeyboardInterrupt:
        _remove(name)
        raise


def _remove(name: str) -> None:
    try:
        subprocess.run(  # noqa: S603  (fixed arguments)
            ["docker", "rm", "--force", name],  # noqa: S607  (docker is expected on PATH)
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=ENGINE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            f"could not remove the container {name}; `docker rm -f {name}` once Docker answers",
            file=sys.stderr,
        )


def _visible_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "-co", "--exclude-standard"],  # noqa: S607  (git is expected on PATH)
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return [name for name in listed.split("\0") if name]


def scan_tree() -> int:
    with tempfile.TemporaryDirectory(prefix="aria-gitleaks-") as temporary:
        copy = Path(temporary)
        for name in _visible_files():
            source = ROOT / name
            # A tracked file deleted in the working tree is not in a checkout of it either.
            if source.is_file():
                target = copy / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        return _docker(copy, "dir", "/scan", *FLAGS)


def scan_history() -> int:
    return _docker(ROOT, "git", "/scan", *FLAGS)


def scan_staged() -> int:
    return _docker(ROOT, "git", "/scan", "--pre-commit", "--staged", *FLAGS, env=tuple(_index_inside()))


MODES = {"tree": scan_tree, "history": scan_history, "staged": scan_staged}


def main(argv: list[str]) -> int:
    modes = argv or ["tree", "history"]
    unknown = [mode for mode in modes if mode not in MODES]
    if unknown:
        print(f"unknown mode(s) {unknown}; expected any of {sorted(MODES)}", file=sys.stderr)
        return 2
    status = 0
    try:
        _check_engine()
        for mode in modes:
            print(f"gitleaks {VERSION}: {mode}", flush=True)
            status |= MODES[mode]()
    except CannotRun as reason:
        print(f"gitleaks could not run: {reason}", file=sys.stderr)
        return CANNOT_RUN
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
