"""``python -m aria_core.audit.record_anchors`` — anchor every chain's head.

Intended to run daily (``./tasks.ps1 anchor`` locally; a scheduled job in Phase 6).
Each run appends one record per non-empty chain to the anchor store, so a later
consistent rewrite of the database can be detected against something the database
does not control. See :mod:`aria_core.audit.anchor`.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from aria_core.audit.anchor import FileAnchorStore, record_all_heads
from aria_core.config import get_settings
from aria_core.db.session import create_engine


async def _run(path: Path) -> int:
    settings = get_settings()
    engine = create_engine(settings.database_dsn())
    try:
        records = await record_all_heads(engine, FileAnchorStore(path))
    finally:
        await engine.dispose()

    for record in records:
        print(f"{record.chain_id} seq={record.seq} head={record.head_hash[:12]}")
    print(f"anchored {len(records)} chain(s) to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        required=True,
        help="append-only anchor log, outside the repository and outside the database",
    )
    return asyncio.run(_run(parser.parse_args(argv).path))


if __name__ == "__main__":
    raise SystemExit(main())
