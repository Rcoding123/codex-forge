from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from codex_forge.cli import main
from codex_forge.config import ForgeConfig, load_config
from codex_forge.db import ForgeDB
from codex_forge.models import Project, TaskState
from codex_forge.utils import derived_worktree_path


UNICODE_PROJECT = "Démon-工具"
SECRET_VALUE = "sk-live-DO-NOT-LEAK-42"


def _path(path: Path) -> str:
    return path.as_posix()


def _configured_db(tmp_path: Path, *, initialize: bool = True) -> tuple[ForgeConfig, ForgeDB]:
    """Build a fully isolated config and database rooted in the test's temporary directory."""
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


def _register_project(db: ForgeDB, tmp_path: Path, name: str = UNICODE_PROJECT) -> Project:
    project = Project(
        id="project-submission",
        name=name,
        repo_path=tmp_path / "Repositories" / "Démon 工具",
        default_branch="trunk",
        validation_commands=["python -m pytest"],
    )
    db.add_project(project)
    return project


def _run(config: ForgeConfig, capsys, *arguments: str) -> tuple[int, str, str]:
    exit_code = main(["--config", str(config.config_path), *arguments])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _run_json(config: ForgeConfig, capsys, *arguments: str) -> tuple[int, dict, str, str]:
    exit_code, out, err = _run(config, capsys, *arguments)
    return exit_code, _single_json_document(out), out, err


def _single_json_document(stdout: str) -> dict:
    """Parse stdout and prove it holds exactly one JSON document and nothing else."""
    decoder = json.JSONDecoder()
    document, index = decoder.raw_decode(stdout.lstrip())
    assert stdout.lstrip()[index:].strip() == "", "stdout carried trailing output"
    assert isinstance(document, dict)
    return document


def _submit_arguments(**overrides: object) -> list[str]:
    base = {
        "--project": UNICODE_PROJECT,
        "--title": "Add deterministic report export",
        "--objective": "Implement CSV export for the existing report model.",
    }
    base.update({key: value for key, value in overrides.items() if value is not None})
    arguments: list[str] = ["task", "add"]
    for flag, value in base.items():
        if isinstance(value, list):
            for item in value:
                arguments += [flag, str(item)]
        else:
            arguments += [flag, str(value)]
    return arguments


def _assert_envelope(document: dict, *, success: bool) -> None:
    assert document["schema_version"] == "1.0"
    assert document["command"] == "task.add"
    assert document["success"] is success
    if success:
        assert document["error"] is None
        assert isinstance(document["data"], dict)
    else:
        assert document["data"] is None
        assert isinstance(document["error"], dict)


def _assert_failure(document: dict, code: str) -> dict:
    _assert_envelope(document, success=False)
    assert document["error"]["code"] == code
    assert isinstance(document["error"]["message"], str) and document["error"]["message"]
    assert isinstance(document["error"]["details"], dict)
    return document["error"]


# --- validation (no mutation) -------------------------------------------------------------


def test_dry_run_validates_without_writing_to_the_database(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--acceptance": ["Existing tests pass."], "--validation": ["python -m pytest"]}),
        "--dry-run",
        "--json",
    )

    assert exit_code == 0
    _assert_envelope(document, success=True)
    data = document["data"]
    assert data["dry_run"] is True
    assert data["created"] is False
    assert data["would_create"] is True
    assert data["task"]["state"] == TaskState.READY.value
    assert data["task"]["created_at"] is None
    assert db.list_tasks() == []
    # No branch, worktree, or evidence directory may appear from a validation call.
    assert not config.workspace_root.exists()
    assert not config.evidence_root.exists()


def test_dry_run_reports_normalized_request_and_resolved_defaults(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(
            **{
                "--title": "  Trim me  ",
                "--acceptance": ["  Spaced criterion  ", "Spaced criterion"],
            }
        ),
        "--dry-run",
        "--json",
    )

    assert exit_code == 0
    normalized = document["data"]["normalized_request"]
    assert normalized["title"] == "Trim me"
    assert normalized["acceptance_criteria"] == ["Spaced criterion"]
    assert normalized["project"] == UNICODE_PROJECT
    assert normalized["task_id_source"] == "derived"

    defaults = document["data"]["defaults_applied"]
    assert defaults["risk"] == "medium"
    assert defaults["priority"] == 100
    assert defaults["max_attempts"] == 3
    assert defaults["state"] == TaskState.READY.value
    assert defaults["token_limit"] == 4321
    assert defaults["task_id"] == normalized["task_id"]


