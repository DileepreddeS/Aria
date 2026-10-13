"""Fail when a real person's identifiers appear in the repository.

The founder's name, email and handles reached committed test data because the rule
"fixtures carry a synthetic identity" was a convention nothing enforced. This enforces
it: every tracked file (or the files given) is searched for the identifiers in a
denylist, and any match fails the check.

**The denylist is personal data, so it is never in the repository.** It is read from
the ARIA_PII_DENYLIST environment variable (a CI secret) or, failing that, from
private/pii-denylist.txt, which is gitignored. With neither, the check fails: a guard
with nothing to guard passes by accident. Matches are reported by file, line and kind,
never by value, so CI logs do not republish what the check exists to keep out.

Denylist lines are ``kind: value``; blank lines and ``#`` comments are ignored.

======  ==================================================================
name    words matched whole, in order, any separators between them, any case
email   matched in any case
phone   its digits, with any separators between them
handle  a username or profile slug, matched in any case as a whole token
======  ==================================================================

    python infra/checks/no_personal_identifiers.py [FILE ...]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DENYLIST_FILE = REPO_ROOT / "private" / "pii-denylist.txt"
DENYLIST_ENV = "ARIA_PII_DENYLIST"
KINDS = ("name", "email", "phone", "handle")


@dataclass(frozen=True, slots=True)
class Identifier:
    kind: str
    pattern: re.Pattern[str]


def compile_identifier(kind: str, value: str) -> Identifier:
    value = value.strip()
    if kind == "name":
        words = value.split()
        body = r"[\s_.\-]*".join(re.escape(word) for word in words)
        pattern = rf"(?<![A-Za-z]){body}(?![A-Za-z])"
    elif kind == "email":
        pattern = re.escape(value)
    elif kind == "phone":
        digits = re.sub(r"\D", "", value)[-10:]
        if len(digits) < 7:
            raise ValueError("a phone entry needs at least seven digits")
        # An optional country code in front, so +1 555... and +91 98... are caught too.
        pattern = r"(?<!\d)(?:\+?\d{1,3}[\s().\-]*)?" + r"[\s().+\-]*".join(digits) + r"(?!\d)"
    elif kind == "handle":
        pattern = rf"(?<![A-Za-z0-9_\-]){re.escape(value)}(?![A-Za-z0-9_\-])"
    else:
        raise ValueError(f"unknown kind {kind!r}; use one of {', '.join(KINDS)}")
    return Identifier(kind, re.compile(pattern, re.IGNORECASE))


def parse_denylist(text: str) -> list[Identifier]:
    identifiers: list[Identifier] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, separator, value = line.partition(":")
        if not separator or not value.strip():
            raise ValueError(f"denylist line {number} is not 'kind: value'")
        identifiers.append(compile_identifier(kind.strip().lower(), value))
    return identifiers


def load_denylist() -> list[Identifier]:
    """From the environment first (CI), then the gitignored file (a developer's machine)."""
    text = os.environ.get(DENYLIST_ENV)
    if text is None and DENYLIST_FILE.is_file():
        text = DENYLIST_FILE.read_text(encoding="utf-8")
    if text is None:
        raise LookupError(
            f"no denylist: set {DENYLIST_ENV} (in CI, a repository secret) or create "
            f"{DENYLIST_FILE.relative_to(REPO_ROOT).as_posix()} \u2014 "
            "see this file's docstring for the format"
        )
    identifiers = parse_denylist(text)
    if not identifiers:
        raise LookupError("the denylist is empty, so this check would pass without checking anything")
    return identifiers


def tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607  (git is expected on PATH)
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return [name for name in output.decode("utf-8").split("\0") if name]


def scan(paths: Iterable[str], identifiers: Sequence[Identifier], root: Path = REPO_ROOT) -> list[str]:
    """``path:line: kind`` for every match, values withheld."""
    findings: list[str] = []
    for name in paths:
        for identifier in identifiers:
            if identifier.pattern.search(name):
                findings.append(f"{name}: a denylisted {identifier.kind} in the file's path")
        path = root / name
        if name == ".env" or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary: a PDF's text is compressed, and the fixtures are synthetic by rule
        text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            kinds = sorted({identifier.kind for identifier in identifiers if identifier.pattern.search(line)})
            findings.extend(f"{name}:{number}: a denylisted {kind}" for kind in kinds)
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        identifiers = load_denylist()
    except (LookupError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    paths = arguments or tracked_files()
    findings = scan(paths, identifiers, root=REPO_ROOT)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(
            "a real person's identifier is in the repository. Test data and examples use the "
            "synthetic identity (Jordan A. Rivera, jordan.rivera@example.com, (555) 014-2288).",
            file=sys.stderr,
        )
        return 1
    print(f"no denylisted identifier in {len(paths)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
