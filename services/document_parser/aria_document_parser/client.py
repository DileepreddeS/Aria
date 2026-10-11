"""Driving the worker from the API.

:class:`SubprocessDocumentParser` satisfies the ``DocumentParser`` protocol the upload
pipeline depends on, and runs each file in its own short-lived process.

Three details that make the isolation real rather than nominal:

**The timeout kills.** ``asyncio.wait_for`` on its own would leave the process running
and the pipe open; this terminates the child, then kills it if termination is ignored,
and only then reports the timeout.

**stdout carries the result and nothing else.** pdfminer writes warnings about
real-world PDFs; those go to stderr, are collected, and are recorded as detail rather
than being allowed to corrupt the JSON.

**A refusal is a value, not an exception across the boundary.** The worker reports
``refused`` with advice for the candidate, and the client turns that back into a
:class:`~aria_document_parser.errors.ParseRefused` on this side. A crashed worker and a
refused file are then impossible to confuse.

:class:`InProcessDocumentParser` exists for tests, which should not pay for a
subprocess per case. Tests that specifically cover the isolation use the real one.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass

import structlog

from aria_document_parser.errors import ParseFailed, ParseRefused
from aria_document_parser.limits import ParseLimits
from aria_document_parser.worker import parse_bytes

__all__ = ["InProcessDocumentParser", "ParsedDocument", "SubprocessDocumentParser"]

logger = structlog.get_logger(__name__)

WORKER_MODULE = "aria_document_parser.worker"


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """What crossed back from the worker: text, notes, and what to do about them.

    ``needs_review`` is the parser saying a person should look. Two things set it: a
    page the two extractors disagreed about, and a page whose columns were found by
    geometry and whose text was rebuilt in reading order. The second is not a
    disagreement — the gutter settles which column comes first — but the gutter does
    not settle whether a sidebar of skills belongs where it landed, so it is still
    worth a look. The importer imports either way and names the pages to check;
    guessing silently is how a resume ends up saying something nobody wrote.
    """

    text: str
    detail: str = ""
    #: URIs from the file's link annotations. Data only: nothing fetches these.
    links: tuple[str, ...] = ()
    #: Which extractor's text this is, recorded so a surprising import can be explained.
    extractor: str = ""
    #: 1-based pages where the extractors disagreed about reading order.
    low_agreement_pages: tuple[int, ...] = ()
    #: 1-based pages where the geometry found columns and the text was rebuilt.
    column_pages: tuple[int, ...] = ()
    needs_review: bool = False


def _string_tuple(value: object) -> tuple[str, ...]:
    """A tuple of strings from an unknown JSON value, or empty.

    The worker's output is JSON from another process, so nothing in it is typed until
    it is checked. A malformed field degrades to empty rather than raising here: the
    text is what matters, and losing a link list is not worth losing the parse.
    """
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _int_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    numbers: list[int] = []
    for item in value:
        try:
            numbers.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(numbers)


def _result_to_document(result: dict[str, object], *, stderr: str = "") -> ParsedDocument:
    status = result.get("status")
    if status == "refused":
        raise ParseRefused(str(result.get("reason", "refused")), str(result.get("advice", "")))
    if status == "failed":
        raise ParseFailed(str(result.get("detail", "the parser failed without saying why")))
    if status != "parsed":
        raise ParseFailed(f"the worker returned an unknown status {status!r}")

    detail = str(result.get("detail", ""))
    if result.get("has_header_or_footer_text"):
        detail = f"{detail}; header/footer text present"
    if stderr.strip():
        # Kept, truncated: a library's complaints about a PDF are often the clue when
        # an import comes out wrong.
        detail = f"{detail}; parser notes: {stderr.strip()[:300]}"

    return ParsedDocument(
        text=str(result.get("text", "")),
        detail=detail,
        links=_string_tuple(result.get("links")),
        extractor=str(result.get("extractor", "")),
        low_agreement_pages=_int_tuple(result.get("low_agreement_pages")),
        column_pages=_int_tuple(result.get("column_pages")),
        needs_review=bool(result.get("needs_review", False)),
    )


class SubprocessDocumentParser:
    """Parses each file in its own process, with a timeout that kills."""

    def __init__(self, limits: ParseLimits | None = None) -> None:
        self._limits = limits or ParseLimits()

    async def parse(self, content: bytes, detected_type: str) -> ParsedDocument:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            WORKER_MODULE,
            detected_type,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=content), timeout=self._limits.timeout_seconds
            )
        except TimeoutError:
            await self._stop(process)
            logger.warning(
                "parse_timed_out",
                detected_type=detected_type,
                timeout_seconds=self._limits.timeout_seconds,
            )
            raise ParseFailed(
                f"parsing did not finish within {self._limits.timeout_seconds:.0f}s and was "
                "stopped. The file may be built in a way that makes extraction loop."
            ) from None

        if process.returncode != 0 and not stdout:
            raise ParseFailed(
                f"the parser exited with code {process.returncode}: {stderr.decode('utf-8', 'replace')[:300]}"
            )

        line = stdout.decode("utf-8", "replace").strip().splitlines()
        if not line:
            raise ParseFailed("the parser produced no output")

        try:
            result = json.loads(line[-1])
        except json.JSONDecodeError as error:
            raise ParseFailed(f"the parser's output was not JSON: {line[-1][:200]!r}") from error

        return _result_to_document(result, stderr=stderr.decode("utf-8", "replace"))

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        """Terminate, then kill. A timeout that leaves the process running is not one."""
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.kill()
            await process.wait()


class InProcessDocumentParser:
    """Parses in this process. For tests, and never for an upload from a real user.

    It skips the isolation, which is the point of the subprocess, so the API builds
    the subprocess parser and this one is only ever constructed by a test.
    """

    def __init__(self, limits: ParseLimits | None = None) -> None:
        self._limits = limits or ParseLimits()

    async def parse(self, content: bytes, detected_type: str) -> ParsedDocument:
        return _result_to_document(parse_bytes(content, detected_type, self._limits))
