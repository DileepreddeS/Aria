"""The upload pipeline (SECURITY.md §7).

The parser is built here so the rest of the API never has the choice: uploads from
real users go through the subprocess worker, and the in-process parser exists only for
tests that should not pay for a process per case.
"""

from __future__ import annotations

from aria_api.uploads.pipeline import DocumentParser
from aria_document_parser.client import SubprocessDocumentParser
from aria_document_parser.limits import ParseLimits

__all__ = ["build_document_parser"]


def build_document_parser(limits: ParseLimits | None = None) -> DocumentParser:
    """The parser the API uses: isolated, with a timeout that kills."""
    return SubprocessDocumentParser(limits)
