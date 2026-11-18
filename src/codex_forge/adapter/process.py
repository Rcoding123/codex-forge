"""Bounded native child execution, without a shell or an inference dependency.

Windows children start suspended and enter a kill-on-close Job Object before
their first instruction. The POSIX implementation uses a new process group;
that is lifecycle ownership, not protection against a hostile setsid() child.
"""

from __future__ import annotations

import math
import os
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


MAX_STDIN_BYTES = 2 * 1024 * 1024


class _SpawnFailure(OSError):
    def __init__(self, cleanup_verified: bool):
        super().__init__("launch_or_transport_failed")
        self.cleanup_verified = cleanup_verified


@dataclass(frozen=True)
class ProcessOutcome:
    launched: bool
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    output_limit: bool
    cleanup_verified: bool
    error: str | None
    duration_s: float


if os.name == "nt":
    import ctypes
    import msvcrt
    import _winapi
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for _name, _argtypes, _restype in (
        ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                     wintypes.DWORD], wintypes.BOOL),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ):
        _function = getattr(_kernel, _name)
        _function.argtypes = _argtypes
        _function.restype = _restype

    class _WindowsJob:
        def __init__(self):
            self.handle = _kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not _kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())

        def assign(self, process_handle: int) -> None:
            if not _kernel.AssignProcessToJobObject(self.handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())

        def terminate(self) -> None:
            if not _kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

        def empty(self) -> bool:
            accounting = _Accounting()
            if not _kernel.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return accounting.ActiveProcesses == 0

        def close(self) -> None:
            if self.handle:
                _kernel.CloseHandle(self.handle)
                self.handle = None

    class _WindowsProcess:
        def __init__(self, argv: list[str], cwd: Path, env: dict[str, str]):
            self.job = _WindowsJob()
            self.handle = None
            self.stdin = self.stdout = self.stderr = None
            child_fds: list[int] = []
            parent_fds: list[int] = []
            thread_handle = None
            try:
                in_read, in_write = os.pipe()
                child_fds.append(in_read)
                parent_fds.append(in_write)
                out_read, out_write = os.pipe()
                child_fds.append(out_write)
                parent_fds.append(out_read)
                err_read, err_write = os.pipe()
                child_fds.append(err_write)
                parent_fds.append(err_read)
                handles = [msvcrt.get_osfhandle(fd) for fd in child_fds]
                for handle in handles:
                    os.set_handle_inheritable(handle, True)
                startup = subprocess.STARTUPINFO()
                startup.dwFlags = _winapi.STARTF_USESTDHANDLES
                startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
                startup.lpAttributeList = {"handle_list": handles}
                self.handle, thread_handle, self.pid, _ = _winapi.CreateProcess(
                    argv[0], subprocess.list2cmdline(argv), None, None, True,
                    0x4 | subprocess.CREATE_NO_WINDOW, env, str(cwd), startup,
                )
                # No user-mode instruction has run. Ownership failure terminates
                # this suspended process; it must never be resumed as a fallback.
                self.stdin = os.fdopen(in_write, "wb", buffering=0)
                parent_fds.remove(in_write)
                self.stdout = os.fdopen(out_read, "rb", buffering=0)
                parent_fds.remove(out_read)
                self.stderr = os.fdopen(err_read, "rb", buffering=0)
                parent_fds.remove(err_read)
                self.job.assign(self.handle)
                if _kernel.ResumeThread(thread_handle) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException as exc:
                cleanup_verified = True
                try:
                    if self.handle is not None:
                        _winapi.TerminateProcess(self.handle, 1)
                        cleanup_verified = _winapi.WaitForSingleObject(self.handle, 5000) == 0
                except OSError:
                    cleanup_verified = False
                finally:
                    self.close()
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise _SpawnFailure(cleanup_verified) from exc
            finally:
                if thread_handle is not None:
                    _winapi.CloseHandle(thread_handle)
                for fd in child_fds + parent_fds:
                    os.close(fd)

        def poll(self) -> int | None:
            if _winapi.WaitForSingleObject(self.handle, 0) == _winapi.WAIT_TIMEOUT:
                return None
            return _winapi.GetExitCodeProcess(self.handle)

        def terminate_tree(self) -> None:
            self.job.terminate()

        def empty(self) -> bool:
            return self.job.empty()

        def close(self) -> None:
            self.job.close()
            for stream in (self.stdin, self.stdout, self.stderr):
                if stream is not None:
                    stream.close()
            if self.handle is not None:
                _winapi.CloseHandle(self.handle)
                self.handle = None


