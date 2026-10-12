"""Driving the worker from the API.

:class:`SubprocessDocumentParser` satisfies the ``DocumentParser`` protocol the upload
pipeline depends on, and runs each file in its own short-lived process.

Two parsers run the worker out of the API's process. :class:`DockerDocumentParser`,
the default, runs it in a container with no network, a read-only filesystem, no
capabilities and a memory ceiling. :class:`SubprocessDocumentParser` runs it as a
plain process, bounded by the worker's own limits but not contained, and is allowed
only in dev and CI. Both share the details that make the isolation real rather than
nominal:

**The timeout kills.** ``asyncio.wait_for`` on its own would leave the process running
and the pipe open; this terminates the child, then kills it if termination is ignored,
and only then reports the timeout. For a container, that means ``docker kill``: ending
the docker client leaves the container running.

**The output is bounded.** stdout and stderr are each read up to a limit, so a worker
subverted into writing without end cannot fill the API's memory.

**The worker inherits nothing.** An empty working directory and an environment with
none of the API's settings in it — no database URL, no token, no key path.

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
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass

import structlog
from pydantic import ValidationError

from aria_core.documents import LayoutDocument
from aria_document_parser.errors import ParseFailed, ParseRefused
from aria_document_parser.limits import ParseLimits
from aria_document_parser.worker import parse_bytes

__all__ = [
    "DockerDocumentParser",
    "InProcessDocumentParser",
    "ParsedDocument",
    "SubprocessDocumentParser",
    "docker_run_command",
    "worker_environment",
]

logger = structlog.get_logger(__name__)

WORKER_MODULE = "aria_document_parser.worker"


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """What crossed back from the worker: text, notes, and what to do about them.

    ``needs_review`` is the parser saying a person should look. Two things set it: an
    independent extractor reading the lines in a different order (RESUME_ENGINE §5.6),
    and a page laid out in columns. The gutter settles which column comes first, but
    not whether a sidebar of skills belongs where it landed, so that is worth a look
    too. Guessing silently is how a resume ends up saying something nobody wrote.
    """

    text: str
    detail: str = ""
    #: URIs from the file's link annotations. Data only: nothing fetches these.
    links: tuple[str, ...] = ()
    #: Which extractor's text this is, recorded so a surprising import can be explained.
    extractor: str = ""
    #: 1-based pages where the geometry found columns and the text was rebuilt.
    column_pages: tuple[int, ...] = ()
    needs_review: bool = False
    #: Stage 0's reading of a PDF, validated on arrival; None for other types.
    layout: LayoutDocument | None = None


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
        column_pages=_int_tuple(result.get("column_pages")),
        needs_review=bool(result.get("needs_review", False)),
        layout=_layout(result.get("layout")),
    )


def _layout(value: object) -> LayoutDocument | None:
    """The worker's layout, validated as untrusted input from another process.

    Unlike the optional fields above, a layout that does not validate is a failure,
    not an empty value: the importer would read the document from it, and a malformed
    one is what a subverted worker would send.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ParseFailed("the parser sent a layout that is not JSON text")
    try:
        return LayoutDocument.model_validate_json(value)
    except ValidationError as error:
        raise ParseFailed(
            f"the parser sent a layout that does not validate ({error.error_count()} errors)"
        ) from None


#: Environment variables a worker process is given. Everything else the API holds —
#: database URLs, tokens, key paths — stays behind, because the environment is the
#: first thing a subverted extractor would read. SYSTEMROOT is what Windows needs to
#: start Python at all; the temporary directory is pointed at the worker's own.
_PASSED_ENVIRONMENT = ("SYSTEMROOT", "LANG", "LC_ALL")


def worker_environment(workdir: str) -> dict[str, str]:
    """The whole environment a plain-process worker sees."""
    passed = {key: value for key, value in os.environ.items() if key.upper() in _PASSED_ENVIRONMENT}
    return {**passed, "TEMP": workdir, "TMP": workdir, "TMPDIR": workdir}


