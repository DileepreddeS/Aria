"""The API refuses to start with credentials it must not hold (SECURITY.md §2.4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from aria_api.startup import run_preflight_checks
from aria_core.config import MIGRATION_ENV_VAR, Settings, assert_no_migration_credentials
from aria_document_parser.image import ImageStatus

APP_DSN = "postgresql+asyncpg://aria_app:x@127.0.0.1:5433/aria"


def _settings(dsn: str = APP_DSN) -> Settings:
    return Settings(database_url=SecretStr(dsn), parser_isolation="process")


@pytest.fixture
def current_parser_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """For tests about other checks: the parser image is present and current."""
    monkeypatch.setattr("aria_api.startup.image_status", lambda image: ImageStatus(True))


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

    def test_a_scanner_that_does_not_scan_stops_start_up_outside_dev(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole point of the check: this configuration is fine locally and must
        # never serve a real user (SECURITY.md §7).
        monkeypatch.delenv(MIGRATION_ENV_VAR, raising=False)
        from aria_api.uploads.scanner import ScannerMisconfigured

        with pytest.raises(ScannerMisconfigured, match="must be scanned"):
            run_preflight_checks(
                Settings(
                    database_url=SecretStr(APP_DSN),
                    env="prod",
                    llm_gateway_service_token=SecretStr("x"),
                    api_service_token=SecretStr("x"),
                    dev_kms_key_path=Path("/keys/managed.key"),
                    apply_task_signing_kid="prod-1",
                )
            )

    @pytest.mark.usefixtures("current_parser_image")
    def test_a_configured_scanner_passes_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MIGRATION_ENV_VAR, raising=False)
        run_preflight_checks(
            Settings(
                database_url=SecretStr(APP_DSN),
                env="prod",
                clamav_host="127.0.0.1",
                llm_gateway_service_token=SecretStr("x"),
                api_service_token=SecretStr("x"),
                dev_kms_key_path=Path("/keys/managed.key"),
                apply_task_signing_kid="prod-1",
            )
        )

    def test_migration_credentials_in_the_real_environment_stop_start_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIGRATION_ENV_VAR, "postgresql+asyncpg://aria_migrate:x@127.0.0.1:5433/aria")
        with pytest.raises(RuntimeError):
            run_preflight_checks(_settings())


class TestUploadsAreParsedInIsolation:
    """SECURITY.md §7: the parse runs in a container, and the container is current."""

    def test_a_missing_or_stale_image_stops_start_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aria_api.startup import ParserIsolationUnavailable, assert_parser_isolation

        reason = "the parser image aria-document-parser:local was built from different sources"
        monkeypatch.setattr("aria_api.startup.image_status", lambda image: ImageStatus(False, reason))
        with pytest.raises(ParserIsolationUnavailable, match="different sources"):
            assert_parser_isolation(Settings(database_url=SecretStr(APP_DSN)))

    def test_a_current_image_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aria_api.startup import assert_parser_isolation

        asked: list[str] = []

        def status(image: str) -> ImageStatus:
            asked.append(image)
            return ImageStatus(True)

        monkeypatch.setattr("aria_api.startup.image_status", status)
        assert_parser_isolation(Settings(database_url=SecretStr(APP_DSN), parser_image="aria-parser:test"))
        assert asked == ["aria-parser:test"]

    def test_docker_is_the_default(self) -> None:
        assert Settings(database_url=SecretStr(APP_DSN)).parser_isolation == "docker"

    def test_a_plain_process_is_accepted_in_dev_without_asking_docker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aria_api.startup import assert_parser_isolation

        def must_not_be_called(image: str) -> ImageStatus:
            raise AssertionError("process isolation must not depend on Docker")

        monkeypatch.setattr("aria_api.startup.image_status", must_not_be_called)
        assert_parser_isolation(Settings(database_url=SecretStr(APP_DSN), parser_isolation="process"))

    def test_a_plain_process_outside_dev_is_refused_even_past_the_validator(self) -> None:
        # Settings refuses this combination; the preflight repeats the rule so it does
        # not rest on one validator. model_copy skips validation, which is the point.
        from aria_api.startup import ParserIsolationUnavailable, assert_parser_isolation

        settings = Settings(database_url=SecretStr(APP_DSN)).model_copy(
            update={"env": "prod", "parser_isolation": "process"}
        )
        with pytest.raises(ParserIsolationUnavailable, match="parsed in a container"):
            assert_parser_isolation(settings)
