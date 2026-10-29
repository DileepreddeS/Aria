"""Download the model files models.lock pins, and nothing else (docs/vetting/2026-10-18-ml-group.md).

The only code in the repository that fetches model weights. Each file comes from
``https://huggingface.co/<repo>/resolve/<commit>/<path>`` (a commit, never a branch), is
written to a temporary file beside its destination, and is moved into place only if its size
and SHA-256 match the lock; otherwise it is deleted and the run fails. A file already in place
and matching is left alone.

Run through ``./tasks.ps1 models`` or ``make models``. ``--only embedding`` limits the run to
one model.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import urllib.request
from pathlib import Path

from aria_resume.models import ModelError, ModelSpec, load_lock, model_directory, models_root, verify

_CHUNK = 1 << 20


def fetch(spec: ModelSpec, root: Path) -> None:
    directory = model_directory(spec, root)
    try:
        verify(spec, directory)
        print(f"{spec.name}: {spec.repo}@{spec.commit[:12]} already in place and verified")
        return
    except ModelError:
        pass
    for file in spec.files:
        target = directory / file.path
        if target.is_file() and target.stat().st_size == file.size:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest == file.sha256:
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        url = spec.url(file)
        if not url.startswith("https://huggingface.co/"):
            raise ModelError(f"{spec.name}: refusing {url}: models come from https://huggingface.co only")
        print(f"{spec.name}: fetching {file.path} ({file.size / 1e6:.0f} MB) from {url}")
        digest_builder = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, suffix=".part") as handle:
            temporary = Path(handle.name)
            request = urllib.request.Request(url, headers={"User-Agent": "aria-models"})  # noqa: S310  (https, checked above)
            try:
                # https://huggingface.co/ only: any other URL is refused above.
                # nosemgrep: dynamic-urllib-use-detected
                with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310  (https, a fixed host)
                    for chunk in iter(lambda: response.read(_CHUNK), b""):
                        handle.write(chunk)
                        digest_builder.update(chunk)
                        size += len(chunk)
            except Exception:
                handle.close()
                temporary.unlink(missing_ok=True)
                raise
        if size != file.size or digest_builder.hexdigest() != file.sha256:
            temporary.unlink(missing_ok=True)
            raise ModelError(f"{spec.name}: {file.path} did not match models.lock; deleted, nothing kept")
        temporary.replace(target)
    verify(spec, directory)
    print(f"{spec.name}: {spec.repo}@{spec.commit[:12]} verified in {directory}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="fetch one model from the lock, by name")
    args = parser.parse_args(argv)
    specs = load_lock()
    if args.only:
        if args.only not in specs:
            print(f"no model named {args.only!r} in models.lock", file=sys.stderr)
            return 2
        specs = {args.only: specs[args.only]}
    root = models_root()
    for spec in specs.values():
        try:
            fetch(spec, root)
        except ModelError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
