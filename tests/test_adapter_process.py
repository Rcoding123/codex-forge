"""Native subprocess canaries only: none of these commands invokes a model."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from codex_forge.adapter import process


def _run(tmp_path: Path, script: str, **overrides) -> process.ProcessOutcome:
    arguments = dict(
        cwd=tmp_path, env=dict(os.environ), stdin=None,
        stdout_path=tmp_path / "stdout.bin", stderr_path=tmp_path / "stderr.bin",
        timeout_s=10, max_output_bytes=128 * 1024,
    )
    arguments.update(overrides)
    return process.run_owned([sys.executable, "-c", script], **arguments)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Linux zombies are terminated, although their PID persists until reaped.
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists() and stat.read_text().rsplit(")", 1)[-1].split()[0] == "Z":
        return False
    return True


def test_success_preserves_exact_stdin_argv_and_both_streams(tmp_path):
    data = b"hello\x00world\n" * 1000
    result = _run(
        tmp_path,
        "import sys; d=sys.stdin.buffer.read(); sys.stdout.buffer.write(d); "
        "sys.stderr.buffer.write(b'err\\n')",
        stdin=data,
    )
    assert result.launched and result.exit_code == 0
    assert result.cleanup_verified and result.error is None
    assert not result.timed_out and not result.cancelled and not result.output_limit
    assert (tmp_path / "stdout.bin").read_bytes() == data
    assert (tmp_path / "stderr.bin").read_bytes() == b"err\n"


def test_nonzero_exit_is_transport_fact_without_regeneration(tmp_path):
    result = _run(tmp_path, "import sys; print('partial', flush=True); sys.exit(7)")
    assert result.launched and result.exit_code == 7 and result.cleanup_verified
    assert (tmp_path / "stdout.bin").read_text().strip() == "partial"


def _copy_python(directory: Path) -> Path:
    executable = directory / "python.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sys.executable, executable)
    config = Path(sys.executable).parent.parent / "pyvenv.cfg"
    if config.is_file():
        shutil.copy2(config, directory / "pyvenv.cfg")
    return executable


def test_relative_native_executable_binds_to_requested_cwd(tmp_path, monkeypatch):
    adapter_cwd, caller_cwd = tmp_path / "adapter", tmp_path / "caller"
    wrong_executable = _copy_python(adapter_cwd / "build")
    expected_executable = _copy_python(caller_cwd / "build")
    monkeypatch.chdir(adapter_cwd)
    result = process.run_owned(
        ["./build/python.exe", "-c", "import sys; print(sys.executable)"], cwd=caller_cwd,
        env=dict(os.environ), stdin=None, stdout_path=tmp_path / "out", stderr_path=tmp_path / "err",
        timeout_s=5, max_output_bytes=10000,
    )
    assert result.launched and result.exit_code == 0 and result.cleanup_verified
    observed = Path((tmp_path / "out").read_text().strip())
    assert observed == expected_executable
    assert observed != wrong_executable


def test_bare_name_uses_child_path_without_implicit_adapter_cwd(tmp_path, monkeypatch):
    adapter_cwd, caller_cwd = tmp_path / "adapter", tmp_path / "caller"
    wrong_executable = _copy_python(adapter_cwd)
    expected_executable = _copy_python(caller_cwd / "tools")
    monkeypatch.chdir(adapter_cwd)
    result = process.run_owned(
        ["python.exe", "-c", "import sys; print(sys.executable)"], cwd=caller_cwd,
        env={**os.environ, "PATH": "tools"}, stdin=None,
        stdout_path=tmp_path / "out", stderr_path=tmp_path / "err",
        timeout_s=5, max_output_bytes=10000,
    )
    assert result.launched and result.exit_code == 0 and result.cleanup_verified
    observed = Path((tmp_path / "out").read_text().strip())
    assert observed == expected_executable
    assert observed != wrong_executable


def test_child_exit_without_consuming_stdin_is_transport_error(tmp_path):
    result = _run(
        tmp_path, "import sys; sys.stdin.close(); print('ignoring input', flush=True)",
        stdin=b"x" * 1000000,
    )
    assert result.launched and result.cleanup_verified and result.error == "output_io_failed"


@pytest.mark.parametrize("normal_exit", [False, True], ids=["deadline", "normal-parent-exit"])
def test_descendants_are_killed_even_after_successful_parent_exit(tmp_path, normal_exit):
    script = (
        "import pathlib, subprocess, sys, time; "
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path('grandchild.pid').write_text(str(p.pid)); "
        "print('started', flush=True); "
        + ("sys.exit(0)" if normal_exit else "time.sleep(60)")
    )
    result = _run(tmp_path, script, timeout_s=1.5)
    assert result.launched and result.cleanup_verified
    assert result.timed_out is not normal_exit
    assert not _alive(int((tmp_path / "grandchild.pid").read_text()))
    assert (tmp_path / "stdout.bin").read_text().strip() == "started"
    if normal_exit:
        assert result.exit_code == 0


def test_cancel_kills_child_and_preserves_partial_output(tmp_path):
    cancellation = threading.Event()

    def cancel_after_started():
        until = time.monotonic() + 4
        while not (tmp_path / "started.pid").exists() and time.monotonic() < until:
            time.sleep(0.01)
        cancellation.set()

    thread = threading.Thread(target=cancel_after_started)
    thread.start()
    try:
        result = _run(
            tmp_path,
            "import os, pathlib, time; print('ready', flush=True); "
            "pathlib.Path('started.pid').write_text(str(os.getpid())); time.sleep(60)",
            cancel_event=cancellation,
        )
    finally:
        thread.join(timeout=5)
    assert result.launched and result.cancelled and result.cleanup_verified
    assert not _alive(int((tmp_path / "started.pid").read_text()))
    assert (tmp_path / "stdout.bin").read_text().strip() == "ready"


def test_pre_cancelled_request_runs_nothing(tmp_path):
    cancellation = threading.Event()
    cancellation.set()
    result = _run(
        tmp_path, "from pathlib import Path; Path('should-not-exist').touch()",
        cancel_event=cancellation,
    )
    assert not result.launched and result.cancelled and result.cleanup_verified
    assert not (tmp_path / "should-not-exist").exists()


def test_output_budget_is_combined_and_preserves_bounded_raw_prefix(tmp_path):
    result = _run(
        tmp_path,
        "import os, time; os.write(1, b'a' * 8000); os.write(2, b'b' * 8000); time.sleep(60)",
        max_output_bytes=10000,
    )
    assert result.launched and result.output_limit and result.cleanup_verified
    assert not result.timed_out
    stdout, stderr = (tmp_path / "stdout.bin").read_bytes(), (tmp_path / "stderr.bin").read_bytes()
    assert len(stdout) + len(stderr) == 10000
    assert set(stdout) <= {ord("a")} and set(stderr) <= {ord("b")}


def test_large_unread_input_does_not_block_deadline(tmp_path):
    result = _run(tmp_path, "import time; time.sleep(60)", stdin=b"x" * 1000000, timeout_s=0.5)
    assert result.launched and result.timed_out and result.cleanup_verified
    assert result.duration_s < 8


def test_monitored_final_file_overflow_kills_process_during_execution(tmp_path):
    final = tmp_path / "final.json"
    result = _run(
        tmp_path,
        "import pathlib, time; pathlib.Path('final.json').write_bytes(b'x' * 20000); time.sleep(60)",
        monitored_paths=(final,), max_output_bytes=10000,
    )
    assert result.launched and result.output_limit and result.cleanup_verified
    assert not result.timed_out and result.duration_s < 5
    assert final.stat().st_size == 20000  # A separate-file write can overshoot before sampling.


def test_monitored_file_counts_together_with_stdout(tmp_path):
    result = _run(
        tmp_path,
        "import pathlib, os, time; pathlib.Path('final.json').write_bytes(b'x' * 8000); "
        "os.write(1, b'y' * 8000); time.sleep(60)",
        monitored_paths=(tmp_path / "final.json",), max_output_bytes=10000,
    )
    assert result.launched and result.output_limit and result.cleanup_verified
    assert (tmp_path / "final.json").stat().st_size == 8000


def test_existing_monitored_file_is_rejected_before_execution(tmp_path):
    final = tmp_path / "final.json"
    final.write_bytes(b"prior evidence")
    result = _run(tmp_path, "raise AssertionError('must not execute')", monitored_paths=(final,))
    assert not result.launched and result.error == "unsafe_monitored_path"
    assert final.read_bytes() == b"prior evidence"


def test_monitored_reparse_ancestor_is_rejected_before_execution(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = _run(
        tmp_path, "raise AssertionError('must not execute')", monitored_paths=(link / "final.json",),
    )
    assert not result.launched and result.error == "unsafe_monitored_path"


def test_existing_logs_are_not_rewritten(tmp_path):
    (tmp_path / "stdout.bin").write_bytes(b"prior evidence")
    result = _run(tmp_path, "from pathlib import Path; Path('should-not-exist').touch()")
    assert not result.launched and result.error == "launch_or_transport_failed"
    assert (tmp_path / "stdout.bin").read_bytes() == b"prior evidence"
    assert not (tmp_path / "should-not-exist").exists()


def test_missing_executable_fails_without_running(tmp_path):
    result = process.run_owned(
        [str(tmp_path / "missing.exe")], cwd=tmp_path, env=dict(os.environ), stdin=None,
        stdout_path=tmp_path / "out", stderr_path=tmp_path / "err", timeout_s=1, max_output_bytes=1000,
    )
    assert not result.launched and result.cleanup_verified and result.error == "executable_not_found"


@pytest.mark.parametrize("overrides", [{"timeout_s": 0}, {"max_output_bytes": -1},
                                      {"stdin": b"x" * (process.MAX_STDIN_BYTES + 1)}])
def test_invalid_bounds_fail_before_launch(tmp_path, overrides):
    result = _run(tmp_path, "raise AssertionError('must not execute')", **overrides)
    assert not result.launched and result.error == "invalid_transport_request"


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended ownership boundary")
def test_failed_job_assignment_never_executes_child(tmp_path, monkeypatch):
    def reject_ownership(_self, _handle):
        raise OSError("synthetic ownership failure")

    monkeypatch.setattr(process._WindowsJob, "assign", reject_ownership)
    result = _run(tmp_path, "from pathlib import Path; Path('should-not-exist').touch()")
    assert not result.launched and result.error == "launch_or_transport_failed"
    assert result.cleanup_verified
    assert not (tmp_path / "should-not-exist").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows shim rejection")
def test_batch_shims_are_rejected_without_shell_fallback(tmp_path):
    shim = tmp_path / "shim.cmd"
    shim.write_text("@echo SHOULD_NOT_EXECUTE\n")
    result = process.run_owned(
        [str(shim)], cwd=tmp_path, env=dict(os.environ), stdin=None,
        stdout_path=tmp_path / "out", stderr_path=tmp_path / "err", timeout_s=1, max_output_bytes=1000,
    )
    assert not result.launched and result.error == "native_executable_required"
