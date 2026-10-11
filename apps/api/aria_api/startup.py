"""Checks that run before the API serves anything.

A process that is about to accept requests states what it must not have. These are
cheap, and each one turns a configuration mistake into a failure at start-up rather
than a quiet loss of a security property under load.
"""

from __future__ import annotations

import structlog

from aria_api.uploads.scanner import assert_scanner_is_allowed, build_scanner
from aria_core.config import Settings, assert_no_migration_credentials

__all__ = ["run_preflight_checks"]

logger = structlog.get_logger(__name__)


def run_preflight_checks(settings: Settings) -> None:
    """Refuse to serve if the process is configured in a way that breaks a boundary.

    Raises ``RuntimeError``. Nothing here is recoverable: the fix is the deployment.
    """
    # aria_migrate owns the tables and is not subject to tenant isolation. The API
    # must not be able to reach those credentials even in principle.
    assert_no_migration_credentials()

    dsn = settings.database_dsn()
    if "aria_migrate" in dsn or "aria_owner" in dsn:
        raise RuntimeError(
            "the API's database URL names a privileged role. It connects as aria_app, which owns "
            "nothing and is subject to row-level security (SECURITY.md §2.4)."
        )

    # A scanner that does not scan may only be used in dev/ci. Without this, a
    # staging deployment that forgot to configure clamd would accept real files from
    # real people and record them as unscanned — true, and unnoticed until someone
    # read the column (SECURITY.md §7).
    scanner = build_scanner(settings)
    assert_scanner_is_allowed(scanner, settings)

    logger.info(
        "preflight_passed",
        env=settings.env,
        malware_scanner=scanner.name,
        checks=[
            "no_migration_credentials",
            "unprivileged_database_role",
            "malware_scanner_allowed",
        ],
    )
