from pathlib import Path

from types import SimpleNamespace

from codex_forge.cli import cmd_doctor, cmd_task_retry
from codex_forge.config import ForgeConfig
from codex_forge.db import ForgeDB
from codex_forge.models import Project, Task, TaskState


def test_manual_retry_cannot_exceed_attempt_limit(tmp_path: Path, capsys):
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repos",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "evidence",
        knowledge_root=tmp_path / "knowledge",
    )
    db = ForgeDB(config.database_path)
    db.initialize()
    project = Project(id="project-1", name="demo", repo_path=tmp_path / "repo")
    db.add_project(project)
    task = Task(
        id="TASK-1",
        project_id=project.id,
        title="Exhausted",
        objective="stop",
        acceptance=[],
        validation_commands=[],
        state=TaskState.FAILED,
        attempts=2,
        max_attempts=2,
    )
    db.add_task(task)
    assert cmd_task_retry(config, task.id) == 1
    assert "exhausted its attempt limit" in capsys.readouterr().err
    assert db.get_task(task.id).state == TaskState.FAILED


def test_manual_retry_preserves_failure_context(tmp_path: Path):
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repos",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "evidence",
        knowledge_root=tmp_path / "knowledge",
    )
    db = ForgeDB(config.database_path)
    db.initialize()
    project = Project(id="project-1", name="demo", repo_path=tmp_path / "repo")
    db.add_project(project)
    task = Task(
        id="TASK-1",
        project_id=project.id,
        title="Retryable",
        objective="retry",
        acceptance=[],
        validation_commands=[],
        state=TaskState.FAILED,
        attempts=1,
        max_attempts=2,
        last_error="exact prior failure",
    )
    db.add_task(task)
    assert cmd_task_retry(config, task.id) == 0
    retried = db.get_task(task.id)
    assert retried.state == TaskState.READY
    assert retried.last_error == "exact prior failure"


def test_doctor_fails_when_codex_authentication_is_unavailable(tmp_path: Path, monkeypatch, capsys):
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repos",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "evidence",
        knowledge_root=tmp_path / "knowledge",
    )
    config.config_path.write_text("", encoding="utf-8")
    config.data_root.mkdir()
    config.database_path.write_text("", encoding="utf-8")
    monkeypatch.setattr("codex_forge.cli.command_exists", lambda _: True)
    monkeypatch.setattr(
        "codex_forge.cli.run_process", lambda *args, **kwargs: SimpleNamespace(returncode=1)
    )
    assert cmd_doctor(config) == 1
    output = capsys.readouterr()
    assert "[MISSING] codex_auth" in output.out
    assert "codex_auth" in output.err