def test_dry_run_omits_defaults_the_client_supplied(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    _, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--risk": "high", "--max-attempts": 5, "--id": "TASK-EXPLICIT"}),
        "--dry-run",
        "--json",
    )

    defaults = document["data"]["defaults_applied"]
    assert "risk" not in defaults
    assert "max_attempts" not in defaults
    assert "task_id" not in defaults
    assert defaults["priority"] == 100
    assert document["data"]["normalized_request"]["task_id_source"] == "client"


def test_dry_run_detects_a_conflict_before_submission(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)
    _run_json(config, capsys, *_submit_arguments(**{"--id": "TASK-PREFLIGHT"}), "--json")

    exit_code, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--id": "TASK-PREFLIGHT", "--objective": "A materially different goal."}),
        "--dry-run",
        "--json",
    )

    assert exit_code == 2
    _assert_failure(document, "TASK_ID_CONFLICT")


# --- submission ---------------------------------------------------------------------------


def test_submission_creates_one_task_and_returns_stable_fields(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(
            **{
                "--id": "TASK-EXPORT-1",
                "--acceptance": ["Existing tests pass.", "Malformed paths fail clearly."],
                "--validation": ["python -m pytest"],
                "--tag": ["export"],
                "--risk": "high",
                "--priority": 250,
                "--max-attempts": 4,
            }
        ),
        "--json",
    )

    assert exit_code == 0
    _assert_envelope(document, success=True)
    data = document["data"]
    assert data["created"] is True
    assert data["dry_run"] is False

    task = data["task"]
    for name in (
        "task_id",
        "project",
        "title",
        "objective",
        "state",
        "risk",
        "max_attempts",
        "token_limit",
        "acceptance_criteria",
        "validation_commands",
        "branch",
        "worktree",
        "created_at",
    ):
        assert name in task, name
    assert task["task_id"] == "TASK-EXPORT-1"
    assert task["project"] == UNICODE_PROJECT
    assert task["risk"] == "high"
    assert task["max_attempts"] == 4
    assert task["priority"] == 250
    assert task["token_limit"] == 4321
    assert task["state"] == TaskState.READY.value
    assert task["acceptance_criteria"] == ["Existing tests pass.", "Malformed paths fail clearly."]
    assert task["validation_commands"] == ["python -m pytest"]
    assert task["created_at"]
    # Branch and worktree are unset until the orchestrator prepares them; the planned values are
    # the deterministic derivation the orchestrator will use.
    assert task["branch"] is None
    assert task["worktree"] is None
    assert task["planned_branch"].startswith("forge/task-export-1")
    assert task["planned_worktree"].startswith(str(config.workspace_root / project.name))

    stored = db.get_task("TASK-EXPORT-1")
    assert stored is not None
    assert stored.state is TaskState.READY
    assert len(db.list_tasks()) == 1


def test_submission_with_dependencies_starts_in_backlog(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    _, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--id": "TASK-DEPENDENT", "--depends-on": ["TASK-BASE"]}),
        "--json",
    )

    assert document["data"]["task"]["state"] == TaskState.BACKLOG.value
    assert document["data"]["task"]["dependencies"] == ["TASK-BASE"]


# --- idempotency --------------------------------------------------------------------------


def test_identical_retry_returns_the_existing_task_without_creating_a_duplicate(
    tmp_path: Path, capsys
):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)
    arguments = _submit_arguments(
        **{
            "--id": "TASK-RETRY",
            "--acceptance": ["Existing tests pass."],
            "--validation": ["python -m pytest"],
        }
    )

    first_code, first, _, _ = _run_json(config, capsys, *arguments, "--json")
    second_code, second, _, _ = _run_json(config, capsys, *arguments, "--json")
    third_code, third, _, _ = _run_json(config, capsys, *arguments, "--json")

    assert (first_code, second_code, third_code) == (0, 0, 0)
    assert first["data"]["created"] is True
    assert second["data"]["created"] is False
    assert third["data"]["created"] is False
    assert second["data"]["task"]["task_id"] == first["data"]["task"]["task_id"]
    assert second["data"]["task"]["created_at"] == first["data"]["task"]["created_at"]
    assert len(db.list_tasks()) == 1


