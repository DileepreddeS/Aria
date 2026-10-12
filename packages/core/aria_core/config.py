"""Configuration and secrets layout (SECURITY.md §10).

Every setting is typed and every secret is a ``SecretStr``, so a stray f-string or
log line prints ``**********`` instead of the value. Nothing has a usable default
that would let a misconfigured process start and do real work: missing settings
fail at start-up, not at the first request.

Local development reads ``.env`` (gitignored). Keys and the dev KMS root key live
in ``%APPDATA%\\aria``, outside the repository, so a stray ``git add -A`` cannot
pick them up. Staging and production read their environment from a secret manager
and are refused the dev KMS entirely.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "MIGRATION_ENV_FILE",
    "MIGRATION_ENV_VAR",
    "MigrationSettings",
    "Settings",
    "assert_no_migration_credentials",
    "get_settings",
]

Environment = Literal["dev", "ci", "staging", "prod"]


def _expand(path: str | Path) -> Path:
    """Expand ``%APPDATA%``-style and ``~`` paths, so .env can stay readable."""
    return Path(os.path.expandvars(str(path))).expanduser()


class Settings(BaseSettings):
    """Process configuration. Build it with :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="ARIA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Environment = "dev"

    # ---------------------------------------------------------------- database
    #: The API connects as aria_app and is subject to row-level security. There is
    #: deliberately no field for the migration role's DSN: aria_migrate owns the
    #: tables and bypasses the tenant policies through its maintenance policy, so
    #: its credentials must never be reachable from a process that serves requests.
    #: Alembic reads them through MigrationSettings instead, from a separate file.
    database_url: SecretStr

    # ------------------------------------------------------- uploads (SECURITY.md §7)
    #: Quarantine store for uploaded files, outside the repository and outside any
    #: web root. Files land here before they are scanned or parsed.
    upload_quarantine_path: Path = Field(default=Path(r"%APPDATA%\aria\quarantine"))
    #: clamd's address. Empty means no scanner, which the API refuses to serve with
    #: outside dev/ci (see aria_api.uploads.scanner.assert_scanner_is_allowed).
    clamav_host: str = ""
    clamav_port: int = Field(default=3310, ge=1, le=65535)
    #: SECURITY.md §7. Enforced while streaming, not after the file is in memory.
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    #: Whether .docx uploads are accepted. Off by default: a .docx is a zip of XML,
    #: which brings XXE, zip bombs and external relationships, and every tool that
    #: writes one also writes a PDF. The reader and its tests are kept — turning this
    #: on is a configuration change, not a code change — so the decision can be
    #: revisited at beta without rebuilding anything.
    allow_docx_uploads: bool = False
    #: Where the parse worker runs. "docker" is a container with no network, a
    #: read-only filesystem, no capabilities and a memory ceiling, because some
    #: extractors are native code reading hostile files. "process" is a plain local
    #: process, bounded but able to read this machine's files and reach its network,
    #: so it is refused outside dev/ci and is never the default.
    parser_isolation: Literal["docker", "process"] = "docker"
    #: The parser image. Built locally (./tasks.ps1 parser-image); the API checks at
    #: start-up that it was built from the sources in this checkout.
    parser_image: str = "aria-document-parser:local"

    # ------------------------------------------------- envelope encryption (S2)
    #: Dev-only KMS root key. Refused outside dev/ci by the validator below.
    dev_kms_key_path: Path = Field(default=Path(r"%APPDATA%\aria\dev-kms-root.key"))

    # ------------------------------------------------------------- llm gateway
    llm_gateway_url: str = "http://127.0.0.1:8081"
    llm_gateway_service_token: SecretStr | None = None
    #: The gateway is an internal service and must not be reachable from the
    #: network (SECURITY.md §2.5). Enforced, not documented.
    llm_gateway_bind_host: str = "127.0.0.1"
    llm_gateway_bind_port: int = Field(default=8081, ge=1024, le=65535)

    # ---------------------------------------------------------------------- api
    #: The API serves one person on their own machine in Phase 1, so a single bearer
    #: token is the proportionate answer. It resolves to a user id, which is what the
    #: audit log records: an unauthenticated API would log every approval as nobody.
    #: Required outside dev by the validator below.
    api_service_token: SecretStr | None = None
    #: The API holds S1 data and can approve facts. Loopback is enforced here rather
    #: than left to a deployment note (SECURITY.md §2.5).
    api_bind_host: str = "127.0.0.1"
    api_bind_port: int = Field(default=8080, ge=1024, le=65535)
    #: Who the token acts as. Set by `tasks.ps1 seed-me`; without it the API will
    #: start but refuse to serve, because an action with no actor cannot be audited.
    api_user_id: str = ""
    api_tenant_id: str = ""
    api_candidate_id: str = ""

    # -------------------------------------------------------- ApplyTask signing
    apply_task_signing_key_path: Path = Field(default=Path(r"%APPDATA%\aria\apply-task-signing.key"))
    apply_task_signing_kid: str = "dev-1"
    #: How long a dispatched task stays valid. Minutes, not hours (SECURITY.md §8).
    apply_task_ttl_seconds: int = Field(default=300, ge=30, le=1800)

    # ------------------------------------------------------------ observability
    otel_exporter: Literal["console", "none"] = "console"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator(
        "dev_kms_key_path", "apply_task_signing_key_path", "upload_quarantine_path", mode="before"
    )
    @classmethod
    def _expand_paths(cls, value: str | Path) -> Path:
        return _expand(value)

    @field_validator("api_bind_host")
    @classmethod
    def _api_bind_must_stay_on_loopback(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"api_bind_host must be a loopback IP address, got {value!r}") from exc
        if not address.is_loopback:
            raise ValueError(
                f"the API may only bind loopback addresses, got {value!r}. It holds S1 data and "
                "can approve facts; exposing it to the network is not a configuration choice."
            )
        return value

    @field_validator("llm_gateway_bind_host")
    @classmethod
    def _bind_must_stay_on_loopback(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"llm_gateway_bind_host must be a loopback IP address, got {value!r}") from exc
        if not address.is_loopback:
            raise ValueError(
                f"the LLM gateway may only bind loopback addresses, got {value!r}. "
                "It is an internal service and is not exposed (SECURITY.md §2.5)."
            )
        return value

    @model_validator(mode="after")
    def _production_refuses_development_shortcuts(self) -> Self:
        if self.env in ("dev", "ci"):
            return self
        problems: list[str] = []
        if self.llm_gateway_service_token is None:
            problems.append("llm_gateway_service_token is required outside dev")
        if self.api_service_token is None:
            problems.append("api_service_token is required outside dev")
        if self.parser_isolation != "docker":
            problems.append("uploads must be parsed in a container outside dev/ci (parser_isolation=docker)")
        if "dev-kms" in str(self.dev_kms_key_path):
            problems.append("the file-backed dev KMS must not be used outside dev/ci; use a managed KMS")
        if self.apply_task_signing_kid.startswith("dev-"):
            problems.append("apply_task_signing_kid still names a development key")
        if problems:
            raise ValueError(f"invalid configuration for env={self.env}: " + "; ".join(problems))
        return self

    @property
    def is_development(self) -> bool:
        return self.env in ("dev", "ci")

    def database_dsn(self) -> str:
        return self.database_url.get_secret_value()


