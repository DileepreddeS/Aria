"""Fail when uv.lock pins a package file younger than 14 days.

The rule (DECISIONS 2026-10-12): any new or upgraded dependency version must have been
on PyPI for at least 14 days. A release published days ago is where a compromised
maintainer account or a poisoned build does its damage before anyone has noticed; two
weeks is long enough for most of those to be found and yanked, and short enough that
ARIA is never far behind.

One exception, taken immediately whatever its age: a release that fixes a published
CVE. It is not declared by hand. For a young pin the check asks OSV (osv.dev) whether
that exact version is the fixed version of an advisory carrying a CVE identifier, and
accepts it only if so — an exception nobody can claim without the advisory existing.

The check reads upload times from uv.lock itself, which records one for every file,
so it needs no network unless a young file is found. The resolver does the same job
up front: new packages carry a 14-day entry in [tool.uv] exclude-newer-package, so
`uv add` and `uv lock` choose old-enough versions to begin with. This is the gate that
holds when someone overrides that.

Pins that predate the rule are listed in GRANDFATHERED by exact version: they were in
the lock when the rule was made, so they are neither new nor upgraded. Every one of
them is 14 days old by 2026-10-22, after which the list is empty in effect and should
be deleted, along with the per-package resolver entries in favour of one global
`exclude-newer = "14 days"`.

    python infra/checks/dependency_age.py [--lock uv.lock]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MINIMUM_AGE = dt.timedelta(days=14)
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


def _uploaded(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


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
        files = ([package["sdist"]] if "sdist" in package else []) + list(package.get("wheels", []))
        for file in files:
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
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  (fixed https URL)
        body = json.load(response)
    vulns = body.get("vulns", [])
    return vulns if isinstance(vulns, list) else []


def cves_fixed_by(package: str, version: str, advisories: Iterable[dict[str, Any]]) -> list[str]:
    """CVE ids of advisories for which ``version`` is the fixed version on PyPI."""
    fixed: list[str] = []
    for advisory in advisories:
        ids = [str(advisory.get("id", "")), *[str(alias) for alias in advisory.get("aliases", []) or []]]
        cves = [identifier for identifier in ids if identifier.startswith("CVE-")]
        if not cves:
            continue
        for affected in advisory.get("affected", []) or []:
            target = affected.get("package", {})
            if target.get("ecosystem") != "PyPI" or str(target.get("name", "")).lower() != package.lower():
                continue
            for range_ in affected.get("ranges", []) or []:
                if any(event.get("fixed") == version for event in range_.get("events", []) or []):
                    fixed.extend(cves)
    return sorted(set(fixed))


def check(
    lock: dict[str, Any],
    now: dt.datetime,
    advisories_for: Callable[[str], list[dict[str, Any]]] = query_osv,
) -> tuple[list[str], list[str]]:
    """``(failures, notes)``. Empty failures means the lock passes."""
    failures: list[str] = []
    notes: list[str] = []
    by_version: dict[tuple[str, str], list[YoungFile]] = {}
    for young in young_files(lock, now):
        by_version.setdefault((young.package, young.version), []).append(young)

    for (package, version), files in sorted(by_version.items()):
        oldest = max(file.age for file in files)
        cves = cves_fixed_by(package, version, advisories_for(package))
        if cves:
            notes.append(
                f"{package} {version} is {oldest.days} day(s) old and allowed: it fixes {', '.join(cves)}"
            )
            continue
        failures.append(
            f"{package} {version} has {len(files)} file(s) on PyPI for less than {MINIMUM_AGE.days} days "
            f"(youngest: {files[0].filename}, {min(file.age for file in files).days} day(s)). Pin an older "
            "version, or wait; only a release that fixes a published CVE is exempt."
        )
    return failures, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    arguments = parser.parse_args(argv)
    lock = tomllib.loads(arguments.lock.read_text(encoding="utf-8"))
    failures, notes = check(lock, dt.datetime.now(dt.UTC))
    for note in notes:
        print(f"note: {note}")
    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    if not failures:
        print(f"every pinned file in {arguments.lock} is at least {MINIMUM_AGE.days} days old, or exempt")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
