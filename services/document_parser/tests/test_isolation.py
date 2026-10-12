"""The parse sandbox holds: each limit is shown to bite, not just to be configured.

SECURITY.md §7 asks for a sandboxed worker, and some extractors are native code
reading hostile bytes. A flag in a command line proves nothing on its own, so these
tests run probe code *in the worker's place* — same environment, same working
directory, same `docker run` flags, only the program swapped — and check from inside
that the network is gone, the filesystem is read-only, memory and processes are
capped, and nothing of the API's environment arrived.

Tests marked ``docker`` need Docker and a current parser image
(``./tasks.ps1 parser-image``). In CI a missing image is a failure; elsewhere it is a
skip that says what to run.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from fixtures import make_pdf  # type: ignore[import-not-found]  # sibling helper, see conftest

from aria_document_parser import worker
from aria_document_parser.client import (
    DockerDocumentParser,
    InProcessDocumentParser,
    SubprocessDocumentParser,
    docker_run_command,
    worker_environment,
)
from aria_document_parser.errors import ParseFailed
from aria_document_parser.image import DEFAULT_IMAGE, image_status
from aria_document_parser.limits import ParseLimits
from aria_document_parser.sandbox import SandboxUnavailable

# ------------------------------------------------------------------ the probes
# Each prints one result line in the worker's format, with its observations as text.

_REPORT = 'print(json.dumps({"status": "parsed", "text": json.dumps(seen)}))'

ENVIRONMENT_PROBE = f"""
import json, os
seen = {{
    "aria": sorted(k for k in os.environ if k.upper().startswith("ARIA_")),
    "cwd_entries": sorted(os.listdir(".")),
}}
{_REPORT}
"""

#: Asks for 3 GiB — past every ceiling here — and for a child process, after applying
#: the worker's own limits exactly as the worker does.
LIMITS_PROBE = f"""
import json, subprocess, sys
from aria_document_parser.limits import ParseLimits
from aria_document_parser.sandbox import apply_worker_limits
apply_worker_limits(ParseLimits())
seen = {{}}
try:
    hoard = bytearray(3 * 1024 * 1024 * 1024)
    seen["big_allocation"] = "allowed"
except MemoryError:
    seen["big_allocation"] = "refused"
try:
    subprocess.run([sys.executable, "-c", "pass"], check=True, capture_output=True)
    seen["child_process"] = "allowed"
except OSError:
    seen["child_process"] = "refused"
{_REPORT}
"""

OUTPUT_FLOOD_PROBE = """
import sys
sys.stdout.write("x" * (20 * 1024 * 1024))
sys.stdout.flush()
"""

SLEEP_PROBE = "import time\ntime.sleep(60)\n"

CONTAINER_PROBE = f"""
import json, os, socket
seen = {{"aria": sorted(k for k in os.environ if k.upper().startswith("ARIA_"))}}
seen["uid"] = os.getuid()
status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
seen["capabilities"] = status["CapEff"].strip()
seen["no_new_privileges"] = status["NoNewPrivs"].strip()
seen["interfaces"] = sorted(os.listdir("/sys/class/net"))
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    seen["outbound"] = "connected"
except OSError:
    seen["outbound"] = "refused"
