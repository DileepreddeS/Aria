"""Checks that run before the API serves anything.

A process that is about to accept requests states what it must not have. These are
cheap, and each one turns a configuration mistake into a failure at start-up rather
than a quiet loss of a security property under load.
"""

from __future__ import annotations

import structlog

from aria_api.uploads.scanner import assert_scanner_is_allowed, build_scanner
from aria_core.config import Settings, assert_no_migration_credentials
from aria_document_parser.image import image_status

__all__ = ["ParserIsolationUnavailable", "assert_parser_isolation", "run_preflight_checks"]

logger = structlog.get_logger(__name__)


class ParserIsolationUnavailable(RuntimeError):
    """Uploads cannot be parsed in the isolation this environment requires."""


def assert_parser_isolation(settings: Settings) -> None:
    """Refuse to serve unless uploads will be parsed where SECURITY.md §7 says.

    In a container, the image must exist and must have been built from the sources in
    this checkout: a stale image would parse every upload with code that is no longer
    here, and nothing at parse time would say so. A plain process is accepted only in
    dev/ci, where it is chosen explicitly; Settings refuses it anywhere else, and this
    repeats the check so the rule does not depend on one validator.
    """
    if settings.parser_isolation == "process":
        if not settings.is_development:
            raise ParserIsolationUnavailable(
                f"parser_isolation=process with env={settings.env}. Uploads from real users are "
                "parsed in a container (SECURITY.md §7)."
            )
        return
    status = image_status(settings.parser_image)
    if not status.current:
        raise ParserIsolationUnavailable(status.reason)


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

    assert_parser_isolation(settings)

    logger.info(
        "preflight_passed",
        env=settings.env,
        malware_scanner=scanner.name,
        parser_isolation=settings.parser_isolation,
        checks=[
            "no_migration_credentials",
            "unprivileged_database_role",
            "malware_scanner_allowed",
            "parser_isolation",
        ],
    )
