"""Controller-owned process trees. A phase cannot outlive its supervisor."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path


class ProcessSupervisionError(RuntimeError):
    """Containment or process closure could not be proven; never retry blindly."""


def run_supervised(
    command: list[str], *, cwd: Path | str | None = None, timeout: float | None = None,
    env: dict[str, str] | None = None, input: str | None = None,
    stdout=None, stderr=None, text: bool = True, encoding: str = "utf-8", errors: str = "replace",
) -> subprocess.CompletedProcess[str]:
    """Run with file-backed output, killing remaining descendants on every exit path.

    Input is file-backed too: a worker that never reads stdin cannot defeat the timeout.
    TimeoutExpired carries partial captured output for the validation evidence channel.
    """
    if not text:
        raise ValueError("The Forge process supervisor requires text output.")
    with ExitStack() as stack:
        source = stack.enter_context(tempfile.TemporaryFile())
        if input is not None:
            source.write(input.encode(encoding, errors))
        source.seek(0)
        out = stdout if stdout is not None else stack.enter_context(tempfile.TemporaryFile())
        err = stderr if stderr is not None else stack.enter_context(tempfile.TemporaryFile())

        def captured(handle, supplied):
            handle.flush()
            if supplied is not None:
                return None
            handle.seek(0)
            return handle.read().decode(encoding, errors)

        try:
            if os.name == "nt":
                code = _windows_run(command, cwd, env, source, out, err, timeout)
            else:
                code = _posix_run(command, cwd, env, source, out, err, timeout)
        except subprocess.TimeoutExpired as exc:
            exc.output = captured(out, stdout)
            exc.stderr = captured(err, stderr)
            raise
        return subprocess.CompletedProcess(command, code, captured(out, stdout), captured(err, stderr))


def _posix_run(command, cwd, env, source, out, err, timeout):
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=source, stdout=out, stderr=err, start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        deadline = time.monotonic() + 10
        while _posix_group_running(process.pid):
            if time.monotonic() >= deadline:
                raise ProcessSupervisionError("Process group did not terminate.")
            time.sleep(0.02)


def _posix_group_running(group: int) -> bool:
    # Zombies cannot execute; waiting for init to reap them can otherwise block forever.
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
                if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def _windows_run(command, cwd, env, source, out, err, timeout):
    import _winapi
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class Accounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in (
            "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime",
        )] + [(name, wintypes.DWORD) for name in (
            "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses",
        )]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    definitions = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL,
        ),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
    }
    for name, (args, result) in definitions.items():
        getattr(api, name).argtypes = args
        getattr(api, name).restype = result

    def checked(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    job = api.CreateJobObjectW(None, None)
    checked(job)
    process_handle = thread_handle = None
    handles = []
    assigned = False
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway flags.
        checked(api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        current = _winapi.GetCurrentProcess()
        for stream in (source, out, err):
            handles.append(_winapi.DuplicateHandle(
                current, msvcrt.get_osfhandle(stream.fileno()), current, 0, True, _winapi.DUPLICATE_SAME_ACCESS,
            ))
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
        startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
        # Restrict inheritance to these streams, including under concurrent worker launches.
        startup.lpAttributeList = {"handle_list": handles}
        process_handle, thread_handle, pid, _ = _winapi.CreateProcess(
            None, subprocess.list2cmdline(command), None, None, True,
            subprocess.CREATE_NO_WINDOW | 0x4,  # CREATE_SUSPENDED
            env, str(cwd) if cwd is not None else None, startup,
        )
        checked(api.AssignProcessToJobObject(job, process_handle))
        assigned = True
        if api.ResumeThread(thread_handle) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = None if timeout is None else time.monotonic() + timeout
        while _winapi.WaitForSingleObject(process_handle, 20) == _winapi.WAIT_TIMEOUT:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
        return _winapi.GetExitCodeProcess(process_handle)
    finally:
        try:
            if assigned:
                checked(api.TerminateJobObject(job, 124))
                deadline = time.monotonic() + 10
                while True:
                    info = Accounting()
                    checked(api.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None))
                    if not info.ActiveProcesses:
                        break
                    if time.monotonic() >= deadline:
                        raise ProcessSupervisionError("Windows job did not terminate.")
                    time.sleep(0.02)
            elif process_handle is not None:
                # Assignment failed: the process is still suspended and has executed nothing.
                _winapi.TerminateProcess(process_handle, 126)
            if process_handle is not None:
                _winapi.WaitForSingleObject(process_handle, _winapi.INFINITE)
        except OSError as exc:
            raise ProcessSupervisionError("Process tree closure could not be proven.") from exc
        finally:
            for handle in [thread_handle, process_handle, *handles, job]:
                if handle is not None:
                    _winapi.CloseHandle(handle)