def test_identical_retry_ignores_insignificant_whitespace_and_ordering(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    first_code, first, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--id": "TASK-WHITESPACE", "--acceptance": ["One criterion."]}),
        "--json",
    )
    second_code, second, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(
            **{
                "--id": "  TASK-WHITESPACE  ",
                "--title": "  Add deterministic report export  ",
                "--acceptance": ["  One criterion.  ", "One criterion."],
            }
        ),
        "--json",
    )

    assert (first_code, second_code) == (0, 0)
    assert first["data"]["created"] is True
    assert second["data"]["created"] is False
    assert len(db.list_tasks()) == 1


def test_reordered_tags_and_dependencies_are_the_same_request(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    first_code, first, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(
            **{
                "--id": "TASK-UNORDERED",
                "--tag": ["alpha", "beta"],
                "--depends-on": ["TASK-A", "TASK-B"],
            }
        ),
        "--json",
    )
    second_code, second, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(
            **{
                "--id": "TASK-UNORDERED",
                "--tag": ["beta", "alpha"],
                "--depends-on": ["TASK-B", "TASK-A"],
            }
        ),
        "--json",
    )

    assert (first_code, second_code) == (0, 0)
    assert first["data"]["created"] is True
    assert second["data"]["created"] is False
    assert len(db.list_tasks()) == 1
    # The stored task keeps the order the creating request supplied.
    assert db.get_task("TASK-UNORDERED").tags == ["alpha", "beta"]


def test_reordered_validation_commands_are_a_different_request(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)
    ordered = ["python -m pytest", "python -m ruff check ."]

    _run_json(
        config, capsys, *_submit_arguments(**{"--id": "TASK-ORDERED", "--validation": ordered}), "--json"
    )
    exit_code, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--id": "TASK-ORDERED", "--validation": list(reversed(ordered))}),
        "--json",
    )

    # Validation commands run in order and stop at the first failure, so their order is material.
    assert exit_code == 2
    _assert_failure(document, "TASK_ID_CONFLICT")
    assert len(db.list_tasks()) == 1


def test_malformed_dependency_identifiers_are_rejected(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--depends-on": ["../escape"]}), "--json"
    )

    assert exit_code == 2
    error = _assert_failure(document, "INVALID_TASK_REQUEST")
    assert error["details"]["field"] == "dependencies"
    assert db.list_tasks() == []


def test_derived_task_id_collision_reports_duplicate_task_id(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    created_code, created, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--acceptance": ["First criterion."]}), "--json"
    )
    assert created_code == 0
    assert created["data"]["created"] is True

    # Same project, title, and objective derive the same id, but the request differs materially.
    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--acceptance": ["A different criterion."]}), "--json"
    )

    assert exit_code == 2
    error = _assert_failure(document, "DUPLICATE_TASK_ID")
    assert error["details"]["task_id"] == created["data"]["task"]["task_id"]
    assert len(db.list_tasks()) == 1


def test_explicit_task_id_reuse_with_a_different_request_reports_a_conflict(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)
    _run_json(config, capsys, *_submit_arguments(**{"--id": "TASK-CONFLICT"}), "--json")

    exit_code, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--id": "TASK-CONFLICT", "--objective": "Something else entirely."}),
        "--json",
    )

    assert exit_code == 2
    error = _assert_failure(document, "TASK_ID_CONFLICT")
    assert error["details"]["task_id"] == "TASK-CONFLICT"
    assert len(db.list_tasks()) == 1
    assert db.get_task("TASK-CONFLICT").objective == "Implement CSV export for the existing report model."


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--risk", "critical"),
        ("--priority", "500"),
        ("--max-attempts", "9"),
        ("--tag", "extra-tag"),
        ("--validation", "python -m ruff check ."),
    ],
)
def test_every_material_field_change_is_a_conflict(tmp_path: Path, capsys, flag: str, value: str):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)
    _run_json(config, capsys, *_submit_arguments(**{"--id": "TASK-MATERIAL"}), "--json")

    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--id": "TASK-MATERIAL"}), flag, value, "--json"
    )

    assert exit_code == 2
    _assert_failure(document, "TASK_ID_CONFLICT")
    assert len(db.list_tasks()) == 1


