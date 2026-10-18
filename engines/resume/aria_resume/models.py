"""The local models the engine may load, as models.lock pins them, and where their files live.

No network code here: the engine opens files by path and checks them against the lock.
Downloading is ``infra/tools/fetch_models.py``'s job, run by ``./tasks.ps1 models``.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "LOCK_PATH",
    "ModelError",
    "ModelFile",
    "ModelSpec",
    "load_lock",
    "model_directory",
    "models_root",
    "verify",
]

LOCK_PATH = Path(__file__).with_name("models.lock")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


class ModelError(RuntimeError):
    """A model the lock names is missing, altered, or the lock itself is wrong."""


@dataclass(frozen=True, slots=True)
class ModelFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    repo: str
    commit: str
    licence: str
    files: tuple[ModelFile, ...]
    pooling: str = "cls"
    dimensions: int = 0
    max_tokens: int = 512
    query_prefix: str = ""

    def url(self, file: ModelFile) -> str:
        """The download URL: pinned to a commit, never a branch."""
        return f"https://huggingface.co/{self.repo}/resolve/{self.commit}/{file.path}"

    def file(self, path: str) -> ModelFile:
        return next(item for item in self.files if item.path == path)


def _spec(name: str, raw: Any) -> ModelSpec:
    if not isinstance(raw, dict):
        raise ModelError(f"models.lock: {name} must be a mapping")
    repo, commit = str(raw.get("repo", "")), str(raw.get("commit", ""))
    if not _REPO.match(repo):
        raise ModelError(f"models.lock: {name} has no valid repo")
    if not _COMMIT.match(commit):
        raise ModelError(f"models.lock: {name} must pin a 40-character commit, not {commit!r}")
    files = []
    for path, entry in (raw.get("files") or {}).items():
        sha = str((entry or {}).get("sha256", ""))
        if not _SHA256.match(sha):
            raise ModelError(f"models.lock: {name} {path} needs a SHA-256")
        if ".." in Path(path).parts or Path(path).is_absolute():
            raise ModelError(f"models.lock: {name} {path} must be a relative path inside the model")
        files.append(ModelFile(str(path), sha, int(entry["size"])))
    if not files:
        raise ModelError(f"models.lock: {name} names no files")
    pooling = str(raw.get("pooling", "cls"))
    if pooling not in ("cls", "mean"):
        raise ModelError(f"models.lock: {name} pooling must be cls or mean")
    return ModelSpec(
        name=name,
        repo=repo,
        commit=commit,
        licence=str(raw.get("licence", "")),
        files=tuple(files),
        pooling=pooling,
        dimensions=int(raw.get("dimensions", 0)),
        max_tokens=int(raw.get("max_tokens", 512)),
        query_prefix=str(raw.get("query_prefix", "")),
    )


def load_lock(path: Path = LOCK_PATH) -> dict[str, ModelSpec]:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
        raise ModelError("models.lock: expected a 'models' mapping")
    return {name: _spec(name, entry) for name, entry in raw["models"].items()}


def models_root() -> Path:
    """``ARIA_MODELS_DIR``, else %LOCALAPPDATA%\\aria\\models (machine-local, never roaming)."""
    override = os.environ.get("ARIA_MODELS_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "aria" / "models"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "aria" / "models"


def model_directory(spec: ModelSpec, root: Path | None = None) -> Path:
    return (root or models_root()) / f"{spec.repo.replace('/', '__')}@{spec.commit}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(spec: ModelSpec, directory: Path) -> None:
    """Every file present with its pinned size and SHA-256, or a ModelError saying what to run."""
    for file in spec.files:
        path = directory / file.path
        if not path.is_file():
            raise ModelError(f"{spec.name}: {file.path} is missing from {directory}; run ./tasks.ps1 models")
        if path.stat().st_size != file.size or _sha256(path) != file.sha256:
            raise ModelError(
                f"{spec.name}: {file.path} does not match models.lock; delete it and run ./tasks.ps1 models"
            )
