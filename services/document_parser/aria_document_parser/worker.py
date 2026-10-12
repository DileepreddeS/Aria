"""``python -m aria_document_parser.worker`` — parse one file and exit.

The isolated worker SECURITY.md §7 asks for. It reads the file's bytes on stdin,
writes one JSON object on stdout, and ends. One file per process, so a parser that
corrupts its own memory, leaks, or loops takes nothing with it: the client kills it on
timeout and the operating system reclaims everything.

Before it reads a byte it bounds itself — memory, CPU time, no new processes (see
sandbox.py) — and if that fails it refuses to parse. In normal operation it also runs
inside a container with no network, a read-only filesystem and no capabilities
(client.py, DockerDocumentParser), because some extractors are native code and an
import check cannot stop native code opening a socket. The import check remains, as
the part that is checkable from source.

Failures leave on stdout as JSON too, because a worker that dies silently and one that
refuses a file for a good reason must be distinguishable by the client.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from aria_document_parser.docx import extract_docx
from aria_document_parser.errors import ParseFailed, ParseRefused
from aria_document_parser.limits import ParseLimits
from aria_document_parser.pdf import extract_pdf
from aria_document_parser.plain_text import extract_text_file
from aria_document_parser.sandbox import SandboxUnavailable, apply_worker_limits

__all__ = ["main", "parse_bytes"]

#: Types the worker knows how to read. PNG and JPEG are accepted as uploads but are
#: not parsed: there is no OCR, by decision, so an image resume is refused with an
#: explanation rather than silently producing nothing.
PARSERS = ("pdf", "docx", "txt")


def parse_bytes(content: bytes, detected_type: str, limits: ParseLimits | None = None) -> dict[str, Any]:
    """Parse one file. Returns the JSON-shaped result the client expects."""
    resolved = limits or ParseLimits()

    if detected_type == "pdf":
        extraction = extract_pdf(content, resolved)
        return {
            "status": "parsed",
            "text": extraction.text,
            "detail": extraction.detail,
            "page_count": extraction.page_count,
            "links": list(extraction.links),
            "extractor": extraction.extractor,
            "column_pages": list(extraction.column_pages),
            "needs_review": extraction.needs_review,
            # As a JSON string, so the client validates it in JSON mode: strict types,
            # arrays read as tuples, nothing coerced.
            "layout": extraction.layout.model_dump_json(),
        }
    if detected_type == "docx":
        docx = extract_docx(content, resolved)
        return {
            "status": "parsed",
            "text": docx.text,
            "detail": docx.detail,
            "has_header_or_footer_text": docx.has_header_or_footer_text,
        }
    if detected_type == "txt":
        plain = extract_text_file(content, resolved)
        return {"status": "parsed", "text": plain.text, "detail": plain.detail}

    if detected_type in ("png", "jpeg"):
        raise ParseRefused(
            "image_upload",
            "this is an image, and ARIA does not read text from images. A resume that is "
            "a picture cannot be parsed by the systems it would be sent to either. Please "
            "upload the PDF or DOCX you exported.",
        )

    raise ParseFailed(f"no parser for type {detected_type!r}")


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if len(arguments) != 1:
        print(
            json.dumps({"status": "failed", "detail": "usage: worker.py <detected_type>"}),
            flush=True,
        )
        return 2

    detected_type = arguments[0]
    try:
        apply_worker_limits(ParseLimits())
    except SandboxUnavailable as error:
        # Refuse rather than parse unbounded: everything downstream assumes the
        # limits held.
        print(json.dumps({"status": "failed", "detail": f"sandbox unavailable: {error}"}), flush=True)
        return 3
    content = sys.stdin.buffer.read()

    try:
        result = parse_bytes(content, detected_type)
    except ParseRefused as refusal:
        result = {"status": "refused", "reason": refusal.reason, "advice": refusal.advice}
    except ParseFailed as failure:
        result = {"status": "failed", "detail": failure.detail}
    except Exception as error:  # a parser raising something unforeseen is still data
        result = {"status": "failed", "detail": f"{type(error).__name__}: {error}"}

    # One line of JSON on stdout, nothing else. Library warnings go to stderr, which
    # the client collects separately so a chatty extractor cannot corrupt the result.
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