def test_a_lost_insert_race_with_an_identical_request_adopts_the_winning_task(
    tmp_path: Path, capsys, monkeypatch
):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)
    original_add_task = ForgeDB.add_task

    def racing_add_task_guarded(self: ForgeDB, task, guard) -> None:
        original_add_task(self, task)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: tasks.id")

    monkeypatch.setattr(ForgeDB, "add_task_guarded", racing_add_task_guarded)
    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--id": "TASK-RACE"}), "--json"
    )

    assert exit_code == 0
    assert document["data"]["created"] is False
    assert document["data"]["task"]["task_id"] == "TASK-RACE"
    assert len(db.list_tasks()) == 1


def test_a_lost_insert_race_with_a_different_request_reports_a_conflict(
    tmp_path: Path, capsys, monkeypatch
):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)
    original_add_task = ForgeDB.add_task

    def racing_add_task_guarded(self: ForgeDB, task, guard) -> None:
        task.objective = "A racing writer stored something else."
        original_add_task(self, task)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: tasks.id")

    monkeypatch.setattr(ForgeDB, "add_task_guarded", racing_add_task_guarded)
    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--id": "TASK-RACE-CONFLICT"}), "--json"
    )

    assert exit_code == 2
    _assert_failure(document, "TASK_ID_CONFLICT")


# --- structured errors --------------------------------------------------------------------


def test_missing_project_reports_project_not_found(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)

    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--project": "not-registered"}), "--json"
    )

    assert exit_code == 2
    error = _assert_failure(document, "PROJECT_NOT_FOUND")
    assert error["details"]["project"] == "not-registered"
    assert db.list_tasks() == []


@pytest.mark.parametrize(
    "task_id",
    [
        "",
        "   ",
        "..",
        "../escape",
        "..\\escape",
        "with space",
        "C:/absolute",
        "sub/dir",
        "sub\\dir",
        "pipe|id",
        "star*id",
        "TASK-\x01",
        "TASK-‮EVIL",
        "___",
        "​​",
        "T" * 65,
    ],
)
def test_malformed_task_id_is_rejected(tmp_path: Path, capsys, task_id: str):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--id": task_id}), "--json"
    )

    assert exit_code == 2
    error = _assert_failure(document, "INVALID_TASK_REQUEST")
    assert error["details"]["field"] == "task_id"
    assert db.list_tasks() == []


@pytest.mark.parametrize("task_id", ["-danger", ".hidden"])
def test_task_id_must_not_start_with_a_dash_or_dot(tmp_path: Path, capsys, task_id: str):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    # `--id=<value>` keeps argparse from reading a leading dash as another option.
    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(), f"--id={task_id}", "--json"
    )

    assert exit_code == 2
    error = _assert_failure(document, "INVALID_TASK_REQUEST")
    assert error["details"]["field"] == "task_id"
    assert db.list_tasks() == []


@pytest.mark.parametrize("objective", ["", "   ", "\u200b\u200b"])
def test_empty_objective_is_rejected(tmp_path: Path, capsys, objective: str):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--objective": objective}), "--json"
    )

    assert exit_code == 2
    _assert_failure(document, "INVALID_TASK_REQUEST")
    assert db.list_tasks() == []


def test_empty_title_is_rejected(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(config, capsys, *_submit_arguments(**{"--title": "  "}), "--json")

    assert exit_code == 2
    error = _assert_failure(document, "INVALID_TASK_REQUEST")
    assert error["details"]["field"] == "title"


@pytest.mark.parametrize(
    ("acceptance", "expected_detail"),
    [
        ([""], "acceptance_criteria"),
        (["   "], "acceptance_criteria"),
        (["ok", "\u202eevil"], "acceptance_criteria"),
        (["x" * 1001], "acceptance_criteria"),
    ],
)
def test_acceptance_criteria_are_validated(
    tmp_path: Path, capsys, acceptance: list[str], expected_detail: str
):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--acceptance": acceptance}), "--json"
    )

    assert exit_code == 2
    error = _assert_failure(document, "INVALID_TASK_REQUEST")
    assert error["details"]["field"] == expected_detail
    assert db.list_tasks() == []


