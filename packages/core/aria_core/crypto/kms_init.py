"""``python -m aria_core.crypto.kms_init`` — create the development KMS root key.

Run once per machine, through ``./tasks.ps1 kms-init``. The key is written to
``%APPDATA%\\aria`` and the command refuses to overwrite an existing one, because
replacing a root key makes everything encrypted under it unreadable.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aria_core.config import get_settings
from aria_core.crypto.kms import generate_root_key_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="where to write the key (default: ARIA_DEV_KMS_KEY_PATH)",
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()
    if not settings.is_development:
        parser.error(f"the file-backed dev KMS is for dev and ci only, not env={settings.env}")

    path = arguments.path or settings.dev_kms_key_path
    written = generate_root_key_file(path, repo_root=Path.cwd())
    print(f"wrote a 32-byte root key to {written}")
    print("Back it up outside the repository. Losing it loses every S2 value encrypted under it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
