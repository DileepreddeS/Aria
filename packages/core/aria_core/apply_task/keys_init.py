"""``python -m aria_core.apply_task.keys_init`` — create the signing keypair.

Run once per machine through ``./tasks.ps1 keys-init``. The private key stays on the
API side; the public key is what the Runner is given, named by its ``kid`` so keys
can be rotated without a flag day.

Refuses to overwrite: replacing a signing key invalidates every task in flight and,
worse, every public key already distributed to a Runner.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from aria_core.apply_task.signing import generate_signing_keypair, private_key_pem, public_key_pem
from aria_core.config import get_settings


def write_keypair(private_path: Path, kid: str, *, repo_root: Path | None = None) -> tuple[Path, Path]:
    """Write the private key and a kid-indexed public keyring. Returns both paths."""
    if repo_root is not None and private_path.resolve().is_relative_to(repo_root.resolve()):
        raise ValueError(
            f"refusing to write a signing key inside the repository ({private_path}). "
            "Keys belong in %APPDATA%\aria."
        )
    public_path = private_path.with_suffix(".pub.json")
    for path in (private_path, public_path):
        if path.exists():
            raise FileExistsError(
                f"{path} already exists. Replacing a signing key invalidates every task in flight "
                "and every public key already given to a Runner."
            )

    private_key, public_key = generate_signing_keypair()
    private_path.parent.mkdir(parents=True, exist_ok=True)

    descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(private_key_pem(private_key))

    public_path.write_text(
        json.dumps({kid: public_key_pem(public_key).decode("ascii")}, indent=2),
        encoding="utf-8",
    )
    return private_path, public_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=None, help="private key path")
    parser.add_argument("--kid", default=None, help="key id the signature will name")
    arguments = parser.parse_args(argv)

    settings = get_settings()
    private_path, public_path = write_keypair(
        arguments.path or settings.apply_task_signing_key_path,
        arguments.kid or settings.apply_task_signing_kid,
        repo_root=Path.cwd(),
    )
    print(f"private key: {private_path}")
    print(f"public keyring (give this to the Runner): {public_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
