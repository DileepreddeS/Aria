"""Build the parser's container image, and tell whether the one present is current.

``python -m aria_document_parser.image build | check | fingerprint``

Never imported by the worker. It runs on the host, at build time and from the API's
start-up checks.

**The build context is assembled, not pointed at.** A fresh temporary directory gets
the Dockerfile, the parser's pinned and hashed requirements, and the two package
sources, and nothing else. The repository is never the context, so .env, private/ and
data/ cannot end up in an image whatever a .dockerignore says.

**An image is a snapshot, so it carries a fingerprint of what it was built from** —
the Dockerfile, uv.lock and every source file of aria_core and aria_document_parser —
as a label. The API recomputes the fingerprint at start-up and refuses to run when the
image's label differs: an image built before the last change to the parser would
otherwise parse every upload with code nobody is reading any more.

Requirements are exported from uv.lock with hashes and installed with
``--require-hashes --only-binary=:all:``, so the image gets exactly the locked
versions and no package runs a build script.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import aria_core
import aria_document_parser

__all__ = ["DEFAULT_IMAGE", "ImageStatus", "fingerprint", "image_status", "main"]

#: The local tag. ARIA_PARSER_IMAGE overrides it, as it does for the API.
DEFAULT_IMAGE = "aria-document-parser:local"
FINGERPRINT_LABEL = "org.aria.source-fingerprint"

_PACKAGES = (Path(aria_core.__file__).parent, Path(aria_document_parser.__file__).parent)
_DOCKERFILE = Path(aria_document_parser.__file__).parents[1] / "Dockerfile"


def _workspace_root() -> Path:
    for parent in Path(aria_document_parser.__file__).resolve().parents:
        if (parent / "uv.lock").is_file():
            return parent
    raise FileNotFoundError(
        "uv.lock not found above the parser package; the image is built from the workspace"
    )


def _sources() -> Iterator[tuple[str, Path]]:
    """Every file that goes into the image, as (name inside the image, path here)."""
    for package in _PACKAGES:
        for path in sorted(package.rglob("*.py")):
            yield f"site/{package.name}/{path.relative_to(package).as_posix()}", path


def fingerprint() -> str:
    """SHA-256 over the Dockerfile, uv.lock and every source file, by name and content."""
    digest = hashlib.sha256()
    inputs = [("Dockerfile", _DOCKERFILE), ("uv.lock", _workspace_root() / "uv.lock"), *_sources()]
    for name, path in inputs:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ImageStatus:
    current: bool
    #: Why not, in words that say what to run.
    reason: str = ""


def _executable(name: str) -> str:
    """The full path of a tool this module runs, so PATH order cannot substitute another."""
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(f"{name} is not installed or not on PATH")
    return found


def image_status(image: str) -> ImageStatus:
    """Whether ``image`` exists here and was built from the sources in this checkout."""
    rebuild = "Build it with ./tasks.ps1 parser-image (or make parser-image)."
    try:
        # Fixed arguments; the only variable is the image name from configuration.
        result = subprocess.run(  # noqa: S603
            [
                _executable("docker"),
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{FINGERPRINT_LABEL}" }}}}',
                image,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return ImageStatus(False, "docker is not installed or not on PATH, and parsing runs in a container.")
    except subprocess.TimeoutExpired:
        return ImageStatus(False, "docker did not answer within 30s; is Docker running?")
    if result.returncode != 0:
        return ImageStatus(False, f"the parser image {image} is not present. {rebuild}")
    if result.stdout.strip() != fingerprint():
        return ImageStatus(
            False,
            f"the parser image {image} was built from different sources than this checkout, so it "
            f"would parse uploads with code that is no longer here. {rebuild}",
        )
    return ImageStatus(True)


def _export_requirements(destination: Path) -> None:
    subprocess.run(  # noqa: S603  (fixed arguments)
        [
            _executable("uv"), "export", "--frozen", "--no-dev", "--no-emit-workspace",
            "--package", "aria-document-parser", "--format", "requirements-txt",
            "--output-file", str(destination),
        ],
        cwd=_workspace_root(),
        check=True,
        capture_output=True,
    )  # fmt: skip


def build(image: str) -> str:
    """Build ``image`` from an assembled context. Returns the fingerprint it carries."""
    stamp = fingerprint()
    with tempfile.TemporaryDirectory(prefix="aria-parser-image-") as directory:
        context = Path(directory)
        shutil.copyfile(_DOCKERFILE, context / "Dockerfile")
        _export_requirements(context / "requirements.txt")
        for name, path in _sources():
            target = context / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        subprocess.run(  # noqa: S603  (fixed arguments; the tag comes from configuration)
            [
                _executable("docker"),
                "build",
                "--label",
                f"{FINGERPRINT_LABEL}={stamp}",
                "--tag",
                image,
                str(context),
            ],
            check=True,
        )
    if stamp != fingerprint():
        raise RuntimeError("the sources changed while the image was building; build again")
    return stamp


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    image = os.environ.get("ARIA_PARSER_IMAGE", DEFAULT_IMAGE)
    command = arguments[0] if arguments else ""
    if command == "build":
        print(f"built {image} with fingerprint {build(image)}")
        return 0
    if command == "check":
        status = image_status(image)
        print(f"{image} is current" if status.current else status.reason)
        return 0 if status.current else 1
    if command == "fingerprint":
        print(fingerprint())
        return 0
    print("usage: python -m aria_document_parser.image build | check | fingerprint", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
