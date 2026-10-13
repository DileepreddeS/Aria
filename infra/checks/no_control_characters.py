"""Fail if a text file contains a stray control character.

This exists because it already happened twice, in exactly the same way: a Windows
path written through a shell heredoc turned ``%APPDATA%\\aria`` into
``%APPDATA%<BEL>ria``, because the ``\\a`` was interpreted as an escape before it
reached the file. The result looks correct in a terminal and in a diff — the BEL is
invisible — and the code silently used the wrong path.

That class of bug is unpleasant precisely because review cannot see it. A byte-level
check can.

Allowed: tab, newline, carriage return. Everything else below 0x20, plus DEL, is
rejected with its file, line, column and name, so the message says where to look.

**Non-ASCII characters in Python string literals** are rejected too, with the escape to
write instead. In a character class an en dash and a hyphen look identical and mean
different things, and a literal U+FFFD is indistinguishable from a rendering fault, so
the convention (DECISIONS 2026-10-08) is to write them as escapes such as
``"\\u2013"``. Docstrings and comments keep real punctuation — they are read, not
matched against. The convention went unenforced and was broken twice by a tool that
decodes escapes on the way to the file; this is the enforcement.

Run with paths (pre-commit passes the staged ones) or with none, in which case every
text file git tracks is checked.
"""

from __future__ import annotations

import ast
import io
import subprocess
import sys
import tokenize
import unicodedata
from pathlib import Path

#: Tab, newline and carriage return are how text files are written.
ALLOWED = frozenset({0x09, 0x0A, 0x0D})

#: Extensions worth checking. A binary file is expected to contain control bytes, and
#: listing what is text is safer than guessing what is not.
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".md",
        ".txt",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".cfg",
        ".ini",
        ".sql",
        ".sh",
        ".ps1",
        ".mako",
        ".example",
        ".html",
        ".css",
        ".js",
        ".ts",
        ".tsx",
    }
)

#: Files with no suffix that are still text.
TEXT_NAMES = frozenset({"Makefile", ".gitignore", ".gitattributes", ".env.example", ".dockerignore"})


def is_text_file(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES


def tracked_text_files() -> list[Path]:
    """Every text file git tracks. Used when no paths are given."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607  (git is expected on PATH)
        capture_output=True,
        check=True,
        text=True,
    )
    return [Path(name) for name in result.stdout.split("\0") if name and is_text_file(Path(name))]


def describe(byte: int) -> str:
    """A name a reader can act on: "BEL (0x07)" rather than a mystery."""
    try:
        name = unicodedata.name(chr(byte))
    except ValueError:
        name = "CONTROL"
    return f"{name} (0x{byte:02x})"


def check(path: Path) -> list[str]:
    """Findings for one file, as ``path:line:column: ...`` strings."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        return [f"{path}: could not be read ({error.__class__.__name__})"]

    findings: list[str] = []
    line = 1
    column = 1
    for byte in raw:
        if byte == 0x0A:
            line += 1
            column = 1
            continue
        if (byte < 0x20 and byte not in ALLOWED) or byte == 0x7F:
            findings.append(f"{path}:{line}:{column}: stray control character {describe(byte)}")
        column += 1
    return findings


def _docstring_starts(tree: ast.AST) -> set[tuple[int, int]]:
    """Where each docstring literal starts, as (line, column)."""
    starts: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    starts.add((first.lineno, first.col_offset))
    return starts


def _escape(character: str) -> str:
    code = ord(character)
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def literal_findings(path: Path) -> list[str]:
    """Non-ASCII characters inside a .py file's string literals, docstrings excepted."""
    if path.suffix not in (".py", ".pyi"):
        return []
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        docstrings = _docstring_starts(ast.parse(source))
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError):
        return []  # not this check's business; the interpreter and the linter report it
    # f-strings are tokenized in parts from Python 3.12; their literal text is FSTRING_MIDDLE.
    literal_kinds = {tokenize.STRING, tokenize.FSTRING_MIDDLE}
    findings: list[str] = []
    for token in tokens:
        if token.type not in literal_kinds or token.string.isascii():
            continue
        if token.type == tokenize.STRING and token.start in docstrings:
            continue
        line, column = token.start
        for offset, character in enumerate(token.string):
            if ord(character) > 127:
                try:
                    name = unicodedata.name(character)
                except ValueError:
                    name = "UNNAMED"
                findings.append(
                    f"{path}:{line}:{column + offset + 1}: non-ASCII {name} in a string literal; "
                    f"write {_escape(character)}"
                )
    return findings


def main(argv: list[str]) -> int:
    paths = [Path(argument) for argument in argv] or tracked_text_files()
    candidates = [path for path in paths if path.is_file() and is_text_file(path)]

    findings = [finding for path in candidates for finding in check(path)]
    literals = [finding for path in candidates for finding in literal_findings(path)]
    if not findings and not literals:
        print(f"no stray control characters in {len(candidates)} text file(s)")
        return 0
    if literals:
        print("Non-ASCII characters in Python string literals; write them as escapes:", file=sys.stderr)
        for finding in literals:
            print(f"  {finding}", file=sys.stderr)
        if not findings:
            return 1

    print("Stray control characters found. These are invisible in a diff:", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "\nA literal control byte in source is almost always an escape that was "
        "interpreted too early \u2014 a Windows path through a shell heredoc is the usual "
        "cause. Write the file with an editor or a Python script rather than a heredoc.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