class _PosixProcess:
    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str]):
        self.child = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, shell=False, bufsize=0,
        )
        self.stdin, self.stdout, self.stderr = self.child.stdin, self.child.stdout, self.child.stderr

    def poll(self) -> int | None:
        return self.child.poll()

    def terminate_tree(self) -> None:
        try:
            os.killpg(self.child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.child.wait(timeout=5)

    def empty(self) -> bool:
        try:
            os.killpg(self.child.pid, 0)
        except ProcessLookupError:
            return True
        # Linux can retain already-killed adopted grandchildren as zombies.
        # They execute no code and cannot hold pipes, but killpg(..., 0) still
        # sees their PID until the host init reaps them.
        proc = Path("/proc")
        if proc.is_dir():
            try:
                for entry in proc.iterdir():
                    if not entry.name.isdecimal():
                        continue
                    try:
                        fields = (entry / "stat").read_text().rsplit(")", 1)[-1].split()
                    except FileNotFoundError:
                        continue
                    if int(fields[2]) == self.child.pid and fields[0] != "Z":
                        return False
                return True
            except (OSError, ValueError, IndexError):
                return False
        return False

    def close(self) -> None:
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                stream.close()


def _monitored_size(paths: tuple[Path, ...], *, require_absent: bool = False) -> int:
    total = 0
    for path in paths:
        if not path.is_absolute():
            raise ValueError("unsafe_monitored_path")
        for entry in (path, *path.parents):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("unsafe_monitored_path")
            if entry == path:
                if require_absent or not stat.S_ISREG(info.st_mode):
                    raise ValueError("unsafe_monitored_path")
                total += info.st_size
    return total


def _resolve_executable(name: str, cwd: Path, env: dict[str, str]) -> Path | None:
    """Resolve against the caller workspace and PATH, never the adapter cwd.

    shutil.which() implicitly adds the host current directory on Windows and
    reads the host PATHEXT. Neither is the child environment's lookup contract.
    """
    executable = Path(name)
    if executable.drive and not executable.is_absolute():
        raise ValueError("invalid_executable_path")
    if executable.is_absolute() or os.path.dirname(name):
        directories = [executable.parent if executable.is_absolute() else cwd / executable.parent]
        filename = executable.name
    else:
        child_path = env.get("PATH")
        if not child_path:
            return None
        directories = []
        for entry in child_path.split(os.pathsep):
            directory = Path(entry.strip('"'))
            if directory.drive and not directory.is_absolute():
                continue
            directories.append(directory if directory.is_absolute() else cwd / directory)
        filename = name
    filenames = [filename]
    if os.name == "nt":
        extensions = [extension.rstrip(".") for extension in
                      env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if extension]
        if not any(filename.lower().endswith(extension.lower()) for extension in extensions):
            filenames = [filename + extension for extension in extensions]
    for directory in directories:
        for filename in filenames:
            candidate = directory / filename
            if candidate.is_file() and os.access(candidate, os.F_OK | os.X_OK):
                return candidate.resolve()
    return None


def run_owned(
    argv: list[str], *, cwd: Path, env: dict[str, str], stdin: bytes | None,
    stdout_path: Path, stderr_path: Path, timeout_s: float, max_output_bytes: int,
    cancel_event: threading.Event | None = None,
    monitored_paths: tuple[Path, ...] = (),
) -> ProcessOutcome:
    """Run once with retained, capped raw output and verified tree cleanup.

    Existing log files are never overwritten. ``max_output_bytes`` applies to
    stdout, stderr and monitored files combined; it is not a generation-token
    limit. Separate files are sampled, so an individual filesystem write may
    overshoot before detection and termination. Monitored targets must start
    absent and may not traverse reparse points. Input is capped at
    MAX_STDIN_BYTES. Only native .exe entrypoints are used on Windows.
    """
    started = time.monotonic()
    child = None
    launched = timed_out = cancelled = output_limit = False
    cleanup_verified = True
    exit_code = None
    error = None
    threads: list[threading.Thread] = []
    output_event, io_error = threading.Event(), threading.Event()
    budget_lock = threading.Lock()
    stored_output_bytes = monitored_bytes = 0
    try:
        if (
            not argv or any(not isinstance(value, str) or "\0" in value for value in argv)
            or not argv[0] or not math.isfinite(timeout_s) or timeout_s <= 0
            or type(max_output_bytes) is not int or max_output_bytes <= 0
            or (stdin is not None and (not isinstance(stdin, bytes) or len(stdin) > MAX_STDIN_BYTES))
            or stdout_path.resolve() == stderr_path.resolve()
        ):
            raise ValueError("invalid_transport_request")
        executable = _resolve_executable(argv[0], cwd.resolve(), env)
        if executable is None:
            raise FileNotFoundError("executable_not_found")
        if os.name == "nt" and Path(executable).suffix.lower() != ".exe":
            raise ValueError("native_executable_required")
        argv = [str(Path(executable).resolve()), *argv[1:]]
        if (
            len(set(monitored_paths)) != len(monitored_paths)
            or any(path in {stdout_path, stderr_path} for path in monitored_paths)
        ):
            raise ValueError("unsafe_monitored_path")
        _monitored_size(monitored_paths, require_absent=True)
        with stdout_path.open("xb") as stdout_log, stderr_path.open("xb") as stderr_log:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
            else:
                child = (_WindowsProcess if os.name == "nt" else _PosixProcess)(argv, cwd, env)
                launched = True
                cleanup_verified = False

                def drain(source: BinaryIO, target: BinaryIO) -> None:
                    nonlocal stored_output_bytes
                    try:
                        while block := source.read(65536):
                            with budget_lock:
                                keep = min(max(0, max_output_bytes - stored_output_bytes - monitored_bytes),
                                           len(block))
                                stored_output_bytes += keep
                            if keep:
                                target.write(block[:keep])
                            if keep < len(block):
                                output_event.set()
                    except OSError:
                        io_error.set()

                def feed() -> None:
                    view = memoryview(stdin or b"")
                    try:
                        while view:
                            written = child.stdin.write(view[:65536])
                            if not written:
                                break
                            view = view[written:]
                    except (BrokenPipeError, OSError):
                        io_error.set()
                    finally:
                        if view:
                            io_error.set()
                        child.stdin.close()

                for target, args in (
                    (drain, (child.stdout, stdout_log)),
                    (drain, (child.stderr, stderr_log)),
                    (feed, ()),
                ):
                    thread = threading.Thread(target=target, args=args, daemon=True)
                    threads.append(thread)
                    thread.start()
                while True:
                    try:
                        monitored_bytes = _monitored_size(monitored_paths)
                    except (OSError, ValueError):
                        error = "unsafe_monitored_path"
                        io_error.set()
                    with budget_lock:
                        if stored_output_bytes + monitored_bytes > max_output_bytes:
                            output_event.set()
                    exit_code = child.poll()
                    cancelled = cancel_event is not None and cancel_event.is_set()
                    output_limit = output_event.is_set()
                    timed_out = time.monotonic() - started >= timeout_s
                    if exit_code is not None or cancelled or output_limit or timed_out or io_error.is_set():
                        break
                    time.sleep(0.01)
                # Even successful parents may leave children holding pipes or
                # continuing work. Every outcome tears down the owned tree.
                child.terminate_tree()
                cleanup_deadline = time.monotonic() + 5
                while time.monotonic() < cleanup_deadline:
                    if child.empty():
                        cleanup_verified = True
                        break
                    time.sleep(0.01)
                if exit_code is None:
                    exit_code = child.poll()
                for thread in threads:
                    thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
                if any(thread.is_alive() for thread in threads):
                    cleanup_verified = False
                    error = "pipe_cleanup_unverified"
                elif io_error.is_set() and error is None:
                    error = "output_io_failed"
                try:
                    monitored_bytes = _monitored_size(monitored_paths)
                    if stored_output_bytes + monitored_bytes > max_output_bytes:
                        output_event.set()
                except (OSError, ValueError):
                    error = "unsafe_monitored_path"
                output_limit = output_limit or output_event.is_set()
    except (OSError, ValueError, TypeError) as exc:
        error = str(exc) if str(exc) in {
            "invalid_transport_request", "native_executable_required", "executable_not_found",
            "unsafe_monitored_path",
            "invalid_executable_path",
        } else "launch_or_transport_failed"
        if isinstance(exc, _SpawnFailure):
            cleanup_verified = exc.cleanup_verified
        if child is not None:
            try:
                child.terminate_tree()
                cleanup_verified = child.empty()
            except OSError:
                cleanup_verified = False
    finally:
        if child is not None:
            child.close()
    return ProcessOutcome(
        launched=launched, exit_code=exit_code, timed_out=timed_out, cancelled=cancelled,
        output_limit=output_limit, cleanup_verified=cleanup_verified, error=error,
        duration_s=time.monotonic() - started,
    )