def docker_run_command(image: str, name: str, detected_type: str, limits: ParseLimits) -> list[str]:
    """The ``docker run`` that parses one file. Every flag is a limit; see SECURITY.md §7.

    No ``-e`` and no volume: the container receives the file on stdin and nothing else
    from this machine. ``--pull never`` keeps a parse from ever reaching a registry —
    the image is built locally and its fingerprint checked at start-up.
    """
    memory = f"{limits.max_memory_bytes}b"
    return [
        "docker", "run", "--rm", "--interactive",
        "--name", name,
        "--pull", "never",
        "--network", "none",
        "--read-only",
        # A small scratch area inside the container, which is otherwise read-only;
        # noexec, so nothing written there can be run.
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",  # noqa: S108
        "--memory", memory,
        "--memory-swap", memory,
        "--pids-limit", str(limits.max_processes),
        "--cpus", "1",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--ulimit", "core=0",
        "--ipc", "none",
        image,
        detected_type,
    ]  # fmt: skip


#: How long to wait for a stopped worker's pipes to close before giving up on it.
_STOP_GRACE_SECONDS = 15.0


@dataclass(slots=True)
class _Exchange:
    """What came back from one worker, and whether it had to be stopped."""

    stdout: bytes = b""
    stderr: bytes = b""
    stopped_because: str = ""


class _IsolatedParser:
    """Runs the worker somewhere else and reads one JSON result back."""

    def __init__(self, limits: ParseLimits | None = None) -> None:
        self._limits = limits or ParseLimits()

    async def _start(self, detected_type: str, workdir: str) -> asyncio.subprocess.Process:
        raise NotImplementedError

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        """Terminate, then kill. A timeout that leaves the process running is not one."""
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.kill()

    def _forget(self, process: asyncio.subprocess.Process) -> None:
        """Drop anything kept about a process once its parse is over."""

    async def _exchange(self, process: asyncio.subprocess.Process, content: bytes) -> _Exchange:
        """Feed the file, read both streams to the end, and stop the worker if it misbehaves.

        The readers always read to EOF and discard past the cap rather than stopping:
        asyncio's ``Process.wait()`` returns only once every pipe has closed, and a pipe
        left full and unread never does. A breach of the output cap and the timeout both
        *trigger* the stop; the worker's death is what ends the reading.
        """
        stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
        assert stdin is not None and stdout is not None and stderr is not None
        result = _Exchange()
        stopping = asyncio.Event()
        cap = self._limits.max_output_bytes

        def stop(reason: str) -> None:
            if not result.stopped_because:
                result.stopped_because = reason
                stopping.set()

        async def feed() -> None:
            try:
                stdin.write(content)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the worker stopped reading; its output says why
            finally:
                stdin.close()

        async def read(stream: asyncio.StreamReader) -> bytes:
            kept: list[bytes] = []
            total = 0
            while chunk := await stream.read(65536):
                total += len(chunk)
                if total > cap:
                    stop("output")
                    continue  # keep draining so the pipe can close
                kept.append(chunk)
            return b"".join(kept)

        async def timer() -> None:
            await asyncio.sleep(self._limits.timeout_seconds)
            stop("timeout")

        async def stopper() -> None:
            await stopping.wait()
            await self._stop(process)

        timing = asyncio.create_task(timer())
        stopping_task = asyncio.create_task(stopper())
        try:
            _, result.stdout, result.stderr = await asyncio.wait_for(
                asyncio.gather(feed(), read(stdout), read(stderr)),
                timeout=self._limits.timeout_seconds + _STOP_GRACE_SECONDS,
            )
            await asyncio.wait_for(process.wait(), timeout=_STOP_GRACE_SECONDS)
        except TimeoutError:
            # Stopped and still not finished: nothing more can be done from here.
            stop("timeout")
            await self._stop(process)
            raise ParseFailed("the parser could not be stopped cleanly after its time ran out") from None
        finally:
            timing.cancel()
            if stopping.is_set():
                await stopping_task  # let a stop in progress finish
            else:
                stopping_task.cancel()
        return result

    async def parse(self, content: bytes, detected_type: str) -> ParsedDocument:
        with tempfile.TemporaryDirectory(prefix="aria-parse-") as workdir:
            process = await self._start(detected_type, workdir)
            try:
                exchange = await self._exchange(process, content)
            finally:
                self._forget(process)

        if exchange.stopped_because == "timeout":
            logger.warning(
                "parse_timed_out", detected_type=detected_type, timeout_seconds=self._limits.timeout_seconds
            )
            raise ParseFailed(
                f"parsing did not finish within {self._limits.timeout_seconds:.0f}s and was "
                "stopped. The file may be built in a way that makes extraction loop."
            )
        if exchange.stopped_because == "output":
            logger.warning("parse_output_too_large", detected_type=detected_type)
            raise ParseFailed(
                f"the parser wrote more than {self._limits.max_output_bytes} bytes and was "
                "stopped. No resume produces that much text."
            )

        stdout, stderr = exchange.stdout, exchange.stderr
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