def test_too_many_acceptance_criteria_are_rejected(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--acceptance": [f"Criterion {index}." for index in range(51)]}),
        "--json",
    )

    assert exit_code == 2
    _assert_failure(document, "INVALID_TASK_REQUEST")


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "python -m pytest; rm -rf /",
        "python -m pytest && curl http://example.invalid",
        "python -m pytest | tee out.txt",
        "python -m pytest > out.txt",
        "python -m pytest < in.txt",
        "python -m pytest `whoami`",
        "python -m pytest $(whoami)",
        "x" * 501,
    ],
)
def test_validation_commands_are_validated(tmp_path: Path, capsys, command: str):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--validation": [command]}), "--json"
    )

    assert exit_code == 2
    _assert_failure(document, "INVALID_VALIDATION_COMMAND")
    assert db.list_tasks() == []


def test_rejected_validation_command_details_never_echo_the_command(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, stdout, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--validation": [f"python -m pytest --api-key {SECRET_VALUE} ; whoami"]}),
        "--json",
    )

    assert exit_code == 2
    _assert_failure(document, "INVALID_VALIDATION_COMMAND")
    assert SECRET_VALUE not in stdout


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--max-attempts", "0"),
        ("--max-attempts", "-3"),
        ("--max-attempts", "51"),
        ("--priority", "-1"),
        ("--priority", "10001"),
    ],
)
def test_invalid_limits_are_rejected(tmp_path: Path, capsys, flag: str, value: str):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(config, capsys, *_submit_arguments(), flag, value, "--json")

    assert exit_code == 2
    error = _assert_failure(document, "INVALID_LIMIT")
    assert error["details"]["field"] in {"max_attempts", "priority"}
    assert db.list_tasks() == []


def test_self_dependency_is_rejected(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--id": "TASK-SELF", "--depends-on": ["TASK-SELF"]}),
        "--json",
    )

    assert exit_code == 2
    _assert_failure(document, "INVALID_TASK_REQUEST")


@pytest.mark.parametrize("extra", [(), ("--dry-run",)])
def test_uninitialized_database_reports_a_configuration_error(
    tmp_path: Path, capsys, extra: tuple[str, ...]
):
    config, _ = _configured_db(tmp_path, initialize=False)

    exit_code, document, _, _ = _run_json(config, capsys, *_submit_arguments(), *extra, "--json")

    assert exit_code == 2
    _assert_failure(document, "CONFIGURATION_ERROR")
    # Reporting that Forge is uninitialized must not initialize it as a side effect.
    assert not config.database_path.exists()


def test_a_corrupt_database_file_reports_a_configuration_error(tmp_path: Path, capsys):
    config, _ = _configured_db(tmp_path, initialize=False)
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.database_path.write_bytes(b"")

    exit_code, document, _, _ = _run_json(config, capsys, *_submit_arguments(), "--json")

    assert exit_code == 2
    _assert_failure(document, "CONFIGURATION_ERROR")


def test_database_failure_reports_a_database_error(tmp_path: Path, capsys, monkeypatch):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    # The first database read admission performs is the guarded read of the task id; nothing is
    # read back after the guarded transaction, so this is the seam a failing disk reaches first.
    def failing_read_task_guarded(self: ForgeDB, task_id: str, guard):
        raise sqlite3.DatabaseError(f"disk I/O error reading {self.path}")

    monkeypatch.setattr(ForgeDB, "read_task_guarded", failing_read_task_guarded)
    exit_code, document, stdout, _ = _run_json(config, capsys, *_submit_arguments(), "--json")

    assert exit_code == 1
    _assert_failure(document, "DATABASE_ERROR")
    assert "disk I/O error" not in stdout
    assert str(config.database_path) not in stdout


def test_argparse_failures_stay_within_the_contract(tmp_path: Path, capsys):
    config, _ = _configured_db(tmp_path)

    exit_code, document, _, _ = _run_json(config, capsys, "task", "add", "--project", "demo", "--json")

    assert exit_code == 2
    _assert_failure(document, "INVALID_ARGUMENT")


def test_every_documented_error_code_has_an_exit_code():
    from codex_forge.control_plane import ERROR_EXIT_CODES, error_exit_code

    for code in (
        "INVALID_TASK_REQUEST",
        "PROJECT_NOT_FOUND",
        "DUPLICATE_TASK_ID",
        "TASK_ID_CONFLICT",
        "INVALID_VALIDATION_COMMAND",
        "INVALID_LIMIT",
        "DATABASE_ERROR",
        "CONFIGURATION_ERROR",
    ):
        assert code in ERROR_EXIT_CODES
        assert error_exit_code(code) in {1, 2}
    assert error_exit_code("DATABASE_ERROR") == 1
    assert error_exit_code("TASK_ID_CONFLICT") == 2


