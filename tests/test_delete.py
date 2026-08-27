from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_forge.cli import (
    _remove_worktree_claimed,
    cmd_project_add,
    main,
    cmd_project_delete,
    cmd_task_add,
    cmd_task_delete,
    cmd_task_evidence_list,
    cmd_task_submit,
)
from codex_forge.config import ForgeConfig
from codex_forge.control_plane import error_exit_code
from codex_forge.db import ACTIVE_TASK_STATES, ForgeDB
from codex_forge.evidence import (
    EvidenceError,
    compute_artifact_id,
    evidence_list,
    evidence_show,
    retained_evidence_claim,
)
from codex_forge import evidence as evidence_module
from codex_forge import gitops as gitops_module
from codex_forge.orchestrator import Orchestrator
from codex_forge.task_submission import TaskSubmissionError, insert_task_guarded
from codex_forge.utils import derived_worktree_path
from codex_forge.gitops import GitError, prepare_worktree, remove_worktree
from codex_forge.models import Project, Task, TaskState

NON_RUNNING_STATES = tuple(state for state in TaskState if state not in ACTIVE_TASK_STATES)


def make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repos",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "evidence",
        knowledge_root=tmp_path / "knowledge",
    )


def make_db(config: ForgeConfig) -> ForgeDB:
    db = ForgeDB(config.database_path)
    db.initialize()
    return db


def add_project(db: ForgeDB, repo_path: Path, name: str = "demo") -> Project:
    # The registered project, carrying the registration identity Forge minted: the locked
    # admission guard requires the snapshot it is handed to name the generation it resolved.
    return db.add_project(Project(id=f"project-{name}", name=name, repo_path=repo_path))


def add_task(
    db: ForgeDB,
    project: Project,
    task_id: str = "TASK-1",
    state: TaskState = TaskState.READY,
    **kwargs,
) -> Task:
    task = Task(
        id=task_id,
        project_id=project.id,
        title="Deletable",
        objective="delete",
        acceptance=[],
        validation_commands=[],
        state=state,
        **kwargs,
    )
    db.add_task(task)
    return task


