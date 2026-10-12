"""The 14-day dependency rule (infra/checks/dependency_age.py, DECISIONS 2026-10-12).

Synthetic locks and recorded OSV advisories, so nothing here touches the network.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NOW = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)


def _load_check() -> ModuleType:
    path = REPO_ROOT / "infra" / "checks" / "dependency_age.py"
    spec = importlib.util.spec_from_file_location("dependency_age", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dependency_age"] = module
    spec.loader.exec_module(module)
    return module


age = _load_check()


def _package(name: str, version: str, *uploaded: dt.datetime, registry: bool = True) -> dict[str, Any]:
    files = [
        {"url": f"https://files.example/{name}-{version}-{i}.whl", "upload-time": when.isoformat()}
        for i, when in enumerate(uploaded)
    ]
    source = {"registry": "https://pypi.org/simple"} if registry else {"editable": "packages/core"}
    return {"name": name, "version": version, "source": source, "wheels": files}


def _lock(*packages: dict[str, Any]) -> dict[str, Any]:
    return {"package": list(packages)}


def _no_advisories(package: str) -> list[dict[str, Any]]:
    return []


def _days_ago(days: float) -> dt.datetime:
    return NOW - dt.timedelta(days=days)


class TestTheMinimumAge:
    def test_a_version_older_than_fourteen_days_passes(self) -> None:
        failures, _ = age.check(_lock(_package("pdfplumber", "0.11.10", _days_ago(118))), NOW, _no_advisories)
        assert failures == []

    def test_a_version_younger_than_fourteen_days_fails(self) -> None:
        failures, _ = age.check(_lock(_package("pypdfium2", "5.14.0", _days_ago(7))), NOW, _no_advisories)
        assert len(failures) == 1
        assert "pypdfium2 5.14.0" in failures[0]
        assert "CVE" in failures[0]  # says what the one exemption is

    def test_one_young_file_is_enough_to_fail(self) -> None:
        # A new wheel uploaded to an old release is a new download all the same.
        package = _package("pillow", "12.3.0", _days_ago(103), _days_ago(2))
        failures, _ = age.check(_lock(package), NOW, _no_advisories)
        assert len(failures) == 1

    def test_the_boundary_is_fourteen_whole_days(self) -> None:
        exactly = _package("a", "1.0", NOW - dt.timedelta(days=14))
        just_short = _package("b", "1.0", NOW - dt.timedelta(days=14) + dt.timedelta(minutes=1))
        failures, _ = age.check(_lock(exactly, just_short), NOW, _no_advisories)
        assert [failure.split()[0] for failure in failures] == ["b"]

    def test_a_file_without_an_upload_time_fails_rather_than_passing_silently(self) -> None:
        package = {"name": "mystery", "version": "1.0", "source": {"registry": "x"}, "wheels": [{"url": "u"}]}
        failures, _ = age.check(_lock(package), NOW, _no_advisories)
        assert len(failures) == 1

    def test_workspace_members_are_not_downloads(self) -> None:
        member = _package("aria-core", "0.0.0", _days_ago(0), registry=False)
        failures, _ = age.check(_lock(member), NOW, _no_advisories)
        assert failures == []


class TestPinsThatPredateTheRule:
    def test_a_grandfathered_pin_passes_at_its_exact_version(self) -> None:
        failures, _ = age.check(_lock(_package("fastapi", "0.142.4", _days_ago(4))), NOW, _no_advisories)
        assert failures == []

    def test_upgrading_a_grandfathered_package_is_checked_like_any_other(self) -> None:
        failures, _ = age.check(_lock(_package("fastapi", "0.142.5", _days_ago(1))), NOW, _no_advisories)
        assert len(failures) == 1

    def test_every_grandfathered_pin_is_old_enough_by_the_day_the_list_can_go(self) -> None:
        # The docstring says the list can be deleted on 2026-10-22. Checked against the
        # upload times in the lock, so the date is a fact rather than an estimate.
        import tomllib

        lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
        expiry = dt.datetime(2026, 10, 22, 0, 0, tzinfo=dt.UTC)
        for package in lock["package"]:
            if (package["name"], package.get("version")) not in age.GRANDFATHERED:
                continue
            files = ([package["sdist"]] if "sdist" in package else []) + package.get("wheels", [])
            newest = max(age._uploaded(file["upload-time"]) for file in files)
            assert newest + age.MINIMUM_AGE <= expiry, package["name"]


class TestTheCveException:
    @staticmethod
    def _advisory(identifier: str, aliases: list[str], package: str, fixed: str) -> dict[str, Any]:
        return {
            "id": identifier,
            "aliases": aliases,
            "affected": [
                {
                    "package": {"ecosystem": "PyPI", "name": package},
                    "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": fixed}]}],
                }
            ],
        }

    def test_a_young_release_that_fixes_a_cve_is_taken_immediately(self) -> None:
        advisory = self._advisory("GHSA-xxxx-yyyy-zzzz", ["CVE-2026-12345"], "cryptography", "50.0.3")
        failures, notes = age.check(
            _lock(_package("cryptography", "50.0.3", _days_ago(1))), NOW, lambda package: [advisory]
        )
        assert failures == []
        assert "CVE-2026-12345" in notes[0]

    def test_an_advisory_without_a_cve_does_not_qualify(self) -> None:
        advisory = self._advisory("GHSA-xxxx-yyyy-zzzz", [], "cryptography", "50.0.3")
        failures, _ = age.check(
            _lock(_package("cryptography", "50.0.3", _days_ago(1))), NOW, lambda package: [advisory]
        )
        assert len(failures) == 1

    def test_a_cve_fixed_in_some_other_version_does_not_qualify(self) -> None:
        advisory = self._advisory("PYSEC-2026-1", ["CVE-2026-12345"], "cryptography", "50.0.2")
        failures, _ = age.check(
            _lock(_package("cryptography", "50.0.3", _days_ago(1))), NOW, lambda package: [advisory]
        )
        assert len(failures) == 1

    def test_a_cve_for_another_package_does_not_qualify(self) -> None:
        advisory = self._advisory("PYSEC-2026-1", ["CVE-2026-12345"], "pyopenssl", "50.0.3")
        failures, _ = age.check(
            _lock(_package("cryptography", "50.0.3", _days_ago(1))), NOW, lambda package: [advisory]
        )
        assert len(failures) == 1

    def test_old_files_never_cost_a_network_call(self) -> None:
        def no_network(package: str) -> list[dict[str, Any]]:
            raise AssertionError("OSV was asked about a package that needed no exemption")

        failures, _ = age.check(_lock(_package("pdfplumber", "0.11.10", _days_ago(118))), NOW, no_network)
        assert failures == []


def test_the_lock_in_this_repository_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(package: str) -> list[dict[str, Any]]:
        raise AssertionError(f"{package} is young and not grandfathered; the lock breaks the rule")

    import tomllib

    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    failures, _ = age.check(lock, dt.datetime.now(dt.UTC), offline)
    assert failures == []
