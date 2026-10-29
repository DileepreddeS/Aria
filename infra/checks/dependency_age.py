"""Fail when uv.lock pins a package file younger than 14 days, unless it is a declared security fix.

The rule (DECISIONS 2026-10-12): any new or upgraded dependency version must have been
on PyPI for at least 14 days. A release published days ago is where a compromised
maintainer account or a poisoned build does its damage before anyone has noticed; two
weeks is long enough for most of those to be found and yanked, and short enough that
ARIA is never far behind.

**One exception, narrow and written down** (DECISIONS 2026-10-22): a release that is the
fix for a published security advisory, under any OSV id (CVE, GHSA, PYSEC). Each use is
declared in pyproject.toml, by exact package, exact version and advisory id:

    [[tool.aria.dependency-age.exemptions]]
    package = "certifi"
    version = "2026.10.19"
    advisory = "GHSA-6q48-6c7w-9gw2"
    expires = 2026-11-03

and holds only while all of these are true:

- OSV says that advisory's fixed version for that package is exactly that version. The
  exemption is for one version; any other version of the package is checked as usual.
- The version is pinned exactly (``package==version`` in ``[tool.uv]
  constraint-dependencies``), and every exact pin there has its exemption. The pin is
  what stops the resolver taking some other young release under the same cover.
- ``expires`` is the day the version's newest file turns 14 days old, read from uv.lock,
  so it cannot be stretched. After it, the exemption is no longer needed and fails the
  check until it is deleted and its pin loosened. For the three days before, the check
  warns, so the failure does not land unannounced in the middle of an unrelated commit.

The check reads upload times from uv.lock itself, so it needs no network unless a young
file is found. Pins that predate the rule are listed in GRANDFATHERED by exact version.

    python infra/checks/dependency_age.py [--lock uv.lock] [--pyproject pyproject.toml]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MINIMUM_AGE = dt.timedelta(days=14)
#: How long before an exemption expires the check starts warning about it.
WARN_BEFORE = dt.timedelta(days=3)
OSV_QUERY = "https://api.osv.dev/v1/query"

#: In uv.lock when the rule was made (2026-10-12) and younger than 14 days then.
#: Exact versions: an upgrade of any of these is new, and is checked like any other.
GRANDFATHERED: frozenset[tuple[str, str]] = frozenset(
    {
        ("ast-serialize", "0.12.1"),
        ("asyncpg", "0.32.0"),
        ("charset-normalizer", "3.5.2"),
        ("cryptography", "50.0.2"),
        ("fastapi", "0.142.4"),
        ("filelock", "4.0.12"),
        ("iniconfig", "2.3.1"),
        ("librt", "0.16.0"),
        ("markupsafe", "3.0.4"),
        ("msgpack", "1.2.3"),
        ("mypy", "2.4.0"),
        ("opentelemetry-api", "1.45.1"),
        ("opentelemetry-instrumentation", "0.66b1"),
        ("opentelemetry-instrumentation-asgi", "0.66b1"),
        ("opentelemetry-instrumentation-fastapi", "0.66b1"),
        ("opentelemetry-sdk", "1.45.1"),
        ("opentelemetry-semantic-conventions", "0.66b1"),
        ("opentelemetry-util-http", "0.66b1"),
        ("platformdirs", "4.12.4"),
        ("python-dotenv", "1.2.4"),
        ("ruff", "0.16.10"),
        ("sqlalchemy", "2.1.4"),
        ("tomli", "2.5.0"),
        ("virtualenv", "21.14.5"),
    }
)


@dataclass(frozen=True, slots=True)
class YoungFile:
    package: str
    version: str
    filename: str
    age: dt.timedelta


@dataclass(frozen=True, slots=True)
class Exemption:
    """One declared use of the exception: this package, this version, this advisory, until then."""

    package: str
    version: str
    advisory: str
    expires: dt.date


@dataclass
class Result:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _uploaded(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _files(package: dict[str, Any]) -> list[dict[str, Any]]:
    return ([package["sdist"]] if "sdist" in package else []) + list(package.get("wheels", []))


def young_files(lock: dict[str, Any], now: dt.datetime) -> list[YoungFile]:
    """Every pinned registry file under the minimum age, grandfathered pins excepted.

    A file without an upload time is reported too, with no age: the rule cannot be
    shown to hold for it, and silence would be a pass.
    """
    found: list[YoungFile] = []
    packages = lock.get("package", [])
    assert isinstance(packages, list)
    for package in packages:
        name, version = str(package["name"]), str(package.get("version", ""))
        if "registry" not in package.get("source", {}):
            continue  # workspace members and local paths are ours, not downloads
        if (name, version) in GRANDFATHERED:
            continue
        for file in _files(package):
            filename = str(file.get("url", "")).rsplit("/", 1)[-1]
            uploaded = file.get("upload-time")
            if uploaded is None:
                found.append(YoungFile(name, version, filename, dt.timedelta(0)))
                continue
            age = now - _uploaded(str(uploaded))
            if age < MINIMUM_AGE:
                found.append(YoungFile(name, version, filename, age))
    return found


def query_osv(package: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        OSV_QUERY,
        data=json.dumps({"package": {"name": package, "ecosystem": "PyPI"}}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    # OSV_QUERY, a fixed https URL; nothing here can choose another scheme or host.
    # nosemgrep: dynamic-urllib-use-detected
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  (fixed https URL)
        body = json.load(response)
    vulns = body.get("vulns", [])
    return vulns if isinstance(vulns, list) else []


def advisory_fixes(
    advisory_id: str, package: str, version: str, advisories: Iterable[dict[str, Any]]
) -> bool:
    """True if the named advisory (by id or alias) has exactly ``version`` as its PyPI fix."""
    for advisory in advisories:
        ids = {str(advisory.get("id", "")), *(str(alias) for alias in advisory.get("aliases", []) or [])}
        if advisory_id not in ids:
            continue
        for affected in advisory.get("affected", []) or []:
            target = affected.get("package", {})
            if target.get("ecosystem") != "PyPI" or str(target.get("name", "")).lower() != package.lower():
                continue
            for range_ in affected.get("ranges", []) or []:
                if any(event.get("fixed") == version for event in range_.get("events", []) or []):
                    return True
    return False


def load_exemptions(pyproject: dict[str, Any]) -> list[Exemption]:
    declared = pyproject.get("tool", {}).get("aria", {}).get("dependency-age", {}).get("exemptions", [])
    exemptions = []
    for entry in declared:
        missing = {"package", "version", "advisory", "expires"} - set(entry)
        if missing:
            raise ValueError(f"an exemption is missing {sorted(missing)}: {entry}")
        if not isinstance(entry["expires"], dt.date) or isinstance(entry["expires"], dt.datetime):
            raise ValueError(f"expires must be a TOML date (2026-11-03), not {entry['expires']!r}")
        exemptions.append(
            Exemption(str(entry["package"]), str(entry["version"]), str(entry["advisory"]), entry["expires"])
        )
    return exemptions


def exact_pins(pyproject: dict[str, Any]) -> dict[str, str]:
    """``package==version`` entries in [tool.uv] constraint-dependencies."""
    pins: dict[str, str] = {}
    for constraint in pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", []):
        name, separator, version = str(constraint).partition("==")
        if separator:
            pins[name.strip().lower()] = version.strip()
    return pins


def _expiry_from_lock(lock: dict[str, Any], package: str, version: str) -> dt.date | None:
    for entry in lock.get("package", []):
        if entry["name"] == package and entry.get("version") == version:
            times = [_uploaded(str(file["upload-time"])) for file in _files(entry) if "upload-time" in file]
            return (max(times) + MINIMUM_AGE).date() if times else None
    return None


def check_exemptions(
    lock: dict[str, Any], exemptions: Sequence[Exemption], pins: dict[str, str], today: dt.date
) -> Result:
    """What can be judged without the network: dates, pins, and that each exemption is in use."""
    result = Result()
    declared = {(item.package.lower(), item.version) for item in exemptions}
    for item in exemptions:
        label = f"exemption {item.package} {item.version} ({item.advisory})"
        expected = _expiry_from_lock(lock, item.package, item.version)
        if expected is None:
            result.failures.append(f"{label} names a version uv.lock does not pin; delete it")
            continue
        if item.expires != expected:
            result.failures.append(
                f"{label} expires {item.expires}, but the version turns 14 days old on {expected}; "
                "the date comes from uv.lock and cannot be moved"
            )
        if today > item.expires:
            result.failures.append(
                f"{label} expired on {item.expires}: the version is old enough now. Delete the exemption "
                f'and loosen its pin to "{item.package}>={item.version}" in [tool.uv] constraint-dependencies'
            )
        elif today >= item.expires - WARN_BEFORE:
            days = (item.expires - today).days
            result.warnings.append(
                f"{label} expires on {item.expires} ({days} day(s)): delete it and loosen its pin by then, "
                "or this check fails"
            )
        if pins.get(item.package.lower()) != item.version:
            result.failures.append(
                f'{label} needs the exact pin "{item.package}=={item.version}" in [tool.uv] '
                "constraint-dependencies, so no other young release can resolve under it"
            )
    for package, version in sorted(pins.items()):
        if (package, version) not in declared:
            result.failures.append(
                f"{package}=={version} is pinned exactly with no exemption; an exact pin is only for one"
            )
    return result


def check(
    lock: dict[str, Any],
    now: dt.datetime,
    advisories_for: Callable[[str], list[dict[str, Any]]] = query_osv,
    exemptions: Sequence[Exemption] = (),
    pins: dict[str, str] | None = None,
) -> Result:
    """Every young pin must be a declared, confirmed, unexpired exemption."""
    result = check_exemptions(lock, exemptions, pins or {}, now.date())
    by_version: dict[tuple[str, str], list[YoungFile]] = {}
    for young in young_files(lock, now):
        by_version.setdefault((young.package, young.version), []).append(young)

    for (package, version), files in sorted(by_version.items()):
        youngest = min(file.age for file in files)
        declared = [
            item for item in exemptions if (item.package.lower(), item.version) == (package.lower(), version)
        ]
        if not declared:
            result.failures.append(
                f"{package} {version} has {len(files)} file(s) on PyPI for less than {MINIMUM_AGE.days} days "
                f"(youngest: {files[0].filename}, {youngest.days} day(s)). Pin an older version, or, if "
                "it is the fix for a published advisory, declare it in [tool.aria.dependency-age] exemptions"
            )
            continue
        advisories = advisories_for(package)
        confirmed = [item for item in declared if advisory_fixes(item.advisory, package, version, advisories)]
        if not confirmed:
            result.failures.append(
                f"{package} {version} is declared as the fix for {', '.join(i.advisory for i in declared)}, "
                "but OSV does not list that version as its fix"
            )
            continue
        result.notes.append(
            f"{package} {version} is {youngest.days} day(s) old and allowed until {confirmed[0].expires}: "
            f"it fixes {confirmed[0].advisory}"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    arguments = parser.parse_args(argv)
    lock = tomllib.loads(arguments.lock.read_text(encoding="utf-8"))
    pyproject = tomllib.loads(arguments.pyproject.read_text(encoding="utf-8"))
    result = check(
        lock,
        dt.datetime.now(dt.UTC),
        exemptions=load_exemptions(pyproject),
        pins=exact_pins(pyproject),
    )
    for note in result.notes:
        print(f"note: {note}")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for failure in result.failures:
        print(f"error: {failure}", file=sys.stderr)
    if not result.failures:
        print(
            f"every pinned file in {arguments.lock} is at least {MINIMUM_AGE.days} days old, "
            "or a declared fix"
        )
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
