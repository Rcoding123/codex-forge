import subprocess
from pathlib import Path

from codex_forge.config import ForgeConfig
from codex_forge.runner import CodexRunner, _build_process_command, _worker_environment


def test_runner_captures_structured_output(tmp_path: Path):
    executable = tmp_path / "fake-codex.py"
    executable.write_text(
        """#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index('-o') + 1])
payload = {
  'status': 'completed', 'summary': 'done', 'changed_files': [],
  'validation_performed': [], 'assumptions': [], 'blocking_reason': None,
  'decisions_needed': [], 'reusable_patterns': []
}
out.write_text(json.dumps(payload), encoding='utf-8')
print(json.dumps({'type':'thread.started','thread_id':'thread-1'}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    schema_root = tmp_path / "schemas"
    schema_root.mkdir()
    (schema_root / "task.json").write_text("{}", encoding="utf-8")
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repos",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "evidence",
        knowledge_root=tmp_path / "knowledge",
        codex_executable=str(executable),
    )
    result = CodexRunner(config, schema_root).run(
        prompt="Do work",
        cwd=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        phase="implementation",
        model="test-model",
        effort="medium",
        schema_name="task.json",
    )
    assert result.exit_code == 0
    assert result.thread_id == "thread-1"
    assert result.usage["input_tokens"] == 10
    assert result.final["status"] == "completed"
    assert (tmp_path / "artifacts" / "implementation.prompt.txt").read_text(encoding="utf-8") == "Do work"


def test_runner_places_global_approval_option_before_exec(tmp_path: Path):
    executable = tmp_path / "fake-codex.py"
    executable.write_text(
        """import json, pathlib, sys
args = sys.argv[1:]
assert args.index('--ask-for-approval') < args.index('exec')
assert args[args.index('--ask-for-approval') + 1] == 'never'
disabled = [args[index + 1] for index, value in enumerate(args) if value == '--disable']
assert disabled == ['apps', 'browser_use', 'browser_use_external', 'goals', 'image_generation', 'memories', 'multi_agent', 'plugins', 'skill_search']
assert '--strict-config' in args
assert args[args.index('-c') + 1] == 'model_reasoning_effort=medium'
out = pathlib.Path(args[args.index('-o') + 1])
out.write_text(json.dumps({'status':'completed'}), encoding='utf-8')
""",
        encoding="utf-8",
    )
    schema_root = tmp_path / "schemas"
    schema_root.mkdir()
    (schema_root / "task.json").write_text("{}", encoding="utf-8")
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repos",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "evidence",
        knowledge_root=tmp_path / "knowledge",
        codex_executable=str(executable),
    )
    result = CodexRunner(config, schema_root).run(
        prompt="Do work",
        cwd=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        phase="implementation",
        model="test-model",
        effort="medium",
        schema_name="task.json",
    )
    assert result.exit_code == 0


def test_build_process_command_wraps_windows_shims(monkeypatch):
    """The shim path and its arguments stay separate elements, so `subprocess` quotes them once.

    Flattening them here would quote them twice and split any argument containing a space; see
    tests/test_windows_shim_quoting.py for the child-side proof.
    """
    monkeypatch.setattr("codex_forge.runner.os.name", "nt")
    monkeypatch.setattr("codex_forge.runner.shutil.which", lambda _: r"C:\Tools With Space\codex.cmd")
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")
    command = _build_process_command("codex", ["exec", "quoted value"])
    assert command == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/s",
        "/c",
        "call",
        r"C:\Tools With Space\codex.cmd",
        "exec",
        "quoted value",
    ]


def test_runner_timeout_is_bounded_and_preserved(tmp_path: Path, monkeypatch):
    schema_root = tmp_path / "schemas"
    schema_root.mkdir()
    (schema_root / "task.json").write_text("{}", encoding="utf-8")
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repos",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "evidence",
        knowledge_root=tmp_path / "knowledge",
        codex_executable="codex",
        task_timeout_minutes=1,
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("codex_forge.runner.subprocess.run", timeout)
    result = CodexRunner(config, schema_root).run(
        prompt="Do work",
        cwd=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        phase="implementation",
        model="test-model",
        effort="medium",
        schema_name="task.json",
    )
    assert result.exit_code == 124
    assert "timed out after 60 seconds" in result.stderr_path.read_text(encoding="utf-8")


def test_worker_environment_exposes_private_python():
    environment = _worker_environment()
    path_entries = environment["PATH"].split(";")
    assert str(Path(__import__("sys").executable).resolve().parent).casefold() in {
        str(Path(item)).casefold() for item in path_entries
    }
    assert environment["PYTHONIOENCODING"] == "utf-8"