class SubprocessDocumentParser(_IsolatedParser):
    """Parses each file in its own local process: bounded, but not contained.

    The worker caps its own memory, CPU time and process count, and is given an empty
    working directory and an environment with nothing of the API's in it. What a plain
    process cannot be denied is the filesystem and the network, so outside tests this
    runs only when chosen explicitly in dev or CI (``ARIA_PARSER_ISOLATION=process``).
    """

    def _command(self, detected_type: str) -> list[str]:
        # -I: ignore PYTHON* variables and the user's site-packages, so nothing
        # outside the installed packages can be imported into the worker.
        return [sys.executable, "-I", "-m", WORKER_MODULE, detected_type]

    async def _start(self, detected_type: str, workdir: str) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *self._command(detected_type),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=worker_environment(workdir),
        )  # fmt: skip


class DockerDocumentParser(_IsolatedParser):
    """Parses each file in its own container: no network, read-only, no capabilities.

    The default isolation (SECURITY.md §7). Some extractors are native code reading
    hostile bytes; if one is exploited, the attacker holds a process that can reach no
    network, write no file, see none of this machine's files or environment and start
    only a handful of tasks, inside a memory ceiling — and the worker's own limits
    still apply within that.
    """

    def __init__(self, image: str, limits: ParseLimits | None = None) -> None:
        super().__init__(limits)
        self._image = image
        self._names: dict[int, str] = {}

    def _command(self, name: str, detected_type: str) -> list[str]:
        return docker_run_command(self._image, name, detected_type, self._limits)

    async def _start(self, detected_type: str, workdir: str) -> asyncio.subprocess.Process:
        name = f"aria-parse-{uuid.uuid4().hex}"
        process = await asyncio.create_subprocess_exec(
            *self._command(name, detected_type),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )
        self._names[id(process)] = name
        return process

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        # Killing the docker client does not stop the container; killing the
        # container does, and the client then exits on its own.
        name = self._names.get(id(process))
        if name is not None:
            killer = await asyncio.create_subprocess_exec(
                "docker", "kill", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            await killer.wait()
        await super()._stop(process)

    def _forget(self, process: asyncio.subprocess.Process) -> None:
        self._names.pop(id(process), None)


class InProcessDocumentParser:
    """Parses in this process. For tests, and never for an upload from a real user.

    It skips the isolation, which is the point of the subprocess, so the API builds
    the subprocess parser and this one is only ever constructed by a test.
    """

    def __init__(self, limits: ParseLimits | None = None) -> None:
        self._limits = limits or ParseLimits()

    async def parse(self, content: bytes, detected_type: str) -> ParsedDocument:
        return _result_to_document(parse_bytes(content, detected_type, self._limits))
