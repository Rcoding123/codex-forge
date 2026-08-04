from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_forge.cli import main
from codex_forge.config import ForgeConfig, load_config
from codex_forge.db import ForgeDB
from codex_forge.models import Project, Task, TaskState


def _path(path: Path) -> str:
    return path.as_posix()


def _configured_db(tmp_path: Path, *, initialize: bool = True) -> tuple[ForgeConfig, ForgeDB]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        f"""
[paths]
workspace_root = '{_path(tmp_path / "worktrees")}'
data_root = '{_path(tmp_path / "data")}'
repository_root = '{_path(tmp_path / "repositories")}'
inbox_root = '{_path(tmp_path / "inbox")}'
database_path = '{_path(tmp_path / "data" / "forge.db")}'
evidence_root = '{_path(tmp_path / "evidence")}'
knowledge_root = '{_path(tmp_path / "knowledge")}'

[codex]
approval_policy = 'never'

[limits]
max_task_tokens = 4321
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    db = ForgeDB(config.database_path)
    if initialize:
        db.initialize()
    return config, db


def _run_json(config: ForgeConfig, capsys, *arguments: str) -> tuple[int, dict, str]:
    exit_code = main(["--config", str(config.config_path), *arguments])
    captured = capsys.readouterr()
    return exit_code, json.loads(captured.out), captured.err


def _add_project(db: ForgeDB, tmp_path: Path) -> Project:
    project = Project(
        id="project-unicode",
        name="Démon-工具",
        repo_path=tmp_path / "Repositories" / "Démon 工具",
        default_branch="trunk",
        validation_commands=["python -m pytest", "python -m ruff check ."],
        auto_fetch=False,
        auto_commit=True,
        auto_push=False,
    )
    db.add_project(project)
    return project


def _add_task(db: ForgeDB, project: Project, tmp_path: Path) -> Task:
    task = Task(
        id="TASK-ユニコード",
        project_id=project.id,
        title="Résumé export 工具",
        objective="Preserve Unicode and Windows paths.",
        acceptance=[],
        validation_commands=[],
        state=TaskState.READY,
        risk="high",
        max_attempts=4,
        branch="forge/task-unicode",
        worktree_path=tmp_path / "Worktrees" / "Démon 工具" / "TASK-ユニコード",
    )
    db.add_task(task)
    return task


def test_status_json_is_versioned_and_backward_compatible(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = _add_project(db, tmp_path)
    _add_task(db, project, tmp_path)

    exit_code, payload, stderr = _run_json(config, capsys, "status", "--json")

    assert exit_code == 0
    assert stderr == ""
    assert payload["schema_version"] == "1.0"
    assert payload["command"] == "status"
    assert payload["success"] is True
    assert payload["error"] is None
    assert payload["tasks"]["READY"] == 1
    assert payload["open_decisions"] == 0
    assert payload["database"] == str(config.database_path)
    assert payload["data"] == {
        "tasks": payload["tasks"],
        "open_decisions": payload["open_decisions"],
        "database": payload["database"],
    }


def test_project_list_json_contract(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = _add_project(db, tmp_path)

    exit_code, payload, stderr = _run_json(config, capsys, "project", "list", "--json")

    assert exit_code == 0
    assert stderr == ""
    assert payload["command"] == "project.list"
    assert payload["success"] is True
    assert payload["error"] is None
    assert payload["data"]["projects"] == [
        {
            "project_id": project.id,
            "name": project.name,
            "repository_path": str(project.repo_path),
            "default_branch": "trunk",
            "validation_commands": ["python -m pytest", "python -m ruff check ."],
            "fetch_policy": "disabled",
            "commit_policy": "automatic",
            "push_policy": "disabled",
            "created_at": payload["data"]["projects"][0]["created_at"],
            "updated_at": payload["data"]["projects"][0]["updated_at"],
        }
    ]
    assert payload["data"]["projects"][0]["created_at"] is not None
    assert "Démon 工具" in payload["data"]["projects"][0]["repository_path"]


def test_project_json_redacts_environment_and_credential_values(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = Project(
        id="project-secrets",
        name="secret-test",
        repo_path=tmp_path / "repo",
        validation_commands=[
            "$env:TEST_PASSWORD='full-env-secret'; python -m pytest",
            "tool --api-key TOP-SECRET-TOKEN",
            "tool https://user:password@example.invalid/path",
        ],
    )
    db.add_project(project)

    exit_code, payload, _ = _run_json(config, capsys, "project", "list", "--json")
    serialized = json.dumps(payload)

    assert exit_code == 0
    assert payload["data"]["projects"][0]["validation_commands"] == [
        "$env:TEST_PASSWORD=[REDACTED]; python -m pytest",
        "tool --api-key [REDACTED]",
        "tool https://[REDACTED]@example.invalid/path",
    ]
    assert "full-env-secret" not in serialized
    assert "TOP-SECRET-TOKEN" not in serialized
    assert "user:password" not in serialized


def test_task_list_and_show_json_expose_stable_persisted_fields(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = _add_project(db, tmp_path)
    task = _add_task(db, project, tmp_path)
    run_id = db.record_run_start(
        task.id,
        "implementation",
        "gpt-test",
        "medium",
        tmp_path / "evidence" / project.name / task.id / "attempt-01" / "implementation",
    )
    db.record_run_end(
        run_id,
        thread_id="thread-test",
        exit_code=0,
        usage={"input_tokens": 12, "output_tokens": 3},
    )

    list_exit, list_payload, list_stderr = _run_json(
        config, capsys, "task", "list", "--state", "READY", "--json"
    )
    show_exit, show_payload, show_stderr = _run_json(
        config, capsys, "task", "show", task.id, "--json"
    )

    assert list_exit == show_exit == 0
    assert list_stderr == show_stderr == ""
    assert list_payload["command"] == "task.list"
    assert list_payload["data"]["filters"] == {"state": "READY"}
    listed = list_payload["data"]["tasks"][0]
    shown = show_payload["data"]["task"]
    assert listed == shown
    assert shown["task_id"] == task.id
    assert shown["project"] == project.name
    assert shown["project_id"] == project.id
    assert shown["title"] == task.title
    assert shown["objective"] == task.objective
    assert shown["state"] == "READY"
    assert shown["risk"] == "high"
    assert shown["attempt_count"] == 0
    assert shown["max_attempts"] == 4
    assert shown["token_usage"] == {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}
    assert shown["token_limit"] == 4321
    assert shown["branch"] == task.branch
    assert shown["worktree"] == str(task.worktree_path)
    assert shown["current_phase"] == "implementation"
    assert shown["evidence_path"].endswith("implementation")
    assert shown["started_at"] is not None
    assert shown["created_at"] is not None
    assert shown["updated_at"] is not None
    assert shown["error_code"] is None
    assert shown["error_message"] is None
    assert shown["completed_at"] is None


def test_task_json_uses_null_for_unavailable_optional_values(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = _add_project(db, tmp_path)
    task = Task(
        id="TASK-empty-optionals",
        project_id=project.id,
        title="New task",
        objective="Wait for execution.",
        acceptance=[],
        validation_commands=[],
        state=TaskState.READY,
    )
    db.add_task(task)

    exit_code, payload, _ = _run_json(config, capsys, "task", "show", task.id, "--json")
    public_task = payload["data"]["task"]

    assert exit_code == 0
    for field in (
        "branch",
        "worktree",
        "current_phase",
        "error_code",
        "error_message",
        "evidence_path",
        "started_at",
        "completed_at",
    ):
        assert public_task[field] is None


def test_decision_list_json_contract(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = _add_project(db, tmp_path)
    task = _add_task(db, project, tmp_path)
    db.add_decision(task.id, "Choose a format?", ["CSV", "JSON"], "JSON")

    exit_code, payload, stderr = _run_json(config, capsys, "decision", "list", "--json")

    assert exit_code == 0
    assert stderr == ""
    assert payload["command"] == "decision.list"
    decision = payload["data"]["decisions"][0]
    assert decision["decision_id"] == 1
    assert decision["task_id"] == task.id
    assert decision["state"] == "OPEN"
    assert decision["question"] == "Choose a format?"
    assert decision["reason"] is None
    assert decision["options"] == ["CSV", "JSON"]
    assert decision["recommendation"] == "JSON"
    assert decision["created_at"] is not None
    assert decision["resolved_at"] is None
    assert decision["resolution"] is None


@pytest.mark.parametrize(
    ("arguments", "collection"),
    [
        (("project", "list", "--json"), "projects"),
        (("task", "list", "--json"), "tasks"),
        (("decision", "list", "--json"), "decisions"),
    ],
)
def test_json_list_commands_support_empty_collections(
    tmp_path: Path, capsys, arguments: tuple[str, ...], collection: str
):
    config, _ = _configured_db(tmp_path)

    exit_code, payload, stderr = _run_json(config, capsys, *arguments)

    assert exit_code == 0
    assert stderr == ""
    assert payload["data"][collection] == []


def test_unknown_task_returns_structured_error_and_nonzero_exit(tmp_path: Path, capsys):
    config, _ = _configured_db(tmp_path)

    exit_code, payload, stderr = _run_json(
        config, capsys, "task", "show", "TASK-missing", "--json"
    )

    assert exit_code == 2
    assert stderr == ""
    assert payload == {
        "schema_version": "1.0",
        "command": "task.show",
        "success": False,
        "data": None,
        "error": {
            "code": "TASK_NOT_FOUND",
            "message": "Task was not found.",
            "details": {"task_id": "TASK-missing"},
        },
    }


def test_malformed_json_command_arguments_return_structured_error(tmp_path: Path, capsys):
    config, _ = _configured_db(tmp_path)

    exit_code, payload, stderr = _run_json(config, capsys, "task", "show", "--json")

    assert exit_code == 2
    assert stderr == ""
    assert payload["command"] == "task.show"
    assert payload["success"] is False
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert "traceback" not in json.dumps(payload).lower()


def test_json_config_and_database_failures_are_structured(tmp_path: Path, capsys):
    missing_config = tmp_path / "missing.toml"
    config_exit = main(["--config", str(missing_config), "status", "--json"])
    config_capture = capsys.readouterr()
    config_payload = json.loads(config_capture.out)
    assert config_exit == 2
    assert config_payload["error"]["code"] == "CONFIG_ERROR"
    assert config_capture.err.startswith("ERROR:")

    config, _ = _configured_db(tmp_path / "uninitialized", initialize=False)
    db_exit, db_payload, db_stderr = _run_json(config, capsys, "status", "--json")
    assert db_exit == 1
    assert db_payload["error"]["code"] == "COMMAND_FAILED"
    assert db_stderr.startswith("ERROR:")
    assert "Traceback" not in json.dumps(db_payload)


def test_task_json_excludes_internal_and_secret_fields(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = _add_project(db, tmp_path)
    task = _add_task(db, project, tmp_path)
    db.update_task(
        task.id,
        state=TaskState.FAILED,
        last_error="subprocess output api_key=TOP-SECRET-TOKEN",
        failure_fingerprint="secret-fingerprint",
        result={
            "api_key": "TOP-SECRET-TOKEN",
            "cookie": "session-secret",
            "environment": {"AUTH_TOKEN": "full-env-secret"},
            "raw_stdout": "sensitive subprocess output",
        },
    )

    exit_code, payload, _ = _run_json(config, capsys, "task", "show", task.id, "--json")
    serialized = json.dumps(payload, ensure_ascii=False)
    public_task = payload["data"]["task"]

    assert exit_code == 0
    assert public_task["error_code"] == "TASK_ERROR"
    assert public_task["error_message"] == (
        "The task has a recorded error. Inspect retained evidence locally."
    )
    for secret in (
        "TOP-SECRET-TOKEN",
        "session-secret",
        "full-env-secret",
        "sensitive subprocess output",
        "secret-fingerprint",
    ):
        assert secret not in serialized
    assert "result" not in public_task
    assert "failure_fingerprint" not in public_task


def test_human_readable_list_and_status_output_is_preserved(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = _add_project(db, tmp_path)
    task = _add_task(db, project, tmp_path)
    db.add_decision(task.id, "Choose?", ["A"], None)

    assert main(["--config", str(config.config_path), "project", "list"]) == 0
    assert capsys.readouterr().out == f"{project.name}\t{project.repo_path}\tbase=trunk\n"

    assert main(["--config", str(config.config_path), "task", "list"]) == 0
    assert capsys.readouterr().out == f"{task.id}\tREADY\tP100\t{task.title}\n"

    assert main(["--config", str(config.config_path), "decision", "list"]) == 0
    assert capsys.readouterr().out == f"Decision 1 for {task.id}: Choose?\n  - A\n"

    assert main(["--config", str(config.config_path), "status"]) == 0
    assert capsys.readouterr().out == "READY=1\nOPEN_DECISIONS=1\n"
