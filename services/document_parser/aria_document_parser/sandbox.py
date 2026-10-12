"""The limits the parse worker imposes on itself before it reads any input.

Some extractors are native code reading hostile bytes, and a bug in one is an attacker
running code inside this process. In normal operation the worker runs in a container
with no network, a read-only filesystem and a memory ceiling (see client.py), and these
limits are defence in depth inside it. Where the worker runs as a plain process — tests,
and development when chosen explicitly — they are the only bound, so they apply in both.

What is limited, and why each matters if the extractor is subverted:

* **Memory**, so a decompression bomb fails here rather than exhausting the host.
* **CPU time**, so a loop ends even if the parent that was timing it has gone.
* **New processes: none.** A parser has no reason to start one, and an exploit that
  wants a shell is the main reason anything would.
* **File writes: none** (Linux). The worker reads stdin and writes stdout, which are
  pipes; nothing it does legitimately touches a file.
* **Core dumps: none**, because a core dump of this process is a copy of the resume.

If a limit cannot be applied the worker refuses to parse. A sandbox that quietly did
not happen is worse than none, because everyone downstream believes it did.
"""

from __future__ import annotations

import sys

from aria_document_parser.limits import ParseLimits

__all__ = ["SandboxUnavailable", "apply_worker_limits"]


class SandboxUnavailable(RuntimeError):
    """A limit could not be applied, so the worker must not parse."""


def apply_worker_limits(limits: ParseLimits) -> None:
    """Bound this process. Call once, before reading the file."""
    _apply_limits(limits)


#: Job handles kept open for the life of the process; see _apply_limits on Windows.
_JOB_HANDLES: list[object] = []

# One definition per platform, chosen at import. Written this way rather than as a
# branch inside one function so the type checker reads only the platform it checks.
if sys.platform == "win32":

    def _apply_limits(limits: ParseLimits) -> None:
        """Put this process in a Job Object that bounds memory, CPU time and processes.

        A process may assign itself to a job (nested jobs are supported from Windows 8),
        and nothing has been read yet, so there is no window in which the file is parsed
        unbounded. The job handle is kept open for the life of the process: closing it
        with KILL_ON_JOB_CLOSE set would end the process, which is the right failure if
        the handle were ever lost.
        """
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job_object_limit_process_time = 0x00000002
        job_object_limit_active_process = 0x00000008
        job_object_limit_process_memory = 0x00000100
        job_object_limit_die_on_unhandled_exception = 0x00000400
        job_object_limit_kill_on_job_close = 0x00002000
        job_object_extended_limit_information = 9

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]  # fmt: skip
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise SandboxUnavailable(f"CreateJobObject failed: error {ctypes.get_last_error()}")

        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = (
            job_object_limit_process_time
            | job_object_limit_active_process
            | job_object_limit_process_memory
            | job_object_limit_die_on_unhandled_exception
            | job_object_limit_kill_on_job_close
        )
        # In 100-nanosecond units.
        info.BasicLimitInformation.PerProcessUserTimeLimit = limits.max_cpu_seconds * 10_000_000
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = limits.max_memory_bytes

        if not kernel32.SetInformationJobObject(
            job, job_object_extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise SandboxUnavailable(f"SetInformationJobObject failed: error {ctypes.get_last_error()}")
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            raise SandboxUnavailable(f"AssignProcessToJobObject failed: error {ctypes.get_last_error()}")

        # Kept for the life of the process; see the docstring.
        _JOB_HANDLES.append(job)

else:

    def _apply_limits(limits: ParseLimits) -> None:
        import resource

        wanted = {
            resource.RLIMIT_AS: limits.max_address_space_bytes,
            resource.RLIMIT_CPU: limits.max_cpu_seconds,
            # Processes are counted per user, so 0 means "no new process" for this
            # worker; the ones already running are unaffected.
            resource.RLIMIT_NPROC: 0,
            resource.RLIMIT_FSIZE: 0,
            resource.RLIMIT_CORE: 0,
        }
        for which, value in wanted.items():
            try:
                _soft, hard = resource.getrlimit(which)
                ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
                resource.setrlimit(which, (ceiling, ceiling))
            except (OSError, ValueError) as error:
                raise SandboxUnavailable(f"could not set resource limit {which}: {error}") from error