# --- Unicode, Windows paths, and stdout hygiene -------------------------------------------


def test_unicode_content_survives_submission_and_read_back(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, stdout, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(
            **{
                "--id": "TASK-ユニコード",
                "--title": "Rapport déterministe 工具",
                "--objective": "Générer un rapport CSV déterministe pour le modèle existant.",
                "--acceptance": ["Les tests existants passent ✅"],
                "--tag": ["日本語"],
            }
        ),
        "--json",
    )

    assert exit_code == 0
    task = document["data"]["task"]
    assert task["task_id"] == "TASK-ユニコード"
    assert task["title"] == "Rapport déterministe 工具"
    assert task["project"] == UNICODE_PROJECT
    assert task["acceptance_criteria"] == ["Les tests existants passent ✅"]
    assert "ユニコード" in stdout, "JSON must be emitted without ASCII escaping"
    assert db.get_task("TASK-ユニコード").title == "Rapport déterministe 工具"


def test_json_is_emitted_even_when_the_console_encoding_is_legacy(tmp_path: Path):
    import io

    from codex_forge.control_plane import write_response

    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="")
    write_response("task.add", data={"title": "Rapport déterministe 工具"}, stream=stream)
    stream.flush()
    payload = stream.buffer.getvalue().decode("utf-8")

    assert stream.encoding.lower().replace("_", "-") == "utf-8"
    assert json.loads(payload)["data"]["title"] == "Rapport déterministe 工具"


def test_windows_paths_round_trip_through_json(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    project = _register_project(db, tmp_path)

    _, document, stdout, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--id": "TASK-WINPATH"}), "--json"
    )

    planned = document["data"]["task"]["planned_worktree"]
    expected = str(derived_worktree_path(config.workspace_root, project.name, "TASK-WINPATH"))
    assert planned == expected
    if "\\" in expected:
        assert "\\\\" in stdout, "backslashes must be JSON-escaped, not emitted raw"
    assert Path(planned).is_absolute()
    assert not (config.workspace_root / project.name).exists(), "submission must not create worktrees"


def test_json_mode_writes_exactly_one_document_to_stdout(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, document, stdout, stderr = _run_json(
        config, capsys, *_submit_arguments(**{"--id": "TASK-PURE"}), "--json"
    )

    assert exit_code == 0
    assert document["success"] is True
    assert stdout.lstrip().startswith("{")
    assert stdout.rstrip().endswith("}")
    assert stdout.count('"schema_version"') == 1
    assert "Added " not in stdout
    assert "Traceback" not in stdout and "Traceback" not in stderr
    assert "\t" not in stdout


def test_json_failures_still_write_one_document_and_keep_diagnostics_on_stderr(
    tmp_path: Path, capsys
):
    config, _ = _configured_db(tmp_path)

    exit_code, document, stdout, stderr = _run_json(
        config, capsys, *_submit_arguments(**{"--project": "missing"}), "--json"
    )

    assert exit_code != 0
    assert document["success"] is False
    assert stdout.count('"schema_version"') == 1
    assert "PROJECT_NOT_FOUND" in stderr
    assert "Traceback" not in stdout and "Traceback" not in stderr


def test_secrets_in_validation_commands_are_redacted_but_still_executed_verbatim(
    tmp_path: Path, capsys
):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)
    commands = [
        f"python -m pytest --api-key {SECRET_VALUE}",
        f"FORGE_TOKEN={SECRET_VALUE} python -m pytest",
    ]

    exit_code, document, stdout, stderr = _run_json(
        config,
        capsys,
        *_submit_arguments(**{"--id": "TASK-SECRET", "--validation": commands}),
        "--json",
    )

    assert exit_code == 0
    assert SECRET_VALUE not in stdout
    assert SECRET_VALUE not in stderr
    assert all("[REDACTED]" in command for command in document["data"]["task"]["validation_commands"])
    assert all(
        "[REDACTED]" in command
        for command in document["data"]["normalized_request"]["validation_commands"]
    )
    # Redaction is a presentation concern; the controller must still run the real command.
    assert db.get_task("TASK-SECRET").validation_commands == commands


