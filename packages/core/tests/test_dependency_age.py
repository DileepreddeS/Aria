"""The 14-day dependency rule and its one exception (infra/checks/dependency_age.py).

Synthetic locks and recorded OSV advisories, so nothing here touches the network.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import tomllib
import warnings
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
        failures = age.check(
            _lock(_package("pdfplumber", "0.11.10", _days_ago(118))), NOW, _no_advisories
        ).failures
        assert failures == []

    def test_a_version_younger_than_fourteen_days_fails(self) -> None:
        failures = age.check(
            _lock(_package("pypdfium2", "5.14.0", _days_ago(7))), NOW, _no_advisories
        ).failures
        assert len(failures) == 1
        assert "pypdfium2 5.14.0" in failures[0]
        assert "exemptions" in failures[0]  # says how the one exception is claimed

    def test_one_young_file_is_enough_to_fail(self) -> None:
        # A new wheel uploaded to an old release is a new download all the same.
        package = _package("pillow", "12.3.0", _days_ago(103), _days_ago(2))
        failures = age.check(_lock(package), NOW, _no_advisories).failures
        assert len(failures) == 1

    def test_the_boundary_is_fourteen_whole_days(self) -> None:
        exactly = _package("a", "1.0", NOW - dt.timedelta(days=14))
        just_short = _package("b", "1.0", NOW - dt.timedelta(days=14) + dt.timedelta(minutes=1))
        failures = age.check(_lock(exactly, just_short), NOW, _no_advisories).failures
        assert [failure.split()[0] for failure in failures] == ["b"]

    def test_a_file_without_an_upload_time_fails_rather_than_passing_silently(self) -> None:
        package = {"name": "mystery", "version": "1.0", "source": {"registry": "x"}, "wheels": [{"url": "u"}]}
        failures = age.check(_lock(package), NOW, _no_advisories).failures
        assert len(failures) == 1

    def test_workspace_members_are_not_downloads(self) -> None:
        member = _package("aria-core", "0.0.0", _days_ago(0), registry=False)
        failures = age.check(_lock(member), NOW, _no_advisories).failures
        assert failures == []


class TestPinsThatPredateTheRule:
    def test_a_grandfathered_pin_passes_at_its_exact_version(self) -> None:
        failures = age.check(
            _lock(_package("fastapi", "0.142.4", _days_ago(4))), NOW, _no_advisories
        ).failures
        assert failures == []

    def test_upgrading_a_grandfathered_package_is_checked_like_any_other(self) -> None:
        failures = age.check(
            _lock(_package("fastapi", "0.142.5", _days_ago(1))), NOW, _no_advisories
        ).failures
        assert len(failures) == 1

    def test_every_grandfathered_pin_is_old_enough_by_the_day_the_list_can_go(self) -> None:
        # The list can be deleted from 2026-10-22 (DECISIONS 2026-10-12). Checked against the
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


UPLOADED = _days_ago(1)
#: The day a version uploaded at UPLOADED turns 14 days old.
EXPIRES = (UPLOADED + dt.timedelta(days=14)).date()


def _exempt(
    package: str = "certifi",
    version: str = "2026.10.19",
    advisory: str = "GHSA-aaaa-bbbb-cccc",
    expires: dt.date = EXPIRES,
) -> Any:
    return age.Exemption(package, version, advisory, expires)


def _run(
    *packages: dict[str, Any],
    exemptions: tuple[Any, ...] = (),
    pins: dict[str, str] | None = None,
    advisories: list[dict[str, Any]] | None = None,
    now: dt.datetime = NOW,
) -> Any:
    if pins is None:
        pins = {item.package: item.version for item in exemptions}
    return age.check(_lock(*packages), now, lambda package: advisories or [], exemptions, pins)


class TestADeclaredSecurityFix:
    """The one exception: declared by package, version and advisory, and confirmed by OSV."""

    def test_a_declared_fix_osv_confirms_passes_under_any_osv_id(self) -> None:
        # GHSA with no CVE: certifi's emSign removal was published exactly so.
        advisory = _advisory("GHSA-aaaa-bbbb-cccc", ["PYSEC-2026-1"], "certifi", "2026.10.19")
        result = _run(
            _package("certifi", "2026.10.19", UPLOADED), exemptions=(_exempt(),), advisories=[advisory]
        )
        assert result.failures == []
        assert "GHSA-aaaa-bbbb-cccc" in result.notes[0]

    def test_an_alias_names_the_advisory_as_well_as_its_id(self) -> None:
        advisory = _advisory("PYSEC-2026-1", ["GHSA-aaaa-bbbb-cccc"], "certifi", "2026.10.19")
        result = _run(
            _package("certifi", "2026.10.19", UPLOADED), exemptions=(_exempt(),), advisories=[advisory]
        )
        assert result.failures == []

    def test_an_undeclared_fix_fails_even_though_osv_lists_it(self) -> None:
        # No automatic exemption: every use of the exception is written down.
        advisory = _advisory("GHSA-aaaa-bbbb-cccc", ["CVE-2026-1"], "certifi", "2026.10.19")
        result = _run(_package("certifi", "2026.10.19", UPLOADED), advisories=[advisory])
        assert len(result.failures) == 1
        assert "declare it" in result.failures[0]

    def test_a_declared_advisory_fixed_in_another_version_fails(self) -> None:
        advisory = _advisory("GHSA-aaaa-bbbb-cccc", [], "certifi", "2026.10.18")
        result = _run(
            _package("certifi", "2026.10.19", UPLOADED), exemptions=(_exempt(),), advisories=[advisory]
        )
        assert "OSV does not list that version" in result.failures[0]

    def test_another_advisory_fixing_that_version_does_not_stand_in_for_the_declared_one(self) -> None:
        # The exemption names its advisory; a different one, even with the right fix, is not it.
        other = _advisory("GHSA-zzzz-zzzz-zzzz", ["CVE-2026-9"], "certifi", "2026.10.19")
        result = _run(
            _package("certifi", "2026.10.19", UPLOADED), exemptions=(_exempt(),), advisories=[other]
        )
        assert "OSV does not list that version" in result.failures[0]

    def test_a_declared_advisory_for_another_package_fails(self) -> None:
        advisory = _advisory("GHSA-aaaa-bbbb-cccc", [], "requests", "2026.10.19")
        result = _run(
            _package("certifi", "2026.10.19", UPLOADED), exemptions=(_exempt(),), advisories=[advisory]
        )
        assert len(result.failures) == 1

    def test_an_advisory_osv_does_not_know_fails(self) -> None:
        result = _run(_package("certifi", "2026.10.19", UPLOADED), exemptions=(_exempt(),), advisories=[])
        assert len(result.failures) == 1

    def test_an_exemption_covers_its_own_version_and_no_other(self) -> None:
        advisory = _advisory("GHSA-aaaa-bbbb-cccc", [], "certifi", "2026.10.19")
        result = _run(
            _package("certifi", "2026.10.20", UPLOADED),
            exemptions=(_exempt(),),
            pins={},
            advisories=[advisory],
        )
        assert any("certifi 2026.10.20" in failure for failure in result.failures)

    def test_old_files_never_cost_a_network_call(self) -> None:
        def no_network(package: str) -> list[dict[str, Any]]:
            raise AssertionError("OSV was asked about a package that needed no exemption")

        result = age.check(_lock(_package("pdfplumber", "0.11.10", _days_ago(118))), NOW, no_network)
        assert result.failures == []


class TestAnExemptionExpires:
    """Warned three days ahead, then failed: it cannot be forgotten and cannot ambush."""

    @staticmethod
    def _on(day: dt.date) -> Any:
        return age.check_exemptions(
            _lock(_package("certifi", "2026.10.19", UPLOADED)),
            [_exempt()],
            {"certifi": "2026.10.19"},
            day,
        )

    def test_before_the_warning_window_it_is_silent(self) -> None:
        result = self._on(EXPIRES - dt.timedelta(days=4))
        assert (result.failures, result.warnings) == ([], [])

    @pytest.mark.parametrize("days_left", [3, 2, 1, 0])
    def test_for_three_days_up_to_and_on_the_day_it_warns_and_passes(self, days_left: int) -> None:
        result = self._on(EXPIRES - dt.timedelta(days=days_left))
        assert result.failures == []
        assert len(result.warnings) == 1
        assert f"({days_left} day(s))" in result.warnings[0]

    def test_the_day_after_it_fails_and_says_what_to_do(self) -> None:
        result = self._on(EXPIRES + dt.timedelta(days=1))
        assert len(result.failures) == 1
        assert 'loosen its pin to "certifi>=2026.10.19"' in result.failures[0]

    def test_the_date_is_the_lock_s_not_a_choice(self) -> None:
        result = age.check_exemptions(
            _lock(_package("certifi", "2026.10.19", UPLOADED)),
            [_exempt(expires=EXPIRES + dt.timedelta(days=30))],
            {"certifi": "2026.10.19"},
            NOW.date(),
        )
        assert any("cannot be moved" in failure for failure in result.failures)

    def test_an_exemption_for_a_version_the_lock_does_not_pin_fails(self) -> None:
        result = age.check_exemptions(_lock(), [_exempt()], {"certifi": "2026.10.19"}, NOW.date())
        assert any("does not pin" in failure for failure in result.failures)


class TestExemptionsAndExactPins:
    def test_an_exemption_needs_its_exact_pin(self) -> None:
        result = age.check_exemptions(
            _lock(_package("certifi", "2026.10.19", UPLOADED)), [_exempt()], {}, NOW.date()
        )
        assert any('"certifi==2026.10.19"' in failure for failure in result.failures)

    def test_an_exact_pin_needs_its_exemption(self) -> None:
        result = age.check_exemptions(_lock(), [], {"pypdf": "6.21.1"}, NOW.date())
        assert any(
            "pypdf==6.21.1 is pinned exactly with no exemption" in failure for failure in result.failures
        )

    def test_pins_are_read_from_the_constraints(self) -> None:
        pyproject = {
            "tool": {"uv": {"constraint-dependencies": ["pillow==13.0.0", "pypdf>=6.0", "Certifi == 1"]}}
        }
        assert age.exact_pins(pyproject) == {"pillow": "13.0.0", "certifi": "1"}

    def test_an_exemption_without_a_toml_date_is_refused(self) -> None:
        entry = {"package": "a", "version": "1", "advisory": "GHSA-x", "expires": "2026-11-03"}
        with pytest.raises(ValueError, match="TOML date"):
            age.load_exemptions({"tool": {"aria": {"dependency-age": {"exemptions": [entry]}}}})


def _recorded(package: str, advisory: str, alias: str, fixed: str) -> dict[str, Any]:
    return _advisory(advisory, [alias], package, fixed)


#: OSV's answers for the exemptions in pyproject.toml, as recorded on 2026-10-22: the test runs
#: offline, and the live check in `make audit` asks OSV itself. An exemption or pin that moves no
#: longer matches its record and fails here until the new release is checked and recorded.
RECORDED_FIXES: dict[str, list[dict[str, Any]]] = {
    "pillow": [_recorded("pillow", "GHSA-8g5g-8vmr-9782", "CVE-2026-112403", "13.0.0")],
    "pypdf": [_recorded("pypdf", "GHSA-5hx5-vpf9-5wcv", "CVE-2026-113786", "6.21.1")],
    "virtualenv": [_recorded("virtualenv", "GHSA-c978-59m8-c77v", "CVE-2026-113722", "21.14.9")],
    "certifi": [_recorded("certifi", "GHSA-6q48-6c7w-9gw2", "PYSEC-2026-4320", "2026.10.19")],
}


def test_the_lock_in_this_repository_passes() -> None:
    def offline(package: str) -> list[dict[str, Any]]:
        if package in RECORDED_FIXES:
            return RECORDED_FIXES[package]
        raise AssertionError(f"{package} is young and has no recorded advisory; the lock breaks the rule")

    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    result = age.check(
        lock,
        dt.datetime.now(dt.UTC),
        offline,
        age.load_exemptions(pyproject),
        age.exact_pins(pyproject),
    )
    # Shown in pytest's summary for the three days before an exemption expires.
    for warning in result.warnings:
        warnings.warn(warning, stacklevel=1)
    assert result.failures == []
