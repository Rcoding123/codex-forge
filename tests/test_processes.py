from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codex_forge.config import ForgeConfig
from codex_forge.processes import run_supervised
from codex_forge.runner import CodexRunner
from codex_forge.validation import run_validations


def worker(root: Path) -> tuple[Path, Path]:
    heartbeat = root / "heartbeat"
    child = root / "child.py"
    child.write_text(
        "import pathlib,sys,time\n"
        "p=pathlib.Path(sys.argv[1])\n"
        "while True:\n"
        " with p.open('a') as f: f.write('alive\\n'); f.flush()\n"
        " time.sleep(.02)\n", encoding="utf-8",
    )
    parent = root / "worker.py"
    parent.write_text(
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}, {str(heartbeat)!r}])\n"
        "print('partial stdout', flush=True)\n"
        "print('partial stderr', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n", encoding="utf-8",
    )
    return parent, heartbeat


def assert_stopped(heartbeat: Path):
    assert heartbeat.exists(), "A real descendant must have run before the timeout."
    snapshot = heartbeat.read_bytes()
    time.sleep(.15)
    assert heartbeat.read_bytes() == snapshot


def test_timeout_closes_real_descendants_and_retains_partial_output(tmp_path):
    parent, heartbeat = worker(tmp_path)
    with pytest.raises(subprocess.TimeoutExpired) as error:
        run_supervised([sys.executable, str(parent)], timeout=1)
    assert "partial stdout" in error.value.output
    assert "partial stderr" in error.value.stderr
    assert_stopped(heartbeat)


@pytest.mark.parametrize("shim", [False, pytest.param(True, marks=pytest.mark.skipif(os.name != "nt", reason="Windows shim"))])
def test_codex_runner_closes_descendants_including_command_shims(tmp_path, shim):
    parent, heartbeat = worker(tmp_path)
    executable = parent
    if shim:
        executable = tmp_path / "codex with space.cmd"
        executable.write_text(f'@echo off\n"{sys.executable}" "{parent}" %*\n', encoding="utf-8")
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml", workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data", repository_root=tmp_path / "repos", inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "db", evidence_root=tmp_path / "evidence", knowledge_root=tmp_path / "knowledge",
        codex_executable=str(executable), task_timeout_minutes=1 / 60,
    )
    result = CodexRunner(config, tmp_path).run(
        prompt="test", cwd=tmp_path, artifact_dir=tmp_path / "artifacts", phase="implementation",
        model="fake", effort="medium", schema_name="task.json",
    )
    assert result.exit_code == 124
    assert "partial stdout" in result.jsonl_path.read_text(encoding="utf-8")
    assert "partial stderr" in result.stderr_path.read_text(encoding="utf-8")
    assert_stopped(heartbeat)


def test_validation_timeout_preserves_logs_and_closes_descendants(tmp_path, monkeypatch):
    parent, heartbeat = worker(tmp_path)
    monkeypatch.setattr("codex_forge.validation._shell_command", lambda _: [sys.executable, str(parent)])
    results = run_validations(["test"], cwd=tmp_path, artifact_dir=tmp_path / "logs", timeout_seconds=1)
    assert results[0].exit_code == 124
    assert "partial stdout" in results[0].stdout_path.read_text(encoding="utf-8")
    assert "partial stderr" in results[0].stderr_path.read_text(encoding="utf-8")
    assert_stopped(heartbeat)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment")
def test_windows_assignment_failure_never_runs_suspended_worker(tmp_path, monkeypatch):
    import ctypes

    marker = tmp_path / "must-not-run"
    real_loader = ctypes.WinDLL

    class RefusedAssignment:
        def __call__(self, *_):
            ctypes.set_last_error(5)
            return False

    class Kernel:
        def __init__(self, *args, **kwargs):
            self.real = real_loader(*args, **kwargs)
            self.AssignProcessToJobObject = RefusedAssignment()

        def __getattr__(self, name):
            return getattr(self.real, name)

    monkeypatch.setattr(ctypes, "WinDLL", Kernel)
    with pytest.raises(OSError):
        run_supervised([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"])
    assert not marker.exists()


def test_interruption_closes_descendants(tmp_path, monkeypatch):
    parent, heartbeat = worker(tmp_path)
    if os.name == "nt":
        import _winapi

        original = _winapi.WaitForSingleObject

        def wait(handle, timeout):
            if timeout == 20 and heartbeat.exists():
                raise KeyboardInterrupt
            return original(handle, timeout)

        monkeypatch.setattr(_winapi, "WaitForSingleObject", wait)
    else:
        original = subprocess.Popen.wait
        interrupted = False

        def wait(self, timeout=None):
            nonlocal interrupted
            if timeout is not None and not interrupted:
                deadline = time.monotonic() + 5
                while not heartbeat.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                interrupted = True
                raise KeyboardInterrupt
            return original(self, timeout=timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", wait)
    with pytest.raises(KeyboardInterrupt):
        run_supervised([sys.executable, str(parent)], timeout=10)
    assert_stopped(heartbeat)