def test_submission_response_excludes_private_persistence_details(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    _, document, stdout, _ = _run_json(
        config, capsys, *_submit_arguments(**{"--id": "TASK-PRIVATE"}), "--json"
    )

    task = document["data"]["task"]
    for private in ("failure_fingerprint", "identical_failures", "result", "result_json", "last_error"):
        assert private not in task
    assert str(config.database_path) not in stdout


# --- human-readable behavior and read-command compatibility -------------------------------


def test_human_readable_task_add_is_unchanged(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, stdout, stderr = _run(
        config,
        capsys,
        "task",
        "add",
        "--project",
        UNICODE_PROJECT,
        "--title",
        "  Human title  ",
        "--objective",
        "Human objective.",
    )

    assert exit_code == 0
    assert stderr == ""
    tasks = db.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert stdout == f"Added {task.id} [READY] {task.title}\n"
    # The human path stores the request verbatim and applies the historical defaults.
    assert task.title == "  Human title  "
    assert task.risk == "medium"
    assert task.priority == 100
    assert task.max_attempts == 3
    assert task.acceptance == []
    assert task.validation_commands == []


def test_human_readable_task_add_still_rejects_an_unknown_project(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)

    exit_code, stdout, stderr = _run(
        config,
        capsys,
        "task",
        "add",
        "--project",
        "missing",
        "--title",
        "Title",
        "--objective",
        "Objective.",
    )

    assert exit_code == 2
    assert stdout == ""
    assert "Unknown project: missing" in stderr
    assert db.list_tasks() == []


def test_human_readable_task_add_honours_explicit_values(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, _, _ = _run(
        config,
        capsys,
        "task",
        "add",
        "--project",
        UNICODE_PROJECT,
        "--title",
        "Explicit",
        "--objective",
        "Objective.",
        "--risk",
        "critical",
        "--priority",
        "7",
        "--max-attempts",
        "9",
    )

    assert exit_code == 0
    task = db.list_tasks()[0]
    assert (task.risk, task.priority, task.max_attempts) == ("critical", 7, 9)


def test_human_readable_dry_run_reports_without_writing(tmp_path: Path, capsys):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    exit_code, stdout, stderr = _run(
        config, capsys, *_submit_arguments(**{"--id": "TASK-HUMAN-DRY"}), "--dry-run"
    )

    assert exit_code == 0
    assert stderr == ""
    assert stdout == "Would add TASK-HUMAN-DRY [READY] Add deterministic report export\n"
    assert db.list_tasks() == []


def test_human_readable_dry_run_reports_a_structured_code_on_failure(tmp_path: Path, capsys):
    config, _ = _configured_db(tmp_path)

    exit_code, stdout, stderr = _run(
        config, capsys, *_submit_arguments(**{"--project": "missing"}), "--dry-run"
    )

    assert exit_code == 2
    assert stdout == ""
    assert "PROJECT_NOT_FOUND" in stderr


def test_submitted_task_is_visible_to_the_existing_schema_1_0_read_commands(
    tmp_path: Path, capsys
):
    config, db = _configured_db(tmp_path)
    _register_project(db, tmp_path)

    _, submission, _, _ = _run_json(
        config,
        capsys,
        *_submit_arguments(
            **{
                "--id": "TASK-READBACK",
                "--acceptance": ["Existing tests pass."],
                "--validation": ["python -m pytest"],
            }
        ),
        "--json",
    )
    submitted = submission["data"]["task"]

    show_code, show, _, _ = _run_json(config, capsys, "task", "show", "TASK-READBACK", "--json")
    list_code, listed, _, _ = _run_json(config, capsys, "task", "list", "--json")
    status_code, status, _, _ = _run_json(config, capsys, "status", "--json")

    assert (show_code, list_code, status_code) == (0, 0, 0)
    assert show["schema_version"] == listed["schema_version"] == status["schema_version"] == "1.0"

    shown = show["data"]["task"]
    for name in (
        "task_id",
        "project",
        "project_id",
        "title",
        "objective",
        "state",
        "risk",
        "max_attempts",
        "token_limit",
        "branch",
        "worktree",
        "created_at",
        "updated_at",
    ):
        assert shown[name] == submitted[name], name

    assert [task["task_id"] for task in listed["data"]["tasks"]] == ["TASK-READBACK"]
    assert status["data"]["tasks"][TaskState.READY.value] == 1
    assert db.get_task("TASK-READBACK") is not None