def task_args(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(task_id=task_id, json=True)


def project_args(project: str) -> SimpleNamespace:
    return SimpleNamespace(project=project, json=True)


def response(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout


def initialize_repository(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Forge Delete Test")
    git(repo, "config", "user.email", "forge-delete@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    return repo


def registered_worktrees(repo: Path) -> list[str]:
    return [
        os.path.normcase(os.path.normpath(line[len("worktree ") :]))
        for line in git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


@pytest.mark.parametrize("state", NON_RUNNING_STATES)
def test_each_non_running_state_deletes(tmp_path: Path, capsys, state: TaskState):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=state)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    payload = response(capsys)
    assert payload["success"] is True
    assert payload["data"]["deleted"] is True
    assert payload["data"]["task_id"] == task.id
    assert payload["data"]["evidence_retained"] is True
    assert db.get_task(task.id) is None


@pytest.mark.parametrize("state", ACTIVE_TASK_STATES)
def test_each_active_state_refuses(tmp_path: Path, capsys, state: TaskState):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=state)
    assert cmd_task_delete(config, task_args(task.id)) == 2
    payload = response(capsys)
    assert payload["success"] is False
    assert payload["error"]["code"] == "TASK_ACTIVE"
    assert payload["error"]["details"]["state"] == state.value
    assert db.get_task(task.id).state is state


def test_task_delete_leaves_no_owned_rows(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    doomed = add_task(db, project, task_id="TASK-1", state=TaskState.NEEDS_DECISION)
    kept = add_task(db, project, task_id="TASK-2", state=TaskState.READY)
    for task in (doomed, kept):
        db.add_decision(task.id, "which way?", ["a", "b"], None)
        run_id = db.record_run_start(task.id, "implement", "model", "medium", tmp_path / "art")
        db.record_run_end(run_id, thread_id=None, exit_code=0, usage={})
    assert cmd_task_delete(config, task_args(doomed.id)) == 0
    with db.connect() as connection:
        for table in ("decisions", "runs"):
            gone = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE task_id=?", (doomed.id,)
            ).fetchone()[0]
            remaining = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE task_id=?", (kept.id,)
            ).fetchone()[0]
            assert gone == 0, table
            assert remaining == 1, table
    assert db.get_task(doomed.id) is None
    assert db.get_task(kept.id) is not None


# ---------------------------------------------------------------------------------------------
# Retained evidence stays readable after deletion
# ---------------------------------------------------------------------------------------------


def seed_evidence(config: ForgeConfig, project_name: str, task_id: str, content: str = "{}") -> Path:
    artifact = config.evidence_root / project_name / task_id / "attempt-01" / "task-result.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(content, encoding="utf-8")
    return artifact


def evidence_list_args(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(task_id=task_id, json=True, schema_version=None)


def test_evidence_remains_listable_after_task_delete(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    artifact = seed_evidence(config, project.name, task.id)

    before = evidence_list(config, task.id, schema_version=None)
    assert before["task_registered"] is True
    assert before["task_state"] == "FAILED"
    assert before["attempt_count"] == 0
    assert before["evidence_available"] is True

    assert cmd_task_delete(config, task_args(task.id)) == 0
    payload = response(capsys)
    assert payload["data"]["evidence_retained"] is True
    assert artifact.exists()

    after = evidence_list(config, task.id, schema_version=None)
    assert after["task_registered"] is False
    assert after["task_state"] is None
    assert after["attempt_count"] is None
    assert after["task_id"] == task.id
    assert after["project"] == project.name
    assert after["evidence_available"] is True
    assert [item["artifact_id"] for item in after["artifacts"]] == [
        item["artifact_id"] for item in before["artifacts"]
    ]
    # The delete-time evidence_path names exactly the root the fallback later resolves.
    assert Path(payload["data"]["evidence_path"]) == (
        config.evidence_root / after["project"] / after["task_id"]
    )


def test_artifact_show_is_identical_before_and_after_delete(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    content = '{"status": "completed", "summary": "done"}'
    seed_evidence(config, project.name, task.id, content=content)
    artifact_id = compute_artifact_id(task.id, "attempt-01/task-result.json")

    before = evidence_show(config, task.id, artifact_id, schema_version=None)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    capsys.readouterr()
    after = evidence_show(config, task.id, artifact_id, schema_version=None)

    expected_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert before["content"]["sha256"] == expected_sha
    assert after["content"]["sha256"] == expected_sha
    assert after["content"] == before["content"]
    assert after["artifact"]["artifact_id"] == artifact_id
    assert before["task_state"] == "FAILED"
    assert after["task_state"] is None


def test_deleted_task_without_evidence_is_task_not_found(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.READY)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    capsys.readouterr()
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, task.id, schema_version=None)
    assert excinfo.value.code == "TASK_NOT_FOUND"


def test_live_task_row_wins_over_stale_retained_evidence(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    alpha = add_project(db, tmp_path / "repo-a", name="alpha")
    add_task(db, alpha, task_id="TASK-X", state=TaskState.FAILED)
    seed_evidence(config, "alpha", "TASK-X")
    assert cmd_task_delete(config, task_args("TASK-X")) == 0
    capsys.readouterr()

    beta = add_project(db, tmp_path / "repo-b", name="beta")
    add_task(db, beta, task_id="TASK-X", state=TaskState.READY)
    live = evidence_list(config, "TASK-X", schema_version=None)
    assert live["task_registered"] is True
    assert live["project"] == "beta"
    # The stale retained evidence under alpha is never selected while a registry row exists.
    assert live["evidence_available"] is False
    assert live["artifacts"] == []

    seed_evidence(config, "beta", "TASK-X")
    assert cmd_task_delete(config, task_args("TASK-X")) == 0
    capsys.readouterr()
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, "TASK-X", schema_version=None)
    assert excinfo.value.code == "EVIDENCE_AMBIGUOUS"


def test_ambiguous_retained_evidence_fails_closed_with_safe_details(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    make_db(config)
    seed_evidence(config, "alpha", "TASK-X")
    seed_evidence(config, "beta", "TASK-X")

    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, "TASK-X", schema_version=None)
    error = excinfo.value
    assert error.code == "EVIDENCE_AMBIGUOUS"
    assert error_exit_code(error.code) == 2
    assert set(error.details) == {"task_id", "candidate_projects"}
    assert error.details["task_id"] == "TASK-X"
    assert error.details["candidate_projects"] == ["alpha", "beta"]
    for value in [error.details["task_id"], *error.details["candidate_projects"]]:
        assert "\\" not in value and "/" not in value and ":" not in value

    with pytest.raises(EvidenceError) as show_excinfo:
        evidence_show(
            config,
            "TASK-X",
            compute_artifact_id("TASK-X", "attempt-01/task-result.json"),
            schema_version=None,
        )
    assert show_excinfo.value.code == "EVIDENCE_AMBIGUOUS"

    assert cmd_task_evidence_list(config, evidence_list_args("TASK-X")) == 2
    captured = capsys.readouterr()
    assert "ERROR: EVIDENCE_AMBIGUOUS" in captured.err
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert payload["error"]["code"] == "EVIDENCE_AMBIGUOUS"


def test_project_delete_keeps_retained_task_evidence_readable(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    content = '{"kept": true}'
    seed_evidence(config, project.name, task.id, content=content)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    capsys.readouterr()
    assert cmd_project_delete(config, project_args(project.name)) == 0
    assert response(capsys)["data"]["deleted"] is True
    assert db.get_project(project.name) is None

    listing = evidence_list(config, task.id, schema_version=None)
    assert listing["task_registered"] is False
    assert listing["project"] == project.name
    assert listing["evidence_available"] is True
    shown = evidence_show(
        config,
        task.id,
        compute_artifact_id(task.id, "attempt-01/task-result.json"),
        schema_version=None,
    )
    assert shown["content"]["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_fallback_ignores_files_links_and_unsafe_ids(tmp_path: Path):
    config = make_config(tmp_path)
    make_db(config)
    seed_evidence(config, "alpha", "TASK-X")
    # A plain file at the evidence-root level is not a project directory.
    (config.evidence_root / "not-a-project").write_text("x", encoding="utf-8")
    # A plain file named exactly like the task id is not a task evidence root.
    beta = config.evidence_root / "beta"
    beta.mkdir()
    (beta / "TASK-X").write_text("x", encoding="utf-8")
    # A linked project or task directory is inspected as itself and refused, never followed.
    try:
        (config.evidence_root / "gamma").symlink_to(
            config.evidence_root / "alpha", target_is_directory=True
        )
        delta = config.evidence_root / "delta"
        delta.mkdir()
        (delta / "TASK-X").symlink_to(
            config.evidence_root / "alpha" / "TASK-X", target_is_directory=True
        )
    except OSError:
        pass  # Symlink creation is not permitted here; the plain-file decoys still apply.

    listing = evidence_list(config, "TASK-X", schema_version=None)
    assert listing["project"] == "alpha"

    # An unsafe task id cannot address the evidence tree at all.
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, "..", schema_version=None)
    assert excinfo.value.code == "TASK_NOT_FOUND"


def test_fallback_task_id_match_is_exact(tmp_path: Path):
    config = make_config(tmp_path)
    make_db(config)
    for task_id in ("TASK", "TASK-1", "TASK-10"):
        seed_evidence(config, "alpha", task_id)
    for task_id in ("TASK", "TASK-1", "TASK-10"):
        listing = evidence_list(config, task_id, schema_version=None)
        assert listing["task_id"] == task_id
        assert listing["project"] == "alpha"
        assert listing["artifact_count"] == 1
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, "TASK-2", schema_version=None)
    assert excinfo.value.code == "TASK_NOT_FOUND"


def test_worktree_removed_and_branch_retained(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    task = add_task(db, project, state=TaskState.READY)
    branch, worktree = prepare_worktree(project, task, config.workspace_root)
    db.update_task(task.id, branch=branch, worktree_path=worktree)
    normalized = os.path.normcase(os.path.normpath(str(worktree)))
    assert normalized in registered_worktrees(repo)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    payload = response(capsys)
    assert payload["data"]["worktree_removed"] is True
    assert not worktree.exists()
    assert normalized not in registered_worktrees(repo)
    # The branch and its commits stay verifiable for retained evidence.
    git(repo, "show-ref", "--verify", f"refs/heads/{branch}")


def test_missing_worktree_is_harmless(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=tmp_path / "gone")
    assert cmd_task_delete(config, task_args(task.id)) == 0
    payload = response(capsys)
    assert payload["data"]["deleted"] is True
    assert payload["data"]["worktree_removed"] is False
    assert db.get_task(task.id) is None


def test_cleanup_failure_is_surfaced_without_resurrecting_the_task(
    tmp_path: Path, capsys, monkeypatch
):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=tmp_path / "locked")

    def refuse(
        repo: Path, worktree: Path, containment_root: Path, protected, foreign_repositories=()
    ) -> bool:
        raise GitError("worktree is locked")

    monkeypatch.setattr("codex_forge.cli.remove_worktree", refuse)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    captured = capsys.readouterr()
    assert "ERROR: WORKTREE_CLEANUP_FAILED" in captured.err
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert payload["error"]["details"]["deleted"] is True
    assert payload["error"]["details"]["worktree_removed"] is False
    assert db.get_task(task.id) is None


def test_project_cleanup_failure_is_surfaced_without_resurrecting_the_project(
    tmp_path: Path, capsys, monkeypatch
):
    config = make_config(tmp_path)
    db = make_db(config)
    # A real repository lets the flow pass the registration inventory and reach the rmtree
    # refusal this test exists to prove.
    project = add_project(db, initialize_repository(tmp_path))
    (config.workspace_root / project.name).mkdir(parents=True)

    def refuse(path):
        raise OSError("directory is locked")

    monkeypatch.setattr("codex_forge.cli.shutil.rmtree", refuse)
    assert cmd_project_delete(config, project_args(project.name)) == 1
    captured = capsys.readouterr()
    assert "ERROR: PROJECT_CLEANUP_FAILED" in captured.err
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert payload["error"]["details"]["deleted"] is True
    assert db.get_project(project.name) is None


def test_unknown_and_already_deleted_task_are_deterministic(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.READY)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    capsys.readouterr()
    for _ in range(2):
        assert cmd_task_delete(config, task_args(task.id)) == 2
        payload = response(capsys)
        assert payload["error"]["code"] == "TASK_NOT_FOUND"


def test_exact_task_id_only(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    add_task(db, project, task_id="TASK-1", state=TaskState.READY)
    add_task(db, project, task_id="TASK-10", state=TaskState.READY)
    assert cmd_task_delete(config, task_args("TASK")) == 2
    assert response(capsys)["error"]["code"] == "TASK_NOT_FOUND"
    assert cmd_task_delete(config, task_args("TASK-1")) == 0
    capsys.readouterr()
    assert db.get_task("TASK-1") is None
    assert db.get_task("TASK-10") is not None


def test_claimed_task_cannot_be_deleted(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.READY)
    assert db.claim_ready_tasks(1)
    assert cmd_task_delete(config, task_args(task.id)) == 2
    assert response(capsys)["error"]["code"] == "TASK_ACTIVE"
    assert db.get_task(task.id).state is TaskState.IMPLEMENTING


def test_deleted_task_cannot_be_claimed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.READY)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    assert db.claim_ready_tasks(5) == []


def test_delete_serializes_against_an_inflight_claim(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.READY)
    claim_started = threading.Event()

    def claim_and_hold():
        connection = sqlite3.connect(db.path, timeout=30)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE tasks SET state=? WHERE id=? AND state=?",
                (TaskState.IMPLEMENTING.value, task.id, TaskState.READY.value),
            )
            claim_started.set()
            time.sleep(0.3)
            connection.commit()
        finally:
            connection.close()

    thread = threading.Thread(target=claim_and_hold)
    thread.start()
    assert claim_started.wait(timeout=10)
    # Blocks on the claim's write lock; once it commits, the delete must observe the claim.
    outcome, snapshot = db.delete_task(task.id)
    thread.join(timeout=10)
    assert outcome == "active"
    assert snapshot.state is TaskState.IMPLEMENTING
    assert db.get_task(task.id).state is TaskState.IMPLEMENTING


def test_empty_project_deletes_and_workspace_is_removed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    # Workspace removal inventories the repository's worktree registrations first, so the
    # repository must be a real, verifiable Git repository for the cleanup to proceed.
    project = add_project(db, initialize_repository(tmp_path))
    stale = config.workspace_root / project.name / "stale-task"
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("stale\n", encoding="utf-8")
    assert cmd_project_delete(config, project_args(project.name)) == 0
    payload = response(capsys)
    assert payload["success"] is True
    assert payload["data"]["deleted"] is True
    assert payload["data"]["project"] == project.name
    assert payload["data"]["evidence_retained"] is True
    assert payload["data"]["repository_removed"] is False
    assert payload["data"]["workspace_removed"] is True
    assert not (config.workspace_root / project.name).exists()
    assert db.get_project(project.name) is None
    assert db.list_projects() == []


def test_project_delete_accepts_exact_id(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    assert cmd_project_delete(config, project_args(project.id)) == 0
    assert response(capsys)["data"]["project_id"] == project.id
    assert db.get_project(project.id) is None


@pytest.mark.parametrize("state", [TaskState.READY, TaskState.COMPLETED])
def test_project_with_any_task_refuses_without_cascade(tmp_path: Path, capsys, state: TaskState):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=state)
    assert cmd_project_delete(config, project_args(project.name)) == 2
    payload = response(capsys)
    assert payload["error"]["code"] == "PROJECT_NOT_EMPTY"
    assert payload["error"]["details"]["task_count"] == 1
    assert db.get_project(project.name) is not None
    assert db.get_task(task.id) is not None


def test_absent_project_is_deterministic(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    make_db(config)
    for _ in range(2):
        assert cmd_project_delete(config, project_args("missing")) == 2
        assert response(capsys)["error"]["code"] == "PROJECT_NOT_FOUND"


def test_project_delete_retains_evidence_and_user_repository(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "keep.txt").write_text("user source\n", encoding="utf-8")
    project = add_project(db, repo)
    artifact = config.evidence_root / project.name / "TASK-OLD" / "attempt-01" / "task-result.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    assert cmd_project_delete(config, project_args(project.name)) == 0
    assert response(capsys)["data"]["deleted"] is True
    assert artifact.exists()
    assert (repo / "keep.txt").exists()


def test_registry_stays_consistent_after_project_delete(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    assert cmd_project_delete(config, project_args(project.name)) == 0
    capsys.readouterr()
    assert db.claim_ready_tasks(5) == []
    assert db.list_projects() == []
    readded = add_project(db, tmp_path / "repo")
    assert db.get_project(readded.name) is not None


# ---------------------------------------------------------------------------------------------
# Cleanup ownership, containment, and generation claims
# ---------------------------------------------------------------------------------------------


def submit_args(project: str, task_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        project=project,
        title="Collision probe",
        objective="probe",
        acceptance=[],
        decision=[],
        constraint=[],
        scope=None,
        out_of_scope=None,
        validation=[],
        tag=[],
        depends_on=[],
        risk=None,
        priority=None,
        max_attempts=None,
        id=task_id,
        json=True,
        dry_run=False,
        schema_version=None,
    )


def test_worktree_pointing_at_repository_is_refused(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=repo)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert (repo / "base.txt").exists()
    assert db.get_task(task.id) is None


def test_worktree_outside_workspace_root_is_refused(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    config.workspace_root.mkdir(parents=True)
    project = add_project(db, tmp_path / "repo")
    precious = tmp_path / "precious"
    (precious / "keep").mkdir(parents=True)
    (precious / "keep" / "data.txt").write_text("keep\n", encoding="utf-8")
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=precious)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert (precious / "keep" / "data.txt").exists()
    assert db.get_task(task.id) is None


def test_unregistered_checkout_inside_workspace_is_removed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    # Removal of an on-disk checkout requires the repository to verifiably testify that the
    # checkout is unregistered before any raw deletion.
    project = add_project(db, initialize_repository(tmp_path))
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    (checkout / "scratch.txt").write_text("scratch\n", encoding="utf-8")
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    payload = response(capsys)
    assert payload["data"]["worktree_removed"] is True
    assert not checkout.exists()


def test_reused_task_id_keeps_the_new_worktree(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)
    outcome, _snapshot = db.delete_task(task.id)
    assert outcome == "deleted"
    # The id is reused before the stale cleanup runs; the claim must leave the checkout alone.
    add_task(db, project, task_id=task.id, state=TaskState.READY, worktree_path=checkout)
    assert _remove_worktree_claimed(db, config, project, checkout, task.id) is False
    assert checkout.exists()


def test_checkout_path_claimed_by_another_task_is_kept(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, initialize_repository(tmp_path))
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    first = add_task(db, project, task_id="TASK-1", state=TaskState.FAILED, worktree_path=checkout)
    outcome, _snapshot = db.delete_task(first.id)
    assert outcome == "deleted"
    add_task(db, project, task_id="TASK-2", state=TaskState.READY, worktree_path=checkout)
    assert _remove_worktree_claimed(db, config, project, checkout, "TASK-1") is False
    assert checkout.exists()
    outcome, _snapshot = db.delete_task("TASK-2")
    assert outcome == "deleted"
    assert _remove_worktree_claimed(db, config, project, checkout, "TASK-1") is True
    assert not checkout.exists()


def test_git_timeout_is_a_structured_cleanup_failure(tmp_path: Path, capsys, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)

    def hang(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=1)

    monkeypatch.setattr("codex_forge.gitops.run_process", hang)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    captured = capsys.readouterr()
    assert "ERROR: WORKTREE_CLEANUP_FAILED" in captured.err
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert db.get_task(task.id) is None


def test_unsafe_project_name_never_escapes_the_workspace(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    marker = tmp_path / "marker.txt"
    marker.write_text("intact\n", encoding="utf-8")
    evil = Project(id="project-evil", name="..", repo_path=tmp_path / "repo")
    db.add_project(evil)
    assert cmd_project_delete(config, project_args("project-evil")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    # Details identify the project by its safe backend-minted id, never by the path-shaped name.
    assert payload["error"]["details"]["project"] == "project-evil"
    assert marker.exists()
    assert db.get_project("project-evil") is None


def test_project_add_refuses_unsafe_and_case_variant_names(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)

    def project_add_args(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            url=None,
            repo=str(tmp_path / "repo"),
            default_branch="main",
            validation=[],
            no_fetch=True,
            no_commit=True,
            push=False,
        )

    assert cmd_project_add(config, project_add_args("../evil")) == 2
    assert db.list_projects() == []
    add_project(db, tmp_path / "repo", name="demo")
    assert cmd_project_add(config, project_add_args("Demo")) == 2
    assert [project.name for project in db.list_projects()] == ["demo"]
    capsys.readouterr()


def test_project_identifier_ambiguity_fails_closed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    alpha = add_project(db, tmp_path / "repo-a", name="alpha")
    trap = Project(id="project-trap", name=alpha.id, repo_path=tmp_path / "repo-b")
    db.add_project(trap)
    assert cmd_project_delete(config, project_args(alpha.id)) == 2
    payload = response(capsys)
    assert payload["error"]["code"] == "PROJECT_AMBIGUOUS"
    assert db.get_project("project-trap") is not None
    # Reads resolve deterministically: the exact id match always wins.
    assert db.get_project(alpha.id).name == "alpha"


def test_task_id_collision_with_retained_evidence(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    seed_evidence(config, project.name, task.id)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    capsys.readouterr()
    for reused_id in (task.id, task.id.lower()):
        rc = cmd_task_submit(config, submit_args(project.name, reused_id))
        payload = json.loads(capsys.readouterr().out)
        assert rc == 2
        assert payload["error"]["code"] == "TASK_ID_COLLISION"
        assert payload["error"]["details"]["collides_with"] == task.id
        assert db.get_task(reused_id) is None


def test_task_id_case_variant_of_live_task_collides(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    add_task(db, project, task_id="TASK-X", state=TaskState.READY)
    rc = cmd_task_submit(config, submit_args(project.name, "task-x"))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"]["code"] == "TASK_ID_COLLISION"
    assert payload["error"]["details"]["collides_with"] == "TASK-X"
    assert db.get_task("task-x") is None


def test_live_task_root_swapped_for_a_link_is_refused(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    seed_evidence(config, project.name, task.id)
    root = config.evidence_root / project.name / task.id
    outside = tmp_path / "outside"
    (outside / "attempt-01").mkdir(parents=True)
    (outside / "attempt-01" / "task-result.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(root)
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted in this environment")
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, task.id, schema_version=None)
    assert excinfo.value.code == "ARTIFACT_OUTSIDE_EVIDENCE_ROOT"


# ---------------------------------------------------------------------------------------------
# Owner-scoped cleanup, verified deregistration, atomic alias refusal
# ---------------------------------------------------------------------------------------------


def test_worktree_inside_another_projects_workspace_is_refused(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    alpha = add_project(db, tmp_path / "repo-a", name="alpha")
    add_project(db, tmp_path / "repo-b", name="beta")
    (config.workspace_root / "alpha").mkdir(parents=True)
    foreign = config.workspace_root / "beta" / "task-9"
    foreign.mkdir(parents=True)
    (foreign / "work.txt").write_text("beta's work\n", encoding="utf-8")
    task = add_task(db, alpha, state=TaskState.FAILED, worktree_path=foreign)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert (foreign / "work.txt").exists()
    assert db.get_task(task.id) is None


def test_worktree_overlapping_a_registered_repository_is_refused(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    alpha = add_project(db, tmp_path / "repo-a", name="alpha")
    nested_repo = config.workspace_root / "alpha" / "nested-repo"
    nested_repo.mkdir(parents=True)
    (nested_repo / "source.txt").write_text("source\n", encoding="utf-8")
    db.add_project(Project(id="project-beta", name="beta", repo_path=nested_repo))
    task = add_task(db, alpha, state=TaskState.FAILED, worktree_path=nested_repo)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert (nested_repo / "source.txt").exists()
    assert db.get_task(task.id) is None


def test_project_delete_keeps_live_foreign_checkout_inside_workspace(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    add_project(db, tmp_path / "repo-a", name="alpha")
    beta = add_project(db, tmp_path / "repo-b", name="beta")
    stray = config.workspace_root / "alpha" / "stray"
    stray.mkdir(parents=True)
    (stray / "work.txt").write_text("beta's work\n", encoding="utf-8")
    add_task(db, beta, task_id="TASK-B", state=TaskState.READY, worktree_path=stray)
    assert cmd_project_delete(config, project_args("alpha")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert (stray / "work.txt").exists()
    assert db.get_project("alpha") is None


def test_project_delete_keeps_a_registered_repository_inside_workspace(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    add_project(db, tmp_path / "repo-a", name="alpha")
    inner_repo = config.workspace_root / "alpha" / "inner-repo"
    inner_repo.mkdir(parents=True)
    (inner_repo / "source.txt").write_text("source\n", encoding="utf-8")
    db.add_project(Project(id="project-beta", name="beta", repo_path=inner_repo))
    assert cmd_project_delete(config, project_args("alpha")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert (inner_repo / "source.txt").exists()
    assert db.get_project("alpha") is None


def test_locked_worktree_registration_is_not_reported_removed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    task = add_task(db, project, state=TaskState.READY)
    branch, worktree = prepare_worktree(project, task, config.workspace_root)
    db.update_task(task.id, branch=branch, worktree_path=worktree)
    git(repo, "worktree", "lock", str(worktree))
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert payload["error"]["details"]["worktree_removed"] is False
    # The registration honestly survives; it is never claimed removed.
    normalized = os.path.normcase(os.path.normpath(str(worktree)))
    assert normalized in registered_worktrees(repo)
    # The directory also survives: registered removal is checked, never a raw-deletion fall-through.
    assert worktree.exists()
    assert db.get_task(task.id) is None


def test_unreadable_worktree_registration_fails_closed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    fake_repo = tmp_path / "notrepo"
    fake_repo.mkdir()
    project = add_project(db, fake_repo)
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert checkout.exists()
    assert db.get_task(task.id) is None


def test_trailing_dot_and_reserved_task_ids_are_refused(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    for bad_id in ("TASK-1.", "CON", "NUL.txt", "../evil"):
        rc = cmd_task_submit(config, submit_args(project.name, bad_id))
        payload = json.loads(capsys.readouterr().out)
        assert rc == 2, bad_id
        assert payload["error"]["code"] == "INVALID_TASK_REQUEST", bad_id
        assert db.get_task(bad_id) is None, bad_id


def test_plain_task_add_validates_the_id(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    args = submit_args(project.name, "../evil")
    args.json = False
    args.dry_run = False
    assert cmd_task_add(config, args) == 2
    captured = capsys.readouterr()
    assert "INVALID_TASK_REQUEST" in captured.err
    assert db.list_tasks() == []


def test_guarded_insert_is_atomic_with_the_collision_check(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    add_task(db, project, task_id="TASK-X", state=TaskState.READY)
    variant = Task(
        id="task-x",
        project_id=project.id,
        title="Variant",
        objective="alias",
        acceptance=[],
        validation_commands=[],
        state=TaskState.READY,
    )
    with pytest.raises(TaskSubmissionError) as excinfo:
        insert_task_guarded(db, config, project, variant)
    assert excinfo.value.code == "TASK_ID_COLLISION"
    assert db.get_task("task-x") is None


def test_sqlite_claim_failure_stays_in_the_cleanup_envelope(tmp_path: Path, capsys, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("codex_forge.cli._remove_worktree_claimed", locked)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    captured = capsys.readouterr()
    assert "ERROR: WORKTREE_CLEANUP_FAILED" in captured.err
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert payload["error"]["details"]["deleted"] is True
    assert db.get_task(task.id) is None


# ---------------------------------------------------------------------------------------------
# Global ownership, honest Git completion, atomic namespace, swap windows
# ---------------------------------------------------------------------------------------------


def _swap_root_for_link(root: Path, tmp_path: Path) -> None:
    """Replace the real task evidence tree with a symlink to a copy held outside the root."""
    outside = tmp_path / "outside-tree"
    shutil.move(str(root), str(outside))
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        shutil.move(str(outside), str(root))
        pytest.skip("symlink creation is not permitted in this environment")


def test_worktree_equal_to_or_above_the_workspace_is_refused(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    own_workspace = config.workspace_root / project.name
    own_workspace.mkdir(parents=True)
    (own_workspace / "sibling.txt").write_text("sibling\n", encoding="utf-8")
    first = add_task(
        db, project, task_id="TASK-1", state=TaskState.FAILED, worktree_path=own_workspace
    )
    assert cmd_task_delete(config, task_args(first.id)) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert (own_workspace / "sibling.txt").exists()
    second = add_task(
        db, project, task_id="TASK-2", state=TaskState.FAILED, worktree_path=config.workspace_root
    )
    assert cmd_task_delete(config, task_args(second.id)) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert own_workspace.exists()


def test_worktree_descending_into_a_registered_repository_is_refused(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    alpha = add_project(db, tmp_path / "repo-a", name="alpha")
    nested_repo = config.workspace_root / "alpha" / "nested-repo"
    inner = nested_repo / "src"
    inner.mkdir(parents=True)
    (inner / "code.txt").write_text("source\n", encoding="utf-8")
    db.add_project(Project(id="project-beta", name="beta", repo_path=nested_repo))
    task = add_task(db, alpha, state=TaskState.FAILED, worktree_path=inner)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert (inner / "code.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="case aliasing applies to Windows paths")
def test_case_twisted_path_to_protected_state_is_refused(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    alpha = add_project(db, tmp_path / "repo-a", name="alpha")
    nested_repo = config.workspace_root / "alpha" / "nested-repo"
    nested_repo.mkdir(parents=True)
    (nested_repo / "code.txt").write_text("source\n", encoding="utf-8")
    db.add_project(Project(id="project-beta", name="beta", repo_path=nested_repo))
    twisted = Path(str(nested_repo).upper())
    task = add_task(db, alpha, state=TaskState.FAILED, worktree_path=twisted)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert (nested_repo / "code.txt").exists()


def test_derived_checkout_of_a_live_task_is_kept(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    checkout = derived_worktree_path(config.workspace_root, project.name, "TASK-2")
    checkout.mkdir(parents=True)
    add_task(db, project, task_id="TASK-1", state=TaskState.FAILED, worktree_path=checkout)
    # TASK-2 is active and has no persisted path yet; its derived checkout is still its own.
    add_task(db, project, task_id="TASK-2", state=TaskState.IMPLEMENTING)
    assert cmd_task_delete(config, task_args("TASK-1")) == 0
    payload = response(capsys)
    assert payload["data"]["worktree_removed"] is False
    assert checkout.exists()


def test_slug_colliding_task_ids_get_distinct_checkouts(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, initialize_repository(tmp_path))
    first_path = derived_worktree_path(config.workspace_root, project.name, "TASK-A@B")
    second_path = derived_worktree_path(config.workspace_root, project.name, "TASK-A-B")
    assert first_path != second_path
    first_path.mkdir(parents=True)
    second_path.mkdir(parents=True)
    add_task(db, project, task_id="TASK-A@B", state=TaskState.FAILED, worktree_path=first_path)
    add_task(db, project, task_id="TASK-A-B", state=TaskState.READY, worktree_path=second_path)
    assert cmd_task_delete(config, task_args("TASK-A@B")) == 0
    payload = response(capsys)
    assert payload["data"]["worktree_removed"] is True
    assert not first_path.exists()
    assert second_path.exists()


def test_prune_failure_fails_closed(tmp_path: Path, capsys, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    task = add_task(db, project, state=TaskState.READY)
    branch, worktree = prepare_worktree(project, task, config.workspace_root)
    db.update_task(task.id, branch=branch, worktree_path=worktree)
    shutil.rmtree(worktree)  # The checkout vanished; a stale registration remains.
    real_run = gitops_module.run_process

    def failing(command, **kwargs):
        if "prune" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="prune refused")
        return real_run(command, **kwargs)

    monkeypatch.setattr("codex_forge.gitops.run_process", failing)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert payload["error"]["details"]["deleted"] is True
    assert db.get_task(task.id) is None


def test_project_add_case_variant_race_is_refused_in_transaction(tmp_path: Path, capsys, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    add_project(db, tmp_path / "repo-a", name="demo")
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    git(repo_b, "init", "-b", "main")
    # Simulate the check-then-insert race: the pre-check sees no projects, so only the
    # in-transaction guard can refuse the case variant.
    monkeypatch.setattr(ForgeDB, "list_projects", lambda self: [])
    args = SimpleNamespace(
        name="Demo",
        url=None,
        repo=str(repo_b),
        default_branch="main",
        validation=[],
        no_fetch=True,
        no_commit=True,
        push=False,
    )
    assert cmd_project_add(config, args) == 2
    captured = capsys.readouterr()
    assert "INVALID_ARGUMENT" in captured.err
    with db.connect() as connection:
        names = [row["name"] for row in connection.execute("SELECT name FROM projects")]
    assert names == ["demo"]


def test_task_case_variant_race_is_refused_in_transaction(tmp_path: Path, capsys, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    add_task(db, project, task_id="TASK-X", state=TaskState.READY)
    # Simulate the race: the pre-check reports no collision, so only the guard sharing the
    # insert's transaction can refuse.
    monkeypatch.setattr(
        "codex_forge.task_submission.task_id_collision", lambda *args, **kwargs: None
    )
    rc = cmd_task_submit(config, submit_args(project.name, "task-x"))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"]["code"] == "TASK_ID_COLLISION"
    assert db.get_task("task-x") is None


def test_plain_task_add_inserts_through_the_guarded_seam(tmp_path: Path, capsys, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")

    def unguarded(self, task):
        raise AssertionError("plain task add must use the guarded insert")

    monkeypatch.setattr(ForgeDB, "add_task", unguarded)
    args = submit_args(project.name, "TASK-PLAIN")
    args.json = False
    args.dry_run = False
    assert cmd_task_add(config, args) == 0
    capsys.readouterr()
    assert db.get_task("TASK-PLAIN") is not None

    variant = submit_args(project.name, "task-plain")
    variant.json = False
    variant.dry_run = False
    assert cmd_task_add(config, variant) == 2
    captured = capsys.readouterr()
    assert "TASK_ID_COLLISION" in captured.err
    assert db.get_task("task-plain") is None


def test_legacy_alias_rows_fail_closed_on_evidence_reads(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    add_task(db, project, task_id="TASK-X", state=TaskState.READY)
    add_task(db, project, task_id="task-x", state=TaskState.READY)
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, "TASK-X", schema_version=None)
    assert excinfo.value.code == "EVIDENCE_AMBIGUOUS"
    assert excinfo.value.details["aliased_with"] == "task-x"


def test_retained_alias_directory_fails_closed(tmp_path: Path):
    config = make_config(tmp_path)
    make_db(config)
    seed_evidence(config, "alpha", "task-x")
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, "TASK-X", schema_version=None)
    assert excinfo.value.code == "EVIDENCE_AMBIGUOUS"


def test_root_swap_after_verification_is_refused(tmp_path: Path, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    seed_evidence(config, project.name, task.id)
    root = config.evidence_root / project.name / task.id
    real_discover = evidence_module.discover_artifacts

    def swap_then_discover(task_root, task_id):
        _swap_root_for_link(root, tmp_path)
        return real_discover(task_root, task_id)

    monkeypatch.setattr("codex_forge.evidence.discover_artifacts", swap_then_discover)
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, task.id, schema_version=None)
    assert excinfo.value.code == "ARTIFACT_OUTSIDE_EVIDENCE_ROOT"


def test_root_swap_during_discovery_is_refused(tmp_path: Path, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    seed_evidence(config, project.name, task.id)
    root = config.evidence_root / project.name / task.id
    real_scandir = evidence_module._scandir
    state = {"swapped": False}

    def swapping_scandir(directory):
        entries = real_scandir(directory)
        if not state["swapped"]:
            state["swapped"] = True
            _swap_root_for_link(root, tmp_path)
        return entries

    monkeypatch.setattr("codex_forge.evidence._scandir", swapping_scandir)
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, task.id, schema_version=None)
    assert excinfo.value.code == "ARTIFACT_OUTSIDE_EVIDENCE_ROOT"


def test_root_swap_before_show_opens_is_refused(tmp_path: Path, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    seed_evidence(config, project.name, task.id)
    root = config.evidence_root / project.name / task.id
    real_discover = evidence_module.discover_artifacts

    def discover_then_swap(task_root, task_id):
        artifacts = real_discover(task_root, task_id)
        _swap_root_for_link(root, tmp_path)
        return artifacts

    monkeypatch.setattr("codex_forge.evidence.discover_artifacts", discover_then_swap)
    with pytest.raises(EvidenceError) as excinfo:
        evidence_show(
            config,
            task.id,
            compute_artifact_id(task.id, "attempt-01/task-result.json"),
            schema_version=None,
        )
    assert excinfo.value.code == "ARTIFACT_OUTSIDE_EVIDENCE_ROOT"


def test_fallback_root_swap_after_verification_is_refused(tmp_path: Path, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    seed_evidence(config, project.name, task.id)
    outcome, _snapshot = db.delete_task(task.id)
    assert outcome == "deleted"
    root = config.evidence_root / project.name / task.id
    real_discover = evidence_module.discover_artifacts

    def swap_then_discover(task_root, task_id):
        _swap_root_for_link(root, tmp_path)
        return real_discover(task_root, task_id)

    monkeypatch.setattr("codex_forge.evidence.discover_artifacts", swap_then_discover)
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, task.id, schema_version=None)
    assert excinfo.value.code == "ARTIFACT_OUTSIDE_EVIDENCE_ROOT"


# ---------------------------------------------------------------------------------------------
# Fail-closed sweeps, canonical registrations, alias keys, protected roots, coherent snapshots
# ---------------------------------------------------------------------------------------------


def test_unsafe_sibling_project_name_fails_cleanup_closed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)
    db.add_project(Project(id="project-evil", name="..", repo_path=tmp_path / "repo-evil"))
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert checkout.exists()
    assert db.get_task(task.id) is None

    # The same unprovable ownership blocks project workspace cleanup.
    add_project(db, tmp_path / "repo-clean", name="clean")
    (config.workspace_root / "clean").mkdir(parents=True)
    assert cmd_project_delete(config, project_args("clean")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert (config.workspace_root / "clean").exists()
    assert db.get_project("clean") is None


def test_symlink_aliased_checkout_clears_its_registration(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    task = add_task(db, project, state=TaskState.READY)
    branch, worktree = prepare_worktree(project, task, config.workspace_root)
    alias_parent = config.workspace_root / "demo-alias"
    try:
        alias_parent.symlink_to(config.workspace_root / project.name, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted in this environment")
    aliased = alias_parent / worktree.name
    db.update_task(task.id, branch=branch, worktree_path=aliased)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    payload = response(capsys)
    assert payload["data"]["worktree_removed"] is True
    assert not worktree.exists()
    normalized = os.path.normcase(os.path.normpath(str(worktree)))
    assert normalized not in registered_worktrees(repo)


def test_legacy_trailing_dot_aliases_are_refused(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    db.add_project(Project(id="project-legacy", name="demo.", repo_path=tmp_path / "repo-legacy"))
    args = SimpleNamespace(
        name="demo",
        url=None,
        repo=str(tmp_path / "repo"),
        default_branch="main",
        validation=[],
        no_fetch=True,
        no_commit=True,
        push=False,
    )
    assert cmd_project_add(config, args) == 2
    captured = capsys.readouterr()
    assert "INVALID_ARGUMENT" in captured.err

    project = add_project(db, tmp_path / "repo-live", name="live")
    add_task(db, project, task_id="TASK-X.", state=TaskState.READY)
    rc = cmd_task_submit(config, submit_args("live", "TASK-X"))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"]["code"] == "TASK_ID_COLLISION"
    assert payload["error"]["details"]["collides_with"] == "TASK-X."


def test_overlapping_evidence_root_is_protected_from_cleanup(tmp_path: Path, capsys):
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repos",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "worktrees" / "alpha" / "evidence",
        knowledge_root=tmp_path / "knowledge",
    )
    db = make_db(config)
    add_project(db, tmp_path / "repo", name="alpha")
    retained = config.evidence_root / "alpha" / "TASK-1" / "attempt-01"
    retained.mkdir(parents=True)
    (retained / "task-result.json").write_text("{}", encoding="utf-8")
    assert cmd_project_delete(config, project_args("alpha")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert (retained / "task-result.json").exists()
    assert db.get_project("alpha") is None


def test_list_revalidates_before_returning(tmp_path: Path, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    seed_evidence(config, project.name, task.id)
    root = config.evidence_root / project.name / task.id
    real_probe = evidence_module._probe

    def probe_then_swap(artifact):
        result = real_probe(artifact)
        _swap_root_for_link(root, tmp_path)
        return result

    monkeypatch.setattr("codex_forge.evidence._probe", probe_then_swap)
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, task.id, schema_version=None)
    assert excinfo.value.code == "ARTIFACT_OUTSIDE_EVIDENCE_ROOT"


def test_manifest_hash_and_size_are_one_snapshot(tmp_path: Path, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    artifact = seed_evidence(config, project.name, task.id)
    grown = '{"grown": true}'
    real_discover = evidence_module.discover_artifacts

    def discover_then_rewrite(task_root, task_id):
        artifacts = real_discover(task_root, task_id)
        artifact.write_text(grown, encoding="utf-8")
        return artifacts

    monkeypatch.setattr("codex_forge.evidence.discover_artifacts", discover_then_rewrite)
    listing = evidence_list(config, task.id, schema_version=None)
    entry = listing["artifacts"][0]
    # Hash, size, and mtime come from the one snapshot the probe verified — never a mix of
    # discovery-time metadata and probe-time bytes.
    assert entry["sha256"] == hashlib.sha256(grown.encode("utf-8")).hexdigest()
    assert entry["original_size_bytes"] == len(grown.encode("utf-8"))


def test_project_lookup_failure_after_commit_stays_in_envelope(tmp_path: Path, capsys, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)

    def broken(self, identifier):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ForgeDB, "get_project", broken)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    captured = capsys.readouterr()
    assert "ERROR: WORKTREE_CLEANUP_FAILED" in captured.err
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert payload["error"]["details"]["deleted"] is True
    assert db.get_task(task.id) is None


def test_error_envelopes_preserve_requested_schema_version(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    make_db(config)
    # Evidence error path.
    args = SimpleNamespace(task_id="TASK-missing", json=True, schema_version="1.1")
    assert cmd_task_evidence_list(config, args, schema_version="1.1") == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1.1"
    assert payload["error"]["code"] == "TASK_NOT_FOUND"
    # Parse error path (fires before argparse succeeds).
    assert main(["task", "evidence", "list", "--json", "--schema-version", "1.1"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1.1"
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    # Config error path.
    exit_code = main(
        [
            "--config",
            str(tmp_path / "missing.toml"),
            "task",
            "evidence",
            "list",
            "TASK-1",
            "--json",
            "--schema-version",
            "1.1",
        ]
    )
    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1.1"
    assert payload["error"]["code"] == "CONFIG_ERROR"


# ---------------------------------------------------------------------------------------------
# Inbox submission authority
# ---------------------------------------------------------------------------------------------


def make_inbox_orchestrator(config: ForgeConfig, db: ForgeDB) -> Orchestrator:
    """The inbox seam needs only the database, the config, and the shared submission path."""
    orchestrator = object.__new__(Orchestrator)
    orchestrator.db = db
    orchestrator.config = config
    return orchestrator


def inbox_payload(project: str, task_id: str | None = None) -> dict:
    data = {"project": project, "title": "Inbox", "objective": "obj"}
    if task_id is not None:
        data["id"] = task_id
    return data


def test_inbox_accepts_a_valid_submission(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    make_inbox_orchestrator(config, db)._task_from_yaml(inbox_payload(project.name, "TASK-9"))
    task = db.get_task("TASK-9")
    assert task is not None
    assert task.state is TaskState.READY


def test_inbox_refuses_exact_retained_evidence_reuse(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    seed_evidence(config, project.name, "TASK-GONE")
    orchestrator = make_inbox_orchestrator(config, db)
    with pytest.raises(TaskSubmissionError) as excinfo:
        orchestrator._task_from_yaml(inbox_payload(project.name, "TASK-GONE"))
    assert excinfo.value.code == "TASK_ID_COLLISION"
    assert db.get_task("TASK-GONE") is None


def test_inbox_refuses_case_variant_of_a_live_task(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    add_task(db, project, task_id="TASK-X", state=TaskState.READY)
    orchestrator = make_inbox_orchestrator(config, db)
    with pytest.raises(TaskSubmissionError) as excinfo:
        orchestrator._task_from_yaml(inbox_payload(project.name, "task-x"))
    assert excinfo.value.code == "TASK_ID_COLLISION"
    assert db.get_task("task-x") is None


def test_inbox_refuses_unsafe_ids(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    orchestrator = make_inbox_orchestrator(config, db)
    for bad_id in ("../evil", "TASK-1.", "CON", "a/b", "a\\b"):
        with pytest.raises(TaskSubmissionError) as excinfo:
            orchestrator._task_from_yaml(inbox_payload(project.name, bad_id))
        assert excinfo.value.code == "INVALID_TASK_REQUEST", bad_id
    assert db.list_tasks() == []


def test_inbox_import_moves_a_refused_submission_to_failed(tmp_path: Path):
    pytest.importorskip("yaml")
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    add_task(db, project, task_id="TASK-X", state=TaskState.READY)
    config.inbox_root.mkdir(parents=True)
    (config.inbox_root / "alias.yaml").write_text(
        f'project: "{project.name}"\ntitle: "Alias"\nobjective: "obj"\nid: "task-x"\n',
        encoding="utf-8",
    )
    orchestrator = make_inbox_orchestrator(config, db)
    assert orchestrator.import_inbox() == 0
    assert (config.inbox_root / "failed" / "alias.yaml").exists()
    error_text = (config.inbox_root / "failed" / "alias.error.txt").read_text(encoding="utf-8")
    assert "collides" in error_text
    assert db.get_task("task-x") is None


# ---------------------------------------------------------------------------------------------
# Retained-evidence scan swap windows
# ---------------------------------------------------------------------------------------------


def _swap_in_decoy(real_dir: Path, tmp_path: Path, decoy_children: list[str]) -> None:
    """Move the real directory aside and put a fresh decoy with the given children in its place."""
    aside = tmp_path / f"aside-{real_dir.name}"
    shutil.move(str(real_dir), str(aside))
    real_dir.mkdir()
    for child in decoy_children:
        (real_dir / child / "attempt-01").mkdir(parents=True)
        (real_dir / child / "attempt-01" / "task-result.json").write_text("{}", encoding="utf-8")


def _swap_on_first_scan(monkeypatch, target: Path, swap) -> None:
    """Perform the swap the moment the scan enumerates the target directory."""
    real_scandir = evidence_module._scandir
    state = {"done": False}

    def wrapper(directory):
        if not state["done"] and os.path.normcase(str(directory)) == os.path.normcase(str(target)):
            state["done"] = True
            swap()
        return real_scandir(directory)

    monkeypatch.setattr(evidence_module, "_scandir", wrapper)


def test_swapped_project_directory_cannot_hide_a_retained_claim(tmp_path: Path, monkeypatch):
    """Zero candidates by swap: an empty decoy must not clear the way for a guarded insertion."""
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    seed_evidence(config, project.name, "TASK-1")
    project_dir = config.evidence_root / project.name
    _swap_on_first_scan(
        monkeypatch, project_dir, lambda: _swap_in_decoy(project_dir, tmp_path, [])
    )
    reuse = Task(
        id="TASK-1",
        project_id=project.id,
        title="Reuse",
        objective="adopt",
        acceptance=[],
        validation_commands=[],
        state=TaskState.READY,
    )
    with pytest.raises(TaskSubmissionError) as excinfo:
        insert_task_guarded(db, config, project, reuse)
    assert excinfo.value.code == "FILESYSTEM_ERROR"
    assert db.get_task("TASK-1") is None


def test_swapped_project_directory_cannot_create_a_false_match(tmp_path: Path, monkeypatch):
    """One candidate by swap: a decoy holding the task id must not resolve as retained evidence."""
    config = make_config(tmp_path)
    make_db(config)
    project_dir = config.evidence_root / "demo"
    (project_dir / "OTHER-TASK" / "attempt-01").mkdir(parents=True)
    _swap_on_first_scan(
        monkeypatch, project_dir, lambda: _swap_in_decoy(project_dir, tmp_path, ["TASK-9"])
    )
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, "TASK-9", schema_version=None)
    assert excinfo.value.code == "FILESYSTEM_ERROR"


def test_swapped_project_directory_cannot_hide_ambiguity(tmp_path: Path, monkeypatch):
    """Multiple candidates: emptying one candidate project by swap must not yield a unique match."""
    config = make_config(tmp_path)
    make_db(config)
    for name in ("alpha", "beta"):
        (config.evidence_root / name / "TASK-9" / "attempt-01").mkdir(parents=True)
    with pytest.raises(EvidenceError) as control:
        evidence_list(config, "TASK-9", schema_version=None)
    assert control.value.code == "EVIDENCE_AMBIGUOUS"
    beta = config.evidence_root / "beta"
    _swap_on_first_scan(monkeypatch, beta, lambda: _swap_in_decoy(beta, tmp_path, []))
    with pytest.raises(EvidenceError) as excinfo:
        evidence_list(config, "TASK-9", schema_version=None)
    assert excinfo.value.code == "FILESYSTEM_ERROR"


def test_alias_named_non_directory_fails_the_claim_closed(tmp_path: Path):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    trap = config.evidence_root / project.name / "TASK-1"
    trap.parent.mkdir(parents=True)
    trap.write_text("not a directory", encoding="utf-8")
    with pytest.raises(EvidenceError) as excinfo:
        retained_evidence_claim(config, project.name, "TASK-1")
    assert excinfo.value.code == "FILESYSTEM_ERROR"


# ---------------------------------------------------------------------------------------------
# Project cleanup of registered worktrees
# ---------------------------------------------------------------------------------------------


def _leftover_registered_checkout(config: ForgeConfig, repo: Path, project: Project) -> Path:
    leftover = config.workspace_root / project.name / "leftover"
    leftover.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-b", "leftover-branch", str(leftover))
    return leftover


def test_project_delete_clears_a_leftover_registered_checkout(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    leftover = _leftover_registered_checkout(config, repo, project)
    assert cmd_project_delete(config, project_args(project.name)) == 0
    payload = response(capsys)
    assert payload["data"]["workspace_removed"] is True
    assert not (config.workspace_root / project.name).exists()
    normalized = os.path.normcase(os.path.normpath(str(leftover)))
    assert normalized not in registered_worktrees(repo)


def test_project_delete_locked_leftover_checkout_fails_closed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    leftover = _leftover_registered_checkout(config, repo, project)
    git(repo, "worktree", "lock", str(leftover))
    assert cmd_project_delete(config, project_args(project.name)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert payload["error"]["details"]["deleted"] is True
    # The locked checkout and its registration honestly survive, intact.
    assert (leftover / "base.txt").exists()
    normalized = os.path.normcase(os.path.normpath(str(leftover)))
    assert normalized in registered_worktrees(repo)
    assert db.get_project(project.name) is None


def test_project_delete_prunes_a_stale_registration(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    leftover = _leftover_registered_checkout(config, repo, project)
    shutil.rmtree(leftover)
    assert cmd_project_delete(config, project_args(project.name)) == 0
    payload = response(capsys)
    assert payload["data"]["workspace_removed"] is True
    assert not (config.workspace_root / project.name).exists()
    normalized = os.path.normcase(os.path.normpath(str(leftover)))
    assert normalized not in registered_worktrees(repo)


def test_project_delete_with_unverifiable_repository_fails_closed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    stray = config.workspace_root / project.name / "stray"
    stray.mkdir(parents=True)
    assert cmd_project_delete(config, project_args(project.name)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert stray.exists()
    assert db.get_project(project.name) is None


# ---------------------------------------------------------------------------------------------
# Missing repository handling
# ---------------------------------------------------------------------------------------------


def test_missing_repository_fails_checkout_removal_closed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert checkout.exists()
    assert db.get_task(task.id) is None


def test_remove_worktree_missing_repository_semantics(tmp_path: Path):
    containment = tmp_path / "worktrees" / "demo"
    checkout = containment / "task-1"
    checkout.mkdir(parents=True)
    with pytest.raises(GitError):
        remove_worktree(tmp_path / "norepo", checkout, containment, [])
    assert checkout.exists()
    assert remove_worktree(tmp_path / "norepo", containment / "gone", containment, []) is False


# ---------------------------------------------------------------------------------------------
# Coherent evidence show metadata
# ---------------------------------------------------------------------------------------------


def test_show_metadata_describes_the_opened_file(tmp_path: Path, monkeypatch):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    task = add_task(db, project, state=TaskState.FAILED)
    seed_evidence(config, project.name, task.id, content='{"status": "completed", "summary": "old"}')
    listing = evidence_list(config, task.id, schema_version=None)
    artifact_id = listing["artifacts"][0]["artifact_id"]
    rewritten = b'{"status": "completed", "summary": "rewritten with a longer body"}'
    stamp = 1_700_000_000
    real_resolve = evidence_module._resolve_served

    def resolve_then_rewrite(selected, verified):
        resolved = real_resolve(selected, verified)
        # Rewrite in place: same file identity, new size, content, and mtime — exactly the
        # window between discovery and the bounded read.
        with open(resolved, "r+b") as handle:
            handle.seek(0)
            handle.write(rewritten)
            handle.truncate()
        os.utime(resolved, (stamp, stamp))
        return resolved

    monkeypatch.setattr(evidence_module, "_resolve_served", resolve_then_rewrite)
    payload = evidence_show(config, task.id, artifact_id, schema_version=None)
    entry = payload["artifact"]
    assert entry["original_size_bytes"] == len(rewritten)
    assert entry["sha256"] == hashlib.sha256(rewritten).hexdigest()
    assert entry["modified_at"] == datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
    assert entry["content_available"] is True
    assert payload["content"]["sha256"] == entry["sha256"]
    assert payload["content"]["original_size_bytes"] == len(rewritten)


# ---------------------------------------------------------------------------------------------
# Safe cleanup details for legacy task ids
# ---------------------------------------------------------------------------------------------


def test_unsafe_legacy_task_id_is_omitted_from_active_details(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    add_task(db, project, task_id="..\\evil", state=TaskState.IMPLEMENTING)
    assert cmd_task_delete(config, task_args("..\\evil")) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "TASK_ACTIVE"
    assert "task_id" not in payload["error"]["details"]
    assert payload["error"]["details"]["state"] == "IMPLEMENTING"
    assert "..\\evil" in captured.err


def test_unsafe_requested_task_id_is_omitted_from_not_found_details(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    make_db(config)
    assert cmd_task_delete(config, task_args("../nope")) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "TASK_NOT_FOUND"
    assert "task_id" not in payload["error"]["details"]
    assert "../nope" in captured.err


def test_unsafe_legacy_task_id_is_omitted_from_cleanup_details(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, tmp_path / "repo")
    checkout = config.workspace_root / project.name / "legacy"
    checkout.mkdir(parents=True)
    task = add_task(
        db, project, task_id="legacy/evil", state=TaskState.FAILED, worktree_path=checkout
    )
    assert cmd_task_delete(config, task_args(task.id)) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert "task_id" not in payload["error"]["details"]
    assert payload["error"]["details"]["deleted"] is True
    assert "legacy/evil" in captured.err
    assert checkout.exists()


# ---------------------------------------------------------------------------------------------
# Workspace validation, foreign ownership, and missing-workspace reconciliation
# ---------------------------------------------------------------------------------------------


def _foreign_repository(tmp_path: Path) -> Path:
    root = tmp_path / "foreign"
    root.mkdir()
    return initialize_repository(root)


def test_symlinked_workspace_never_touches_its_target(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, initialize_repository(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep\n", encoding="utf-8")
    config.workspace_root.mkdir(parents=True)
    workspace = config.workspace_root / project.name
    try:
        workspace.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted in this environment")
    assert cmd_project_delete(config, project_args(project.name)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert (outside / "precious.txt").exists()
    # The link itself also survives; nothing was mutated through it.
    assert os.path.lexists(workspace)
    assert db.get_project(project.name) is None


def test_foreign_registered_checkout_is_never_deleted_by_task_cleanup(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    foreign_repo = _foreign_repository(tmp_path)
    add_project(db, foreign_repo, name="foreign")
    project = add_project(db, initialize_repository(tmp_path))
    checkout = config.workspace_root / project.name / "task-1"
    checkout.parent.mkdir(parents=True)
    git(foreign_repo, "worktree", "add", "-b", "foreign-branch", str(checkout))
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert (checkout / "base.txt").exists()
    normalized = os.path.normcase(os.path.normpath(str(checkout)))
    assert normalized in registered_worktrees(foreign_repo)
    assert db.get_task(task.id) is None


def test_foreign_locked_checkout_blocks_project_cleanup(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    foreign_repo = _foreign_repository(tmp_path)
    add_project(db, foreign_repo, name="foreign")
    project = add_project(db, initialize_repository(tmp_path))
    intruder = config.workspace_root / project.name / "intruder"
    intruder.parent.mkdir(parents=True)
    git(foreign_repo, "worktree", "add", "-b", "intruder-branch", str(intruder))
    git(foreign_repo, "worktree", "lock", str(intruder))
    assert cmd_project_delete(config, project_args(project.name)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert payload["error"]["details"]["deleted"] is True
    assert (intruder / "base.txt").exists()
    normalized = os.path.normcase(os.path.normpath(str(intruder)))
    assert normalized in registered_worktrees(foreign_repo)
    assert db.get_project(project.name) is None


def test_missing_workspace_stale_registration_is_pruned(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    leftover = config.workspace_root / project.name / "leftover"
    leftover.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-b", "leftover-branch", str(leftover))
    shutil.rmtree(config.workspace_root / project.name)
    normalized = os.path.normcase(os.path.normpath(str(leftover)))
    assert normalized in registered_worktrees(repo)
    assert cmd_project_delete(config, project_args(project.name)) == 0
    payload = response(capsys)
    assert payload["data"]["deleted"] is True
    assert payload["data"]["workspace_removed"] is False
    assert normalized not in registered_worktrees(repo)


def test_missing_workspace_locked_stale_registration_fails_closed(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    repo = initialize_repository(tmp_path)
    project = add_project(db, repo)
    leftover = config.workspace_root / project.name / "leftover"
    leftover.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-b", "leftover-branch", str(leftover))
    git(repo, "worktree", "lock", str(leftover))
    shutil.rmtree(config.workspace_root / project.name)
    assert cmd_project_delete(config, project_args(project.name)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PROJECT_CLEANUP_FAILED"
    assert payload["error"]["details"]["deleted"] is True
    normalized = os.path.normcase(os.path.normpath(str(leftover)))
    assert normalized in registered_worktrees(repo)
    assert db.get_project(project.name) is None


def test_unavailable_repository_blocks_ownership_inventory(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, initialize_repository(tmp_path))
    add_project(db, tmp_path / "gone-repo", name="broken")
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)
    assert cmd_task_delete(config, task_args(task.id)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WORKTREE_CLEANUP_FAILED"
    assert checkout.exists()
    assert db.get_task(task.id) is None


def test_ownership_inventory_ignores_non_repository_directories(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    project = add_project(db, initialize_repository(tmp_path))
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    add_project(db, plain, name="plain")
    checkout = config.workspace_root / project.name / "task-1"
    checkout.mkdir(parents=True)
    task = add_task(db, project, state=TaskState.FAILED, worktree_path=checkout)
    assert cmd_task_delete(config, task_args(task.id)) == 0
    payload = response(capsys)
    assert payload["data"]["worktree_removed"] is True
    assert not checkout.exists()


# ---------------------------------------------------------------------------------------------
# Unsafe and aliased legacy project registrations refuse submission
# ---------------------------------------------------------------------------------------------


def test_unsafe_legacy_project_rejects_all_submission_paths(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    db.add_project(Project(id="project-evil", name="..", repo_path=tmp_path / "repo"))
    orchestrator = make_inbox_orchestrator(config, db)
    with pytest.raises(TaskSubmissionError) as excinfo:
        orchestrator._task_from_yaml(inbox_payload("..", "TASK-1"))
    assert excinfo.value.code == "PROJECT_UNSAFE"
    args = submit_args("..", "TASK-1")
    args.json = False
    args.dry_run = False
    assert cmd_task_add(config, args) == 2
    assert "PROJECT_UNSAFE" in capsys.readouterr().err
    rc = cmd_task_submit(config, submit_args("..", "TASK-1"))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"]["code"] == "PROJECT_UNSAFE"
    # Details identify the project by backend-minted id, never by the path-shaped name.
    assert payload["error"]["details"]["project_id"] == "project-evil"
    assert db.list_tasks() == []


def test_case_aliasing_legacy_projects_reject_all_submission_paths(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    add_project(db, tmp_path / "repo", name="demo")
    db.add_project(Project(id="project-legacy", name="Demo.", repo_path=tmp_path / "repo-legacy"))
    # Retained evidence in the shared physical namespace must stay un-adoptable through demo.
    seed_evidence(config, "demo", "TASK-1")
    orchestrator = make_inbox_orchestrator(config, db)
    with pytest.raises(TaskSubmissionError) as excinfo:
        orchestrator._task_from_yaml(inbox_payload("demo", "TASK-2"))
    assert excinfo.value.code == "PROJECT_UNSAFE"
    args = submit_args("demo", "TASK-2")
    args.json = False
    args.dry_run = False
    assert cmd_task_add(config, args) == 2
    assert "PROJECT_UNSAFE" in capsys.readouterr().err
    rc = cmd_task_submit(config, submit_args("demo", "TASK-2"))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"]["code"] == "PROJECT_UNSAFE"
    assert payload["error"]["details"]["collides_with_project_id"] == "project-legacy"
    assert db.list_tasks() == []


def test_project_unsafe_also_refuses_dry_run_previews(tmp_path: Path, capsys):
    config = make_config(tmp_path)
    db = make_db(config)
    db.add_project(Project(id="project-evil", name="..", repo_path=tmp_path / "repo"))
    args = submit_args("..", "TASK-1")
    args.dry_run = True
    rc = cmd_task_submit(config, args)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"]["code"] == "PROJECT_UNSAFE"
    assert db.list_tasks() == []