# The mount itself, not a write attempt: as nobody, a write to / fails on permissions
# alone, so a failed write would not show the filesystem is read-only.
root = [line.split() for line in open("/proc/mounts") if line.split()[1] == "/"][-1]
seen["root_mount_options"] = root[3].split(",")
tmp = [line.split() for line in open("/proc/mounts") if line.split()[1] == "/tmp"]
seen["tmp_mount_options"] = tmp[-1][3].split(",") if tmp else []
seen["memory_max"] = open("/sys/fs/cgroup/memory.max").read().strip()
seen["pids_max"] = open("/sys/fs/cgroup/pids.max").read().strip()
{_REPORT}
"""


class ProbeProcess(SubprocessDocumentParser):
    """The real plain-process parser with the program swapped for a probe."""

    def __init__(self, code: str, limits: ParseLimits | None = None) -> None:
        super().__init__(limits)
        self._code = code

    def _command(self, detected_type: str) -> list[str]:
        return [sys.executable, "-I", "-c", self._code]


class ProbeContainer(DockerDocumentParser):
    """The real container parser with the program swapped for a probe."""

    def __init__(self, image: str, code: str, limits: ParseLimits | None = None) -> None:
        super().__init__(image, limits)
        self._code = code
        self.started: list[str] = []

    def _command(self, name: str, detected_type: str) -> list[str]:
        self.started.append(name)
        command = super()._command(name, detected_type)
        image_and_argument = command[-2:]
        assert image_and_argument[1] == detected_type
        return [*command[:-2], "--entrypoint", "python", image_and_argument[0], "-I", "-c", self._code]


async def _observe(parser: SubprocessDocumentParser) -> dict[str, Any]:
    result = await parser.parse(b"", "txt")
    observed: dict[str, Any] = json.loads(result.text)
    return observed


# ------------------------------------------------------------- the worker itself


class TestTheWorkerBoundsItselfFirst:
    def test_limits_are_applied_before_a_byte_of_input_is_read(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        order: list[str] = []
        monkeypatch.setattr(worker, "apply_worker_limits", lambda limits: order.append("limits"))

        class Input:
            @property
            def buffer(self) -> io.BytesIO:
                order.append("read")
                return io.BytesIO(b"plain text")

        monkeypatch.setattr(sys, "stdin", Input())
        assert worker.main(["txt"]) == 0
        assert order == ["limits", "read"]

    def test_a_worker_that_cannot_bound_itself_refuses_to_parse(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def unavailable(limits: ParseLimits) -> None:
            raise SandboxUnavailable("no job objects here")

        class Input:
            @property
            def buffer(self) -> io.BytesIO:
                raise AssertionError("the file was read without the limits in place")

        monkeypatch.setattr(worker, "apply_worker_limits", unavailable)
        monkeypatch.setattr(sys, "stdin", Input())
        assert worker.main(["txt"]) == 3
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert result["status"] == "failed"
        assert "sandbox unavailable" in result["detail"]


# ------------------------------------------------------------- a plain process


class TestAPlainProcessInheritsNothing:
    def test_the_worker_runs_in_isolated_mode(self) -> None:
        assert SubprocessDocumentParser()._command("pdf")[1] == "-I"

    def test_the_environment_carries_nothing_of_the_api(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ARIA_DATABASE_URL", "postgresql+asyncpg://aria_app:secret@127.0.0.1/aria")
        monkeypatch.setenv("ARIA_API_SERVICE_TOKEN", "a-token")
        environment = worker_environment(str(tmp_path))
        assert not [key for key in environment if key.upper().startswith("ARIA_")]
        assert environment["TEMP"] == environment["TMP"] == str(tmp_path)

    async def test_from_inside_there_is_no_aria_variable_and_an_empty_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARIA_PROBE_SECRET", "must-not-arrive")
        seen = await _observe(ProbeProcess(ENVIRONMENT_PROBE))
        assert seen["aria"] == []
        assert seen["cwd_entries"] == []

    async def test_memory_and_processes_are_capped(self) -> None:
        seen = await _observe(ProbeProcess(LIMITS_PROBE))
        assert seen == {"big_allocation": "refused", "child_process": "refused"}

    async def test_a_flood_of_output_is_cut_off(self) -> None:
        with pytest.raises(ParseFailed, match="wrote more than"):
            await ProbeProcess(OUTPUT_FLOOD_PROBE).parse(b"", "txt")

    async def test_a_parse_that_never_ends_is_stopped(self) -> None:
        started = time.monotonic()
        with pytest.raises(ParseFailed, match="did not finish"):
            await ProbeProcess(SLEEP_PROBE, ParseLimits(timeout_seconds=1.0)).parse(b"", "txt")
        assert time.monotonic() - started < 10


# ------------------------------------------------------------- the container


class TestTheContainerCommand:
    def test_every_limit_is_on_the_command_line(self) -> None:
        limits = ParseLimits()
        command = docker_run_command("aria-parser:test", "aria-parse-x", "pdf", limits)
        pairs = set(pairwise(command))
        for flag in (
            ("--network", "none"),
            ("--pull", "never"),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges"),
            ("--user", "65534:65534"),
            ("--memory", f"{limits.max_memory_bytes}b"),
            ("--memory-swap", f"{limits.max_memory_bytes}b"),
            ("--pids-limit", str(limits.max_processes)),
            ("--ipc", "none"),
        ):
            assert flag in pairs, flag
        assert "--read-only" in command
        assert command[-2:] == ["aria-parser:test", "pdf"]

    def test_nothing_from_this_machine_is_passed_in(self) -> None:
        command = docker_run_command("aria-parser:test", "aria-parse-x", "pdf", ParseLimits())
        for flag in ("-e", "--env", "--env-file", "-v", "--volume", "--mount", "--privileged", "--device"):
            assert flag not in command, flag
        assert ("--network", "host") not in set(pairwise(command))


@pytest.fixture
def parser_image() -> str:
    image = os.environ.get("ARIA_PARSER_IMAGE", DEFAULT_IMAGE)
    status = image_status(image)
    if not status.current:
        if os.environ.get("ARIA_ENV") == "ci":
            pytest.fail(status.reason)
        pytest.skip(status.reason)
    return image


@pytest.mark.docker
class TestInsideTheContainer:
    async def test_a_resume_parses_the_same_as_in_process(self, parser_image: str) -> None:
        content = make_pdf("Senior engineer. Built the thing. " * 20)
        contained = await DockerDocumentParser(parser_image).parse(content, "pdf")
        in_process = await InProcessDocumentParser().parse(content, "pdf")
        assert contained.text == in_process.text

    async def test_no_network_no_writes_no_privileges_and_nothing_of_ours(
        self, parser_image: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARIA_PROBE_SECRET", "must-not-arrive")
        result = await ProbeContainer(parser_image, CONTAINER_PROBE).parse(b"", "txt")
        seen = json.loads(result.text)
        limits = ParseLimits()
        assert seen["aria"] == []
        assert seen["outbound"] == "refused"
        assert seen["interfaces"] == ["lo"]
        assert "ro" in seen["root_mount_options"]
        assert "noexec" in seen["tmp_mount_options"]
        assert seen["uid"] == 65534
        assert seen["capabilities"] == "0000000000000000"
        assert seen["no_new_privileges"] == "1"
        assert seen["memory_max"] == str(limits.max_memory_bytes)
        assert seen["pids_max"] == str(limits.max_processes)

    async def test_the_workers_own_limits_hold_inside_it_too(self, parser_image: str) -> None:
        result = await ProbeContainer(parser_image, LIMITS_PROBE).parse(b"", "txt")
        assert json.loads(result.text) == {"big_allocation": "refused", "child_process": "refused"}

    async def test_a_flood_of_output_is_cut_off(self, parser_image: str) -> None:
        with pytest.raises(ParseFailed, match="wrote more than"):
            await ProbeContainer(parser_image, OUTPUT_FLOOD_PROBE).parse(b"", "txt")

    async def test_a_timeout_kills_the_container_not_just_the_client(self, parser_image: str) -> None:
        parser = ProbeContainer(parser_image, SLEEP_PROBE, ParseLimits(timeout_seconds=3.0))
        with pytest.raises(ParseFailed, match="did not finish"):
            await parser.parse(b"", "txt")
        (name,) = parser.started
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            listing = await asyncio.create_subprocess_exec(
                "docker", "ps", "--all", "--quiet", "--filter", f"name={name}",
                stdout=asyncio.subprocess.PIPE,
            )  # fmt: skip
            found, _ = await listing.communicate()
            if not found.strip():
                return
            await asyncio.sleep(0.5)
        pytest.fail(f"container {name} was still present 10s after the timeout")
