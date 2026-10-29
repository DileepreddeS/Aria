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
"""

from __future__ import annotations

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


def _docker(mount: Path, *args: str) -> int:
    command = ["docker", "run", "--rm", "-v", f"{mount}:/scan:ro", IMAGE, *args]
    return subprocess.run(command, check=False).returncode  # noqa: S603  (fixed arguments)


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
    return _docker(ROOT, "git", "/scan", "--pre-commit", "--staged", *FLAGS)


MODES = {"tree": scan_tree, "history": scan_history, "staged": scan_staged}


def main(argv: list[str]) -> int:
    modes = argv or ["tree", "history"]
    unknown = [mode for mode in modes if mode not in MODES]
    if unknown:
        print(f"unknown mode(s) {unknown}; expected any of {sorted(MODES)}", file=sys.stderr)
        return 2
    status = 0
    for mode in modes:
        print(f"gitleaks {VERSION}: {mode}", flush=True)
        status |= MODES[mode]()
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
