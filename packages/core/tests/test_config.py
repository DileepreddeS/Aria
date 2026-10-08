"""Configuration fails fast and refuses development shortcuts (SECURITY.md §10)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from aria_core.config import MIGRATION_ENV_FILE, Settings

APP_DSN = "postgresql+asyncpg://aria_app:x@127.0.0.1:5433/aria"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test here against an empty environment.

    Settings reads .env from the working directory, so without this the results
    would depend on the developer's own local file — and a test asserting "this is
    required in production" would pass only on a machine that happens not to set it.
    """
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("ARIA_"):
            monkeypatch.delenv(name, raising=False)


def _settings(**overrides: object) -> Settings:
    return Settings(database_url=SecretStr(APP_DSN), **overrides)  # type: ignore[arg-type]


class TestRequiredValues:
    def test_a_missing_database_url_fails_at_construction(self) -> None:
        with pytest.raises(ValidationError, match="database_url"):
            Settings()

    def test_settings_are_immutable(self) -> None:
        with pytest.raises(ValidationError):
            _settings().env = "prod"  # type: ignore[misc]


class TestSecretsAreNotPrintable:
    def test_a_dsn_does_not_render_its_password(self) -> None:
        settings = Settings(database_url=SecretStr("postgresql+asyncpg://aria_app:hunter2@127.0.0.1/aria"))
        for rendered in (str(settings), repr(settings), f"{settings}", str(settings.database_url)):
            assert "hunter2" not in rendered

    def test_reading_the_value_is_explicit(self) -> None:
        assert (
            "hunter2"
            in Settings(
                database_url=SecretStr("postgresql+asyncpg://aria_app:hunter2@127.0.0.1/aria")
            ).database_dsn()
        )


class TestTheGatewayStaysInternal:
    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "127.0.0.2"])
    def test_loopback_addresses_are_accepted(self, host: str) -> None:
        assert _settings(llm_gateway_bind_host=host).llm_gateway_bind_host == host

    # These addresses are the ones being refused, not bound.
    @pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "203.0.113.7", "::"])  # noqa: S104
    def test_anything_reachable_from_the_network_is_refused(self, host: str) -> None:
        # SECURITY.md §2.5: internal services are not exposed. Enforced by the
        # setting, so no way of starting the process can opt out.
        with pytest.raises(ValidationError, match="loopback"):
            _settings(llm_gateway_bind_host=host)

    def test_a_hostname_is_refused_rather_than_resolved(self) -> None:
        # "localhost" can resolve to something else entirely depending on hosts files.
        with pytest.raises(ValidationError, match="loopback"):
            _settings(llm_gateway_bind_host="localhost")

    def test_a_privileged_port_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _settings(llm_gateway_bind_port=80)


class TestProductionRefusesDevelopmentShortcuts:
    def test_dev_defaults_are_fine_in_dev(self) -> None:
        assert _settings(env="dev").is_development is True

    def test_production_requires_a_service_token(self) -> None:
        with pytest.raises(ValidationError, match="service_token is required"):
            _settings(env="prod", dev_kms_key_path=Path("/keys/managed.key"), apply_task_signing_kid="k1")

    def test_production_refuses_the_file_backed_dev_kms(self) -> None:
        with pytest.raises(ValidationError, match="managed KMS"):
            _settings(
                env="prod",
                llm_gateway_service_token=SecretStr("x"),
                apply_task_signing_kid="k1",
            )

    def test_production_refuses_a_development_signing_key(self) -> None:
        with pytest.raises(ValidationError, match="development key"):
            _settings(
                env="prod",
                llm_gateway_service_token=SecretStr("x"),
                dev_kms_key_path=Path("/keys/managed.key"),
                apply_task_signing_kid="dev-1",
            )

    def test_a_correctly_configured_production_passes(self) -> None:
        settings = _settings(
            env="prod",
            llm_gateway_service_token=SecretStr("x"),
            dev_kms_key_path=Path("/keys/managed.key"),
            apply_task_signing_kid="prod-2026-10",
        )
        assert settings.is_development is False


class TestPathsAndTtl:
    def test_environment_variables_in_paths_are_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
        settings = _settings(dev_kms_key_path=r"%APPDATA%\aria\dev-kms-root.key")
        assert "%APPDATA%" not in str(settings.dev_kms_key_path)
        assert settings.dev_kms_key_path.name == "dev-kms-root.key"

    def test_an_apply_task_lives_for_minutes_not_hours(self) -> None:
        assert _settings().apply_task_ttl_seconds <= 1800
        with pytest.raises(ValidationError):
            _settings(apply_task_ttl_seconds=86_400)


def test_the_migration_env_file_is_named_separately() -> None:
    assert MIGRATION_ENV_FILE == ".env.migrate"
