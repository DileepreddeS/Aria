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
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

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
    #: The API connects as aria_app and is subject to row-level security.
    database_url: SecretStr
    #: Alembic connects as aria_migrate, which owns the tables.
    migrate_database_url: SecretStr | None = None

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

    # -------------------------------------------------------- ApplyTask signing
    apply_task_signing_key_path: Path = Field(default=Path(r"%APPDATA%\aria\apply-task-signing.key"))
    apply_task_signing_kid: str = "dev-1"
    #: How long a dispatched task stays valid. Minutes, not hours (SECURITY.md §8).
    apply_task_ttl_seconds: int = Field(default=300, ge=30, le=1800)

    # ------------------------------------------------------------ observability
    otel_exporter: Literal["console", "none"] = "console"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("dev_kms_key_path", "apply_task_signing_key_path", mode="before")
    @classmethod
    def _expand_paths(cls, value: str | Path) -> Path:
        return _expand(value)

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

    def migrate_dsn(self) -> str:
        """Alembic's DSN, falling back to the app DSN only in development."""
        if self.migrate_database_url is not None:
            return self.migrate_database_url.get_secret_value()
        if not self.is_development:
            raise ValueError("migrate_database_url is required outside dev/ci")
        return self.database_dsn()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process's settings, read once.

    Raises ``pydantic.ValidationError`` if anything required is missing, so a
    misconfigured process dies at start-up instead of halfway through a run.
    """
    return Settings()