MIGRATION_ENV_VAR = "ARIA_MIGRATE_DATABASE_URL"
MIGRATION_ENV_FILE = ".env.migrate"


class MigrationSettings(BaseSettings):
    """Alembic's configuration, read from its own file.

    Kept apart from :class:`Settings` so there is no field, no property and no
    accessor on the application's configuration that could hand out the migration
    role's credentials. A process that serves requests cannot reach them by
    accident, because the object it holds does not have them.
    """

    model_config = SettingsConfigDict(
        env_prefix="ARIA_",
        env_file=(".env", MIGRATION_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Environment = "dev"
    migrate_database_url: SecretStr

    def migrate_dsn(self) -> str:
        return self.migrate_database_url.get_secret_value()


def assert_no_migration_credentials(environment: Mapping[str, str] | None = None) -> None:
    """Refuse to start a request-serving process that can see aria_migrate's DSN.

    Checked against the real environment rather than against a settings object: the
    failure this prevents is a deployment handing the API the wrong secret, and by
    the time it reaches a typed field it is already in the process.

    Called by the API and the gateway at start-up.
    """
    environment = os.environ if environment is None else environment
    present = sorted(name for name in environment if name.upper() == MIGRATION_ENV_VAR)
    if present:
        raise RuntimeError(
            f"{', '.join(present)} is set in this process's environment. The migration role owns the "
            "tables and bypasses tenant isolation; a process that serves requests must never hold its "
            f"credentials. Keep them in {MIGRATION_ENV_FILE}, which only Alembic reads."
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process's settings, read once.

    Raises ``pydantic.ValidationError`` if anything required is missing, so a
    misconfigured process dies at start-up instead of halfway through a run.
    """
    return Settings()
