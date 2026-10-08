"""The API refuses to start with credentials it must not hold (SECURITY.md §2.4)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from aria_api.startup import run_preflight_checks
from aria_core.config import MIGRATION_ENV_VAR, Settings, assert_no_migration_credentials

APP_DSN = "postgresql+asyncpg://aria_app:x@127.0.0.1:5433/aria"


def _settings(dsn: str = APP_DSN) -> Settings:
    return Settings(database_url=SecretStr(dsn))


class TestMigrationCredentialsAreRefused:
    def test_an_environment_without_them_passes(self) -> None:
        assert_no_migration_credentials({"ARIA_DATABASE_URL": APP_DSN})

    def test_an_environment_with_them_is_refused(self) -> None:
        with pytest.raises(RuntimeError, match="must never hold its credentials"):
            assert_no_migration_credentials(
                {MIGRATION_ENV_VAR: "postgresql+asyncpg://aria_migrate:x@127.0.0.1:5433/aria"}
            )

    def test_the_check_is_case_insensitive(self) -> None:
        # Environment variable names are case-insensitive on Windows, so a lowercase
        # spelling must not slip past.
        with pytest.raises(RuntimeError):
            assert_no_migration_credentials({MIGRATION_ENV_VAR.lower(): "x"})

    def test_the_message_says_where_they_belong(self) -> None:
        with pytest.raises(RuntimeError, match=r"\.env\.migrate"):
            assert_no_migration_credentials({MIGRATION_ENV_VAR: "x"})

    def test_application_settings_have_no_field_for_them(self) -> None:
        # The strongest version of this rule: there is nothing to read, so no future
        # code path can hand the credentials out.
        assert "migrate_database_url" not in Settings.model_fields
        assert not hasattr(_settings(), "migrate_dsn")


class TestPreflight:
    def test_an_unprivileged_role_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MIGRATION_ENV_VAR, raising=False)
        run_preflight_checks(_settings())

    @pytest.mark.parametrize("role", ["aria_migrate", "aria_owner"])
    def test_a_privileged_database_role_is_refused(self, role: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MIGRATION_ENV_VAR, raising=False)
        with pytest.raises(RuntimeError, match="names a privileged role"):
            run_preflight_checks(_settings(f"postgresql+asyncpg://{role}:x@127.0.0.1:5433/aria"))

    def test_migration_credentials_in_the_real_environment_stop_start_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIGRATION_ENV_VAR, "postgresql+asyncpg://aria_migrate:x@127.0.0.1:5433/aria")
        with pytest.raises(RuntimeError):
            run_preflight_checks(_settings())
