"""Exact admission-base provenance: validation, immutability, worktree law and evidence.

`admission_base_commit` is the immutable Git commit a task's execution lineage was admitted
from. It exists because a controller may need to admit a task from a commit that already contains
a predecessor merged locally and not yet pushed. Forge's whole responsibility here is to use
exactly the commit it was given: it validates the commit, persists it once, branches every attempt
and retry from it, measures terminal evidence against it, and refuses rather than substituting the
project's default branch when it cannot.

A task admitted without one keeps Forge's existing base selection unchanged, which is what makes
this slice additive.

These tests drive the real CLI, the real database and real Git repositories. None of them makes a
model request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

from codex_forge.cli import main
from codex_forge.config import ForgeConfig, load_config
from codex_forge.control_plane import _safe_task_error
from codex_forge.db import ForgeDB
from codex_forge.evidence import evidence_list, evidence_show
from codex_forge.gitops import (
    BaseCommitError,
    BaseCommitUnavailable,
    CandidateStateUnavailable,
    WorktreeUnavailable,
    checkout_is_owned_by,
    current_commit,
    prepare_worktree,
    resolve_admission_base_commit,
)
from codex_forge.models import CodexRunResult, Project, Task, TaskState, ValidationResult
from codex_forge.orchestrator import OUTCOME_COMPLETED, Orchestrator
from codex_forge.prompts import implementation_prompt, review_prompt
from codex_forge.runner import SandboxResolution
from codex_forge.task_submission import TaskSubmissionError, build_task_request, submit_task


# ------------------------------------------------------------------------------- Git fixtures


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout


def _identity(repo: Path) -> None:
    git(repo, "config", "user.name", "Forge Admission Base Test")
    git(repo, "config", "user.email", "forge-admission@example.invalid")


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", f"add {name}")
    return git(repo, "rev-parse", "HEAD").strip()


def repository(path: Path) -> tuple[Path, str]:
    """A real repository whose only commit is the one this returns."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    _identity(path)
    return path, _commit(path, "base.txt", "base\n")


def repository_with_moved_default_branch(root: Path) -> tuple[Path, str, str, str]:
    """A repository where three different commits could plausibly be chosen as a base.

    `origin/main` is one commit, the local default branch has advanced past the admission base to
    another, and the admission base sits between them. Only an implementation that uses the exact
    persisted commit can pass: every other candidate here is reachable through the base selection
    Forge would otherwise perform.
    """
    upstream, remote_head = repository(root / "upstream")
    repo = root / "repo"
    subprocess.run(
        ["git", "clone", str(upstream), str(repo)], capture_output=True, text=True, check=True
    )
    _identity(repo)
    base = _commit(repo, "admitted.txt", "admitted\n")
    advanced = _commit(repo, "later.txt", "later\n")
    assert len({remote_head, base, advanced}) == 3
    assert git(repo, "rev-parse", "origin/main").strip() == remote_head
    return repo, remote_head, base, advanced


# --------------------------------------------------------------------------- Forge fixtures


def configured(tmp_path: Path) -> ForgeConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        f"""
[paths]
workspace_root = '{(tmp_path / "worktrees").as_posix()}'
data_root = '{(tmp_path / "data").as_posix()}'
repository_root = '{(tmp_path / "repositories").as_posix()}'
inbox_root = '{(tmp_path / "inbox").as_posix()}'
database_path = '{(tmp_path / "data" / "forge.db").as_posix()}'
evidence_root = '{(tmp_path / "evidence").as_posix()}'
knowledge_root = '{(tmp_path / "knowledge").as_posix()}'

[codex]
approval_policy = 'never'
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_path)


def run(config: ForgeConfig, *arguments: str) -> int:
    return main(["--config", str(config.config_path), *arguments])


def registered(tmp_path: Path, capsys) -> tuple[ForgeConfig, ForgeDB, Path, str]:
    config = configured(tmp_path)
    repo, base = repository(tmp_path / "repo")
    assert run(config, "project", "add", "alpha", "--repo", str(repo)) == 0
    capsys.readouterr()
    return config, ForgeDB(config.database_path), repo, base


def submit(
    config: ForgeConfig,
    capsys,
    *extra: str,
    title: str = "Exact base",
    objective: str = "Admit from an exact commit.",
    schema_version: str = "1.3",
) -> tuple[int, dict]:
    exit_code = run(
        config,
        "task",
        "add",
        "--project",
        "alpha",
        "--title",
        title,
        "--objective",
        objective,
        "--schema-version",
        schema_version,
        "--json",
        *extra,
    )
    captured = capsys.readouterr()
    return exit_code, json.loads(captured.out)


def only_task(db: ForgeDB) -> Task:
    tasks = db.list_tasks()
    assert len(tasks) == 1
    return tasks[0]


def task_for(project: Project, base: str | None, task_id: str = "SC-ADMISSION-1") -> Task:
    return Task(
        id=task_id,
        project_id=project.id,
        title="Admitted task",
        objective="Run from an exact commit.",
        acceptance=[],
        validation_commands=[],
        state=TaskState.IMPLEMENTING,
        attempts=1,
        admission_base_commit=base,
    )


def project_for(repo: Path, name: str = "admission") -> Project:
    return Project(
        id=f"project-{name}",
        name=name,
        repo_path=repo,
        default_branch="main",
        validation_commands=["controlled-validation"],
        auto_fetch=False,
        auto_commit=True,
        auto_push=False,
    )


# =============================================================== A. explicit task admission


def test_an_exact_commit_is_accepted_and_persisted_as_the_task_base(tmp_path: Path, capsys):
    config, db, _repo, base = registered(tmp_path, capsys)

    exit_code, payload = submit(config, capsys, "--base-commit", base)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["data"]["created"] is True
    assert payload["data"]["task"]["admission_base_commit"] == base
    assert payload["data"]["normalized_request"]["admission_base_commit"] == base
    assert only_task(db).admission_base_commit == base


def test_the_persisted_base_is_gits_canonical_object_id_not_the_caller_spelling(
    tmp_path: Path, capsys
):
    """An uppercase object id names the same commit, so it is accepted and stored canonically."""
    config, db, _repo, base = registered(tmp_path, capsys)

    exit_code, payload = submit(config, capsys, "--base-commit", base.upper())

    assert exit_code == 0
    assert payload["data"]["task"]["admission_base_commit"] == base
    assert only_task(db).admission_base_commit == base


def test_a_commit_the_repository_does_not_have_is_refused(tmp_path: Path, capsys):
    config, db, _repo, base = registered(tmp_path, capsys)
    absent = "0" * len(base)

    exit_code, payload = submit(config, capsys, "--base-commit", absent)

    assert exit_code == 2
    assert payload["error"]["code"] == "INVALID_BASE_COMMIT"
    assert db.list_tasks() == []


@pytest.mark.parametrize("form", ["branch", "tag", "expression", "abbreviation"])
def test_only_a_canonical_commit_id_is_durable_identity(tmp_path: Path, capsys, form: str):
    """Every value that resolves to something other than itself is refused.

    A branch or tag can be repointed, a revision expression is evaluated against whatever the
    repository holds at the time, and an abbreviation can become ambiguous as objects are added.
    None of them is durable identity, so none of them may be persisted as admission provenance.
    """
    config, db, repo, base = registered(tmp_path, capsys)
    git(repo, "tag", "admitted", base)
    requested = {
        "branch": "main",
        "tag": "admitted",
        "expression": "HEAD",
        "abbreviation": base[:8],
    }[form]

    exit_code, payload = submit(config, capsys, "--base-commit", requested)

    assert exit_code == 2
    assert payload["error"]["code"] == "INVALID_BASE_COMMIT"
    assert db.list_tasks() == []


def test_an_object_that_is_not_a_commit_is_refused(tmp_path: Path, capsys):
    config, db, repo, _base = registered(tmp_path, capsys)
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()

    exit_code, payload = submit(config, capsys, "--base-commit", tree)

    assert exit_code == 2
    assert payload["error"]["code"] == "INVALID_BASE_COMMIT"
    assert db.list_tasks() == []


def test_an_unreadable_repository_refuses_rather_than_accepting_an_unproven_base(
    tmp_path: Path, capsys
):
    """A repository fault is reported as one, never as a statement about the requested commit."""
    config, db, repo, base = registered(tmp_path, capsys)
    repo.rename(tmp_path / "moved-away")

    exit_code, payload = submit(config, capsys, "--base-commit", base)

    assert exit_code == 1
    assert payload["error"]["code"] == "REPOSITORY_UNAVAILABLE"
    assert db.list_tasks() == []


def test_a_dry_run_validates_the_base_and_writes_nothing(tmp_path: Path, capsys):
    config, db, _repo, base = registered(tmp_path, capsys)

    exit_code, payload = submit(config, capsys, "--base-commit", base, "--dry-run")

    assert exit_code == 0
    assert payload["data"]["would_create"] is True
    assert payload["data"]["task"]["admission_base_commit"] == base
    assert db.list_tasks() == []


def test_the_resolver_refuses_before_it_asks_git_about_a_non_object_shape(tmp_path: Path):
    repo, base = repository(tmp_path / "repo")

    assert resolve_admission_base_commit(repo, base) == base
    with pytest.raises(BaseCommitError):
        resolve_admission_base_commit(repo, "refs/heads/main")
    with pytest.raises(BaseCommitError):
        resolve_admission_base_commit(repo, "  ")


# ================================================================ B. task-add idempotency


def test_resubmitting_the_same_task_and_base_is_idempotent(tmp_path: Path, capsys):
    config, db, _repo, base = registered(tmp_path, capsys)
    assert submit(config, capsys, "--base-commit", base)[0] == 0

    exit_code, payload = submit(config, capsys, "--base-commit", base)

    assert exit_code == 0
    assert payload["data"]["created"] is False
    assert payload["data"]["task"]["admission_base_commit"] == base
    assert only_task(db).admission_base_commit == base


def test_the_same_task_with_a_different_base_is_refused_and_changes_nothing(
    tmp_path: Path, capsys
):
    config, db, repo, base = registered(tmp_path, capsys)
    assert submit(config, capsys, "--base-commit", base)[0] == 0
    advanced = _commit(repo, "later.txt", "later\n")

    exit_code, payload = submit(config, capsys, "--base-commit", advanced)

    assert exit_code == 2
    assert payload["error"]["code"] == "TASK_BASE_MISMATCH"
    assert payload["error"]["details"]["admission_base_commit"] == base
    assert payload["error"]["details"]["requested_admission_base_commit"] == advanced
    assert only_task(db).admission_base_commit == base


def test_a_task_admitted_without_a_base_cannot_silently_acquire_one(tmp_path: Path, capsys):
    """A task with no exact provenance never gains it: that would fabricate history."""
    config, db, _repo, base = registered(tmp_path, capsys)
    assert submit(config, capsys)[0] == 0
    assert only_task(db).admission_base_commit is None

    exit_code, payload = submit(config, capsys, "--base-commit", base)

    assert exit_code == 2
    assert payload["error"]["code"] == "TASK_BASE_MISMATCH"
    assert only_task(db).admission_base_commit is None


def test_an_admitted_task_resubmitted_without_a_base_is_refused(tmp_path: Path, capsys):
    config, db, _repo, base = registered(tmp_path, capsys)
    assert submit(config, capsys, "--base-commit", base)[0] == 0

    exit_code, payload = submit(config, capsys)

    assert exit_code == 2
    assert payload["error"]["code"] == "TASK_BASE_MISMATCH"
    assert only_task(db).admission_base_commit == base


def test_a_different_base_is_reported_as_a_base_mismatch_not_a_generic_conflict(
    tmp_path: Path, capsys
):
    """The refusal names the actual disagreement, so a caller can act on it."""
    config, _db, repo, base = registered(tmp_path, capsys)
    assert submit(config, capsys, "--base-commit", base)[0] == 0
    advanced = _commit(repo, "later.txt", "later\n")

    _exit_code, payload = submit(config, capsys, "--base-commit", advanced)

    assert payload["error"]["code"] not in {"TASK_ID_CONFLICT", "DUPLICATE_TASK_ID"}
    assert payload["error"]["code"] == "TASK_BASE_MISMATCH"


# ============================================================================ C. worktree


def test_the_worktree_is_created_from_the_admission_base_and_not_from_any_other_candidate(
    tmp_path: Path,
):
    """The key regression: the base wins over both the local and the remote default branch."""
    repo, remote_head, base, advanced = repository_with_moved_default_branch(tmp_path)
    project = project_for(repo)

    _branch, worktree = prepare_worktree(project, task_for(project, base), tmp_path / "worktrees")

    assert current_commit(worktree) == base
    assert current_commit(worktree) != advanced
    assert current_commit(worktree) != remote_head
    assert not (worktree / "later.txt").exists()


def test_origin_default_branch_does_not_win_over_the_admission_base(tmp_path: Path):
    repo, remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    assert git(repo, "rev-parse", "origin/main").strip() == remote_head
    project = project_for(repo)

    _branch, worktree = prepare_worktree(project, task_for(project, base), tmp_path / "worktrees")

    assert current_commit(worktree) == base


def test_a_task_without_an_admission_base_keeps_the_existing_base_selection(tmp_path: Path):
    """The legacy path is unchanged: `origin/<default_branch>` still wins when it exists."""
    repo, remote_head, _base, _advanced = repository_with_moved_default_branch(tmp_path)
    project = project_for(repo)

    _branch, worktree = prepare_worktree(project, task_for(project, None), tmp_path / "worktrees")

    assert current_commit(worktree) == remote_head


# ==================================================================== D. restart and retry


def test_the_admission_base_survives_a_database_reopen(tmp_path: Path, capsys):
    config, _db, _repo, base = registered(tmp_path, capsys)
    assert submit(config, capsys, "--base-commit", base)[0] == 0

    reopened = ForgeDB(config.database_path)

    assert only_task(reopened).admission_base_commit == base


def test_a_retry_still_starts_from_the_admission_base_after_the_default_branch_moves(
    tmp_path: Path,
):
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    project = project_for(repo)
    task = task_for(project, base)
    branch, worktree = prepare_worktree(project, task, tmp_path / "worktrees")
    task.branch, task.worktree_path = branch, worktree
    moved = _commit(repo, "moved.txt", "moved\n")
    assert git(repo, "rev-parse", "main").strip() == moved

    _retry_branch, retry_worktree = prepare_worktree(project, task, tmp_path / "worktrees")

    assert retry_worktree == worktree
    assert current_commit(retry_worktree) == base


def test_a_recreated_worktree_is_rebuilt_from_the_admission_base_not_the_branch_tip_of_the_day(
    tmp_path: Path,
):
    """Cleanup and recreation is a retry too: the branch was cut from the base and still is."""
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    project = project_for(repo)
    task = task_for(project, base)
    branch, worktree = prepare_worktree(project, task, tmp_path / "worktrees")
    git(repo, "worktree", "remove", "--force", str(worktree))
    assert not worktree.exists()
    task.branch = branch

    _recreated_branch, recreated = prepare_worktree(project, task, tmp_path / "worktrees")

    assert current_commit(recreated) == base


def test_a_missing_admission_base_fails_closed_instead_of_substituting_the_default_branch(
    tmp_path: Path,
):
    repo, _base = repository(tmp_path / "repo")
    project = project_for(repo)
    absent = "0" * 40

    with pytest.raises(BaseCommitUnavailable):
        prepare_worktree(project, task_for(project, absent), tmp_path / "worktrees")

    assert not (tmp_path / "worktrees").exists() or not any(
        (tmp_path / "worktrees").rglob(".git")
    )


def test_a_retained_branch_that_no_longer_descends_from_the_base_fails_closed(tmp_path: Path):
    """A branch rewritten underneath the task is a repository failure, not a new base."""
    repo, remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    project = project_for(repo)
    task = task_for(project, base)
    branch, worktree = prepare_worktree(project, task, tmp_path / "worktrees")
    git(repo, "worktree", "remove", "--force", str(worktree))
    # Repointed at a commit the admission base is not an ancestor of, which is what a rewrite
    # underneath a live task looks like from Forge's side.
    git(repo, "branch", "-f", branch, remote_head)
    task.branch = branch

    with pytest.raises(BaseCommitUnavailable):
        prepare_worktree(project, task, tmp_path / "worktrees")


# ============================================================================ E. relocation


def test_a_relocated_repository_that_contains_the_base_still_executes_from_it(tmp_path: Path):
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    relocated = tmp_path / "relocated"
    subprocess.run(
        ["git", "clone", str(repo), str(relocated)], capture_output=True, text=True, check=True
    )
    _identity(relocated)
    project = project_for(relocated)

    _branch, worktree = prepare_worktree(project, task_for(project, base), tmp_path / "worktrees")

    assert current_commit(worktree) == base


def test_a_relocated_repository_missing_the_base_fails_closed_without_rebasing(tmp_path: Path):
    _repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    unrelated, _unrelated_base = repository(tmp_path / "unrelated")
    project = project_for(unrelated)

    with pytest.raises(BaseCommitUnavailable):
        prepare_worktree(project, task_for(project, base), tmp_path / "worktrees")


def test_relocation_does_not_touch_a_task_admission_base(tmp_path: Path, capsys):
    config, db, repo, base = registered(tmp_path, capsys)
    assert submit(config, capsys, "--base-commit", base)[0] == 0
    relocated = tmp_path / "relocated"
    subprocess.run(
        ["git", "clone", str(repo), str(relocated)], capture_output=True, text=True, check=True
    )

    assert db.relocate_project("alpha", relocated).outcome == "relocated"

    assert only_task(db).admission_base_commit == base


# ===================================================================== F. terminal evidence


class ScriptedRunner:
    """One implementation phase and one passing review, with no model request."""

    def resolve_sandbox(self, requested: str, *, cwd: Path) -> SandboxResolution:
        return SandboxResolution(requested=requested, sandbox="danger-full-access", probe=None)

    # A subclass sets this False to leave the worktree byte-for-byte untouched.
    writes_candidate = True

    def run(self, *, cwd, artifact_dir, phase, **_kwargs) -> CodexRunResult:
        worktree = Path(cwd)
        if phase == "implementation":
            if self.writes_candidate:
                (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            final = {
                "status": "completed",
                "summary": "Implemented.",
                "changed_files": ["candidate.txt"],
                "validation_performed": [],
                "assumptions": [],
                "blocking_reason": None,
                "decisions_needed": [],
                "reusable_patterns": [],
            }
        else:
            final = {
                "verdict": "pass",
                "summary": "Candidate is complete.",
                "acceptance_criteria": [],
                "binding_requirements": [],
                "findings": [],
            }
        artifact = Path(artifact_dir)
        artifact.mkdir(parents=True, exist_ok=True)
        final_path = artifact / f"{phase}.final.json"
        final_path.write_text(json.dumps(final), encoding="utf-8")
        events_path = artifact / f"{phase}.events.jsonl"
        events_path.write_text("", encoding="utf-8")
        stderr_path = artifact / f"{phase}.stderr.log"
        stderr_path.write_text("", encoding="utf-8")
        return CodexRunResult(
            exit_code=0,
            final=final,
            jsonl_path=events_path,
            final_path=final_path,
            stderr_path=stderr_path,
            usage={},
        )


def test_terminal_evidence_binds_to_the_admission_base_and_not_the_moved_default_branch(
    tmp_path: Path, monkeypatch
):
    """Candidate, changed files and patch are all measured from the exact admitted commit."""
    repo, _remote_head, base, advanced = repository_with_moved_default_branch(tmp_path)
    config = load_config(configured(tmp_path / "forge").config_path)
    orchestrator = Orchestrator(config, Path(__file__).parents[1])
    project = project_for(repo, name="terminal")
    task = task_for(project, base, task_id="SC-ADMISSION-TERMINAL")
    orchestrator.db.add_project(project)
    orchestrator.db.add_task(task)
    orchestrator.runner = ScriptedRunner()
    monkeypatch.setattr(
        orchestrator,
        "_validate",
        lambda *_args, **_kwargs: [
            ValidationResult(
                command="controlled-validation",
                exit_code=0,
                passed=True,
                duration_seconds=0.01,
                stdout_path=tmp_path / "validation.stdout.log",
                stderr_path=tmp_path / "validation.stderr.log",
                summary="passed",
            )
        ],
    )

    assert orchestrator._execute_task_body(task) == OUTCOME_COMPLETED

    terminal = json.loads(
        (
            config.evidence_root / "terminal" / task.id / "attempt-01" / "task-result.json"
        ).read_text(encoding="utf-8")
    )
    candidate = terminal["commit"]
    assert terminal["base_commit"] == base
    assert terminal["admission_base_commit"] == base
    assert git(repo, "rev-parse", f"{candidate}^").strip() == base
    assert terminal["changed_files"] == git(
        repo, "diff", "--name-only", f"{base}..{candidate}", "--"
    ).split()
    assert "later.txt" not in terminal["changed_files"]
    assert Path(terminal["patch"]).read_text(encoding="utf-8") == git(
        repo, "diff", "--binary", f"{base}..{candidate}", "--"
    )
    assert git(repo, "rev-parse", "main").strip() == advanced
    assert orchestrator.db.get_task(task.id).result["base_commit"] == base


# ======================================================================== G. schema and JSON


@pytest.mark.parametrize("schema_version", ["1.0", "1.1", "1.2"])
def test_older_contract_versions_do_not_carry_the_new_field(
    tmp_path: Path, capsys, schema_version: str
):
    """A consumer built against an older version validated a task object without this field."""
    config, _db, _repo, base = registered(tmp_path, capsys)

    exit_code, payload = submit(
        config, capsys, "--base-commit", base, schema_version=schema_version
    )

    assert exit_code == 0
    assert payload["schema_version"] == schema_version
    assert "admission_base_commit" not in payload["data"]["task"]
    assert "admission_base_commit" not in payload["data"]["normalized_request"]


def test_schema_1_3_reports_null_for_a_task_admitted_without_a_base(tmp_path: Path, capsys):
    """Honest absence, never a value inferred from the repository's current state."""
    config, _db, _repo, _base = registered(tmp_path, capsys)
    assert submit(config, capsys)[0] == 0

    exit_code, payload = submit(config, capsys, schema_version="1.3")

    assert exit_code == 0
    assert payload["data"]["task"]["admission_base_commit"] is None
    assert payload["data"]["normalized_request"]["admission_base_commit"] is None


def test_task_show_reports_the_admission_base_at_the_declaring_version(tmp_path: Path, capsys):
    config, db, _repo, base = registered(tmp_path, capsys)
    assert submit(config, capsys, "--base-commit", base)[0] == 0
    task_id = only_task(db).id

    assert run(config, "task", "show", task_id, "--schema-version", "1.3", "--json") == 0
    declared = json.loads(capsys.readouterr().out)
    assert run(config, "task", "show", task_id, "--json") == 0
    legacy = json.loads(capsys.readouterr().out)

    assert declared["data"]["task"]["admission_base_commit"] == base
    assert "admission_base_commit" not in legacy["data"]["task"]


# =================================================================== H. migration concurrency


def _drop_admission_base_column(config: ForgeConfig) -> None:
    """Reproduce the exact pre-column shape of the tasks table, keeping every row."""
    connection = sqlite3.connect(config.database_path)
    try:
        connection.execute("ALTER TABLE tasks DROP COLUMN admission_base_commit")
        connection.commit()
    finally:
        connection.close()


def _task_columns(config: ForgeConfig) -> list[str]:
    connection = sqlite3.connect(config.database_path)
    try:
        return [row[1] for row in connection.execute("PRAGMA table_info(tasks)")]
    finally:
        connection.close()


def test_two_concurrent_initializers_establish_the_task_column_exactly_once(
    tmp_path: Path, capsys, monkeypatch
):
    """The unguarded add-column pattern fails here; the write-locked one does not.

    The hook sits exactly where the old defect lived: after an initializer has decided the column
    is missing and before it issues `ALTER TABLE`. Holding the first arrival there until another
    one joins is what turns a possible collision into a certain one. Nothing in production
    synchronizes on it -- while the migration holds the write lock a second initializer cannot
    reach the seam at all, the barrier times out, and the first proceeds, which is the behaviour
    under test.
    """
    config, _db, _repo, _base = registered(tmp_path, capsys)
    _drop_admission_base_column(config)
    assert "admission_base_commit" not in _task_columns(config)

    workers = 2
    meet = threading.Barrier(workers, timeout=0.5)
    real_add_column = ForgeDB._add_column_if_missing

    def hooked(connection, table, column, declaration):
        if (table, column) == ("tasks", "admission_base_commit"):
            try:
                meet.wait()
            except threading.BrokenBarrierError:
                pass
        return real_add_column(connection, table, column, declaration)

    monkeypatch.setattr(ForgeDB, "_add_column_if_missing", staticmethod(hooked))
    lock = threading.Lock()
    failures: list[BaseException] = []

    def initialize() -> None:
        try:
            ForgeDB(config.database_path).initialize()
        except BaseException as exc:  # noqa: BLE001 - reported verbatim to the assertion below
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=initialize, name=f"init-{n}") for n in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)

    assert not any(thread.is_alive() for thread in threads), "an initializer never finished"
    assert failures == [], f"an initializer failed: {failures!r}"
    assert _task_columns(config).count("admission_base_commit") == 1


def test_a_legacy_task_row_without_the_column_stays_readable_and_has_no_base(
    tmp_path: Path, capsys
):
    """Migration is additive: nothing is backfilled and nothing existing changes meaning."""
    config, db, _repo, _base = registered(tmp_path, capsys)
    assert submit(config, capsys)[0] == 0
    task_id = only_task(db).id
    _drop_admission_base_column(config)

    assert ForgeDB(config.database_path).get_task(task_id).admission_base_commit is None

    ForgeDB(config.database_path).initialize()

    assert "admission_base_commit" in _task_columns(config)
    assert db.get_task(task_id).admission_base_commit is None


# ================================================================ legacy path compatibility


def test_the_human_task_add_path_still_works_without_a_base(tmp_path: Path, capsys):
    config, db, _repo, _base = registered(tmp_path, capsys)

    exit_code = run(
        config,
        "task",
        "add",
        "--project",
        "alpha",
        "--title",
        "Legacy add",
        "--objective",
        "No base supplied.",
    )
    capsys.readouterr()

    assert exit_code == 0
    assert only_task(db).admission_base_commit is None


def test_the_human_task_add_path_accepts_and_refuses_a_base_on_the_same_law(
    tmp_path: Path, capsys
):
    config, db, _repo, base = registered(tmp_path, capsys)

    assert (
        run(
            config,
            "task",
            "add",
            "--project",
            "alpha",
            "--title",
            "Human base",
            "--objective",
            "Admit from an exact commit.",
            "--base-commit",
            base,
        )
        == 0
    )
    capsys.readouterr()
    assert only_task(db).admission_base_commit == base

    refused = run(
        config,
        "task",
        "add",
        "--project",
        "alpha",
        "--title",
        "Human branch",
        "--objective",
        "Admit from a branch.",
        "--base-commit",
        "main",
    )
    captured = capsys.readouterr()

    assert refused == 2
    assert "INVALID_BASE_COMMIT" in captured.err
    assert len(db.list_tasks()) == 1


# ================================================ 1. reviewer and implementer base authority


def test_the_review_prompt_names_the_admission_base_after_the_default_branch_moves(
    tmp_path: Path,
):
    """Model-facing prose must name the same base terminal evidence is measured from."""
    repo, _remote_head, base, advanced = repository_with_moved_default_branch(tmp_path)
    project = project_for(repo)
    task = task_for(project, base)
    assert git(repo, "rev-parse", "main").strip() == advanced

    prompt = review_prompt(project, task, tmp_path / "worktrees" / "checkout")

    assert base in prompt
    assert f"Review the complete diff against `{base}`" in prompt
    assert f"against `{project.default_branch}`" not in prompt
    assert "immutable admission base" in prompt


def test_the_implementation_prompt_names_the_admission_base_as_the_comparison_base(
    tmp_path: Path,
):
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    project = project_for(repo)

    prompt = implementation_prompt(project, task_for(project, base), repo, [])

    assert f"- Base commit: {base}" in prompt
    assert f"- Base branch: {project.default_branch}" not in prompt


def test_a_task_without_an_admission_base_keeps_the_default_branch_wording(tmp_path: Path):
    """Legacy prose is unchanged, in both prompts."""
    repo, _base = repository(tmp_path / "repo")
    project = project_for(repo)
    task = task_for(project, None)

    implementation = implementation_prompt(project, task, repo, [])
    review = review_prompt(project, task, repo)

    assert f"- Base branch: {project.default_branch}" in implementation
    assert f"Review the complete diff against `{project.default_branch}`" in review
    assert "immutable admission base" not in review


# ============================================ 2. relocated repository, retained worktree ownership


def test_a_worktree_left_by_the_old_repository_is_not_reused_after_relocation(tmp_path: Path):
    """Ownership is proven from Git metadata; a stored path never vouches for a checkout."""
    old_repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    old_project = project_for(old_repo)
    task = task_for(old_project, base)
    branch, worktree = prepare_worktree(old_project, task, tmp_path / "worktrees")
    task.branch, task.worktree_path = branch, worktree
    assert checkout_is_owned_by(old_repo, worktree)

    new_repo = tmp_path / "relocated"
    subprocess.run(
        ["git", "clone", str(old_repo), str(new_repo)], capture_output=True, text=True, check=True
    )
    _identity(new_repo)
    relocated_project = project_for(new_repo)
    assert git(new_repo, "cat-file", "-t", base).strip() == "commit"

    _branch, reused = prepare_worktree(relocated_project, task, tmp_path / "worktrees")

    assert checkout_is_owned_by(new_repo, reused)
    assert not checkout_is_owned_by(old_repo, reused)
    assert current_commit(reused) == base


def test_a_relocation_to_a_repository_without_the_base_fails_closed_with_a_stale_worktree(
    tmp_path: Path,
):
    """No fallback to the old repository and no fallback to the new default branch."""
    old_repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    old_project = project_for(old_repo)
    task = task_for(old_project, base)
    branch, worktree = prepare_worktree(old_project, task, tmp_path / "worktrees")
    task.branch, task.worktree_path = branch, worktree

    unrelated, _unrelated_base = repository(tmp_path / "unrelated")

    with pytest.raises(BaseCommitUnavailable):
        prepare_worktree(project_for(unrelated), task, tmp_path / "worktrees")


@pytest.mark.parametrize("checkout_directory_present", [True, False])
def test_a_physically_moved_repository_clears_its_stale_registration_and_recreates_the_worktree(
    tmp_path: Path, checkout_directory_present: bool
):
    """The moved repository's Git inventory, not path existence, controls reconstruction."""
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    project = project_for(repo)
    task = task_for(project, base, task_id="SC-ADMISSION-PHYSICAL-MOVE")
    task.state = TaskState.READY
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    db.add_project(project)
    db.add_task(task)
    branch, worktree = prepare_worktree(project, task, tmp_path / "worktrees")
    db.update_task(task.id, branch=branch, worktree_path=worktree)
    task.branch, task.worktree_path = branch, worktree
    assert checkout_is_owned_by(repo, worktree)

    relocated = tmp_path / "repo-b"
    repo.rename(relocated)
    if not checkout_directory_present:
        shutil.rmtree(worktree)
    if checkout_directory_present:
        assert checkout_is_owned_by(relocated, worktree) is False
    else:
        assert not worktree.exists()
    assert db.relocate_project(project.id, relocated).outcome == "relocated"

    current_project = db.get_project(project.id)
    current_task = db.get_task(task.id)
    recreated_branch, recreated = prepare_worktree(
        current_project, current_task, tmp_path / "worktrees"
    )

    assert recreated_branch == branch
    assert recreated == worktree
    assert checkout_is_owned_by(relocated, recreated)
    assert current_commit(recreated) == base
    assert not subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, "HEAD"], cwd=recreated
    ).returncode
    registrations = git(relocated, "worktree", "list", "--porcelain")
    assert registrations.count(f"worktree {worktree.as_posix()}") == 1


def test_a_physical_relocation_to_history_without_b_never_reuses_the_old_checkout(
    tmp_path: Path,
):
    old_repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    project = project_for(old_repo)
    task = task_for(project, base, task_id="SC-ADMISSION-PHYSICAL-MISSING")
    task.state = TaskState.READY
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    db.add_project(project)
    db.add_task(task)
    branch, worktree = prepare_worktree(project, task, tmp_path / "worktrees")
    db.update_task(task.id, branch=branch, worktree_path=worktree)
    task.branch, task.worktree_path = branch, worktree

    archived = tmp_path / "repo-a-archive"
    old_repo.rename(archived)
    repo_b, repo_b_head = repository(tmp_path / "repo-b")
    assert db.relocate_project(project.id, repo_b).outcome == "relocated"

    with pytest.raises(BaseCommitUnavailable):
        prepare_worktree(db.get_project(project.id), db.get_task(task.id), tmp_path / "worktrees")

    assert checkout_is_owned_by(repo_b, worktree) is False
    assert repo_b_head == current_commit(repo_b)
    assert f"worktree {worktree.as_posix()}" not in git(
        repo_b, "worktree", "list", "--porcelain"
    )


def test_targeted_stale_registration_cleanup_refuses_an_unrelated_branch(tmp_path: Path):
    repo, base = repository(tmp_path / "repo")
    project = project_for(repo)
    task = task_for(project, base, task_id="SC-ADMISSION-REGISTRATION-SAFETY")
    task.branch = "forge/expected-task-branch"
    task.worktree_path = tmp_path / "worktrees" / project.name / "target"
    task.worktree_path.parent.mkdir(parents=True)
    git(
        repo,
        "worktree",
        "add",
        "-b",
        "unrelated-checkout",
        str(task.worktree_path),
        base,
    )
    shutil.rmtree(task.worktree_path)

    with pytest.raises(WorktreeUnavailable):
        prepare_worktree(project, task, tmp_path / "worktrees")

    registrations = git(repo, "worktree", "list", "--porcelain")
    assert f"worktree {task.worktree_path.as_posix()}" in registrations
    assert "branch refs/heads/unrelated-checkout" in registrations


# ================================================= 3. reused clean branch already ahead of the base


def test_a_clean_reused_branch_already_ahead_of_the_base_keeps_its_candidate(
    tmp_path: Path, monkeypatch
):
    """`commit_task` creating nothing is not the same fact as there being no candidate."""
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    orchestrator, project, task, config = _terminal_orchestrator(
        tmp_path, repo, monkeypatch, task_id="SC-ADMISSION-REUSED", base=base
    )
    branch, worktree = prepare_worktree(project, task, config.workspace_root)
    (worktree / "candidate.txt").write_text("already committed\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "earlier attempt candidate")
    candidate = current_commit(worktree)
    assert candidate != base
    assert not git(worktree, "status", "--porcelain").strip()
    orchestrator.db.update_task(task.id, branch=branch, worktree_path=worktree)
    task.branch, task.worktree_path = branch, worktree
    orchestrator.runner = _NoOpRunner()

    assert orchestrator._execute_task_body(task) == OUTCOME_COMPLETED

    terminal = _terminal_document(config, project, task)
    assert terminal["commit"] == candidate
    assert terminal["base_commit"] == base
    assert terminal["changed_files"] == git(
        repo, "diff", "--name-only", f"{base}..{candidate}", "--"
    ).split()
    assert terminal["changed_files"] == ["candidate.txt"]


def test_a_clean_reused_candidate_is_published_and_confirmed_when_auto_push_is_enabled(
    tmp_path: Path, monkeypatch
):
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    orchestrator, project, task, config = _terminal_orchestrator(
        tmp_path,
        repo,
        monkeypatch,
        task_id="SC-ADMISSION-REUSED-PUSH",
        base=base,
        auto_push=True,
    )
    branch, worktree = prepare_worktree(project, task, config.workspace_root)
    (worktree / "candidate.txt").write_text("already committed\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "earlier attempt candidate")
    candidate = current_commit(worktree)
    orchestrator.db.update_task(task.id, branch=branch, worktree_path=worktree)
    task.branch, task.worktree_path = branch, worktree
    orchestrator.runner = _NoOpRunner()

    from codex_forge import gitops

    real_git = gitops._git
    pushes: list[tuple[str, ...]] = []

    def push_tripwire(where: Path, *arguments: str, **kwargs):
        if arguments and arguments[0] == "push":
            pushes.append(arguments)
        return real_git(where, *arguments, **kwargs)

    monkeypatch.setattr(gitops, "_git", push_tripwire)

    assert orchestrator._execute_task_body(task) == OUTCOME_COMPLETED
    assert _terminal_document(config, project, task)["commit"] == candidate
    assert pushes == [("push", "origin", f"{candidate}:refs/heads/{branch}")]
    assert git(worktree, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == candidate


def test_a_dirty_reused_candidate_cannot_hide_uncommitted_repairs_from_terminal_evidence(
    tmp_path: Path, monkeypatch
):
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    orchestrator, project, task, config = _terminal_orchestrator(
        tmp_path,
        repo,
        monkeypatch,
        task_id="SC-ADMISSION-DIRTY",
        base=base,
        auto_commit=False,
    )
    branch, worktree = prepare_worktree(project, task, config.workspace_root)
    (worktree / "candidate.txt").write_text("committed candidate\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "earlier attempt candidate")
    candidate = current_commit(worktree)
    orchestrator.db.update_task(task.id, branch=branch, worktree_path=worktree)
    task.branch, task.worktree_path = branch, worktree
    orchestrator.runner = ScriptedRunner()

    with pytest.raises(CandidateStateUnavailable) as raised:
        orchestrator._execute_task_body(task)
    orchestrator._handle_unexpected_failure(task, raised.value)

    stored = orchestrator.db.get_task(task.id)
    assert stored.state is TaskState.FAILED
    assert stored.last_error.startswith("CANDIDATE_STATE_UNAVAILABLE: ")
    assert stored.failure_fingerprint is None
    assert stored.identical_failures == 0
    assert stored.result == {}
    assert _safe_task_error(stored.last_error)[0] == "CANDIDATE_STATE_UNAVAILABLE"
    assert current_commit(worktree) == candidate
    assert git(worktree, "status", "--porcelain").strip()
    assert not (
        config.evidence_root / project.name / task.id / "attempt-01" / "task-result.json"
    ).exists()


class _UnrelatedHeadRunner(ScriptedRunner):
    def __init__(self, unrelated: str):
        self.unrelated = unrelated

    def run(self, *, cwd, artifact_dir, phase, **kwargs) -> CodexRunResult:
        if phase == "implementation":
            git(Path(cwd), "checkout", "--detach", self.unrelated)
        return super().run(cwd=cwd, artifact_dir=artifact_dir, phase=phase, **kwargs)


def test_every_created_candidate_is_rejected_when_head_moved_to_unrelated_history(
    tmp_path: Path, monkeypatch
):
    repo, _remote_head, base, advanced = repository_with_moved_default_branch(tmp_path)
    git(repo, "checkout", "--orphan", "unrelated-seed")
    git(repo, "rm", "-rf", ".")
    unrelated = _commit(repo, "unrelated.txt", "unrelated\n")
    git(repo, "checkout", "main")
    assert current_commit(repo) == advanced

    orchestrator, project, task, config = _terminal_orchestrator(
        tmp_path,
        repo,
        monkeypatch,
        task_id="SC-ADMISSION-UNRELATED",
        base=base,
    )
    orchestrator.runner = _UnrelatedHeadRunner(unrelated)

    with pytest.raises(CandidateStateUnavailable) as raised:
        orchestrator._execute_task_body(task)
    orchestrator._handle_unexpected_failure(task, raised.value)

    stored = orchestrator.db.get_task(task.id)
    worktree = Path(stored.worktree_path)
    candidate = current_commit(worktree)
    assert candidate != unrelated
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, candidate], cwd=worktree
    ).returncode != 0
    assert stored.state is TaskState.FAILED
    assert stored.last_error.startswith("CANDIDATE_STATE_UNAVAILABLE: ")
    assert stored.failure_fingerprint is None
    assert stored.identical_failures == 0
    assert stored.result == {}
    assert _safe_task_error(stored.last_error)[0] == "CANDIDATE_STATE_UNAVAILABLE"
    assert not (
        config.evidence_root / project.name / task.id / "attempt-01" / "task-result.json"
    ).exists()


# ====================================================== 4. every terminal field is exactly B..C


def test_every_surfaced_terminal_field_agrees_with_the_real_git_range(
    tmp_path: Path, monkeypatch
):
    """One cross-check over the candidate, the changed files, the patch and the patch hash."""
    repo, _remote_head, base, advanced = repository_with_moved_default_branch(tmp_path)
    orchestrator, project, task, config = _terminal_orchestrator(
        tmp_path, repo, monkeypatch, task_id="SC-ADMISSION-RANGE", base=base
    )
    orchestrator.runner = ScriptedRunner()

    assert orchestrator._execute_task_body(task) == OUTCOME_COMPLETED

    terminal = _terminal_document(config, project, task)
    candidate = terminal["commit"]
    expected_patch = git(repo, "diff", "--binary", f"{base}..{candidate}", "--")

    assert terminal["base_commit"] == base
    assert terminal["admission_base_commit"] == base
    assert candidate == current_commit(Path(terminal["worktree"]))
    assert git(repo, "rev-parse", f"{candidate}^").strip() == base
    assert terminal["changed_files"] == git(
        repo, "diff", "--name-only", f"{base}..{candidate}", "--"
    ).split()
    assert Path(terminal["patch"]).read_text(encoding="utf-8") == expected_patch
    assert orchestrator.db.get_task(task.id).result["commit"] == candidate

    # The patch artifact's published hash is the hash of exactly that range.
    listed = evidence_list(config, task.id, schema_version="1.3")
    patch_artifact = next(item for item in listed["artifacts"] if item["artifact_kind"] == "patch")
    shown = evidence_show(config, task.id, patch_artifact["artifact_id"], schema_version="1.3")
    assert shown["content"]["sha256"] == hashlib.sha256(
        Path(terminal["patch"]).read_bytes()
    ).hexdigest()
    assert advanced not in json.dumps(terminal)


# =========================================================== 5. evidence contract version gating


_PRE_1_3_FINAL_RESULT_FIELDS = {
    "status",
    "summary",
    "changed_files",
    "validation_performed",
    "assumptions",
    "blocking_reason",
    "decisions_needed",
    "reusable_patterns",
    "review",
    "controller_validation",
    "branch",
    "commit",
    "completed_at",
}


def _final_result_projection(config: ForgeConfig, task_id: str, schema_version: str) -> dict:
    listed = evidence_list(config, task_id, schema_version=schema_version)
    artifact = next(item for item in listed["artifacts"] if item["artifact_kind"] == "final_result")
    shown = evidence_show(
        config, task_id, artifact["artifact_id"], schema_version=schema_version
    )
    return shown["structured_data"]


@pytest.mark.parametrize("schema_version", ["1.0", "1.1", "1.2"])
def test_evidence_before_1_3_keeps_its_prior_allowlist_exactly(
    tmp_path: Path, monkeypatch, schema_version: str
):
    """An older consumer validated a fixed allowlist; a silently grown one is the same breakage."""
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    orchestrator, _project, task, config = _terminal_orchestrator(
        tmp_path, repo, monkeypatch, task_id="SC-ADMISSION-EV-OLD", base=base
    )
    orchestrator.runner = ScriptedRunner()
    assert orchestrator._execute_task_body(task) == OUTCOME_COMPLETED

    projection = _final_result_projection(config, task.id, schema_version)

    assert set(projection) == _PRE_1_3_FINAL_RESULT_FIELDS
    assert "base_commit" not in projection
    assert "admission_base_commit" not in projection


def test_evidence_at_1_3_adds_the_base_fields_and_changes_nothing_else(
    tmp_path: Path, monkeypatch
):
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    orchestrator, _project, task, config = _terminal_orchestrator(
        tmp_path, repo, monkeypatch, task_id="SC-ADMISSION-EV-NEW", base=base
    )
    orchestrator.runner = ScriptedRunner()
    assert orchestrator._execute_task_body(task) == OUTCOME_COMPLETED

    legacy = _final_result_projection(config, task.id, "1.2")
    declared = _final_result_projection(config, task.id, "1.3")

    assert set(declared) == _PRE_1_3_FINAL_RESULT_FIELDS | {
        "base_commit",
        "admission_base_commit",
    }
    assert declared["base_commit"] == base
    assert declared["admission_base_commit"] == base
    assert {name: declared[name] for name in _PRE_1_3_FINAL_RESULT_FIELDS} == legacy


# ================================================= 6/8. one idempotency law, and the cross-check


def test_the_human_path_converges_on_the_same_task_and_base(tmp_path: Path, capsys):
    config, db, _repo, base = registered(tmp_path, capsys)
    arguments = (
        "task", "add", "--project", "alpha", "--title", "Human idempotent",
        "--objective", "Admit twice from one commit.", "--base-commit", base,
    )

    assert run(config, *arguments) == 0
    first = capsys.readouterr().out
    assert run(config, *arguments) == 0
    second = capsys.readouterr().out

    assert first.startswith("Added ")
    assert second.startswith("Already present:")
    assert len(db.list_tasks()) == 1
    assert only_task(db).admission_base_commit == base


def test_the_human_path_refuses_a_different_base_with_the_same_structured_code(
    tmp_path: Path, capsys
):
    """No raw SQLite integrity error and no COMMAND_FAILED: the same law as the contract path."""
    config, db, repo, base = registered(tmp_path, capsys)
    arguments = (
        "task", "add", "--project", "alpha", "--title", "Human mismatch",
        "--objective", "Admit from one commit.",
    )
    assert run(config, *arguments, "--base-commit", base) == 0
    capsys.readouterr()
    advanced = _commit(repo, "later.txt", "later\n")

    exit_code = run(config, *arguments, "--base-commit", advanced)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "TASK_BASE_MISMATCH" in captured.err
    assert only_task(db).admission_base_commit == base


def test_the_human_path_refuses_binding_a_legacy_task_to_a_base(tmp_path: Path, capsys):
    config, db, _repo, base = registered(tmp_path, capsys)
    arguments = (
        "task", "add", "--project", "alpha", "--title", "Human legacy",
        "--objective", "Admit without a commit.",
    )
    assert run(config, *arguments) == 0
    capsys.readouterr()

    exit_code = run(config, *arguments, "--base-commit", base)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "TASK_BASE_MISMATCH" in captured.err
    assert only_task(db).admission_base_commit is None


def test_the_human_path_reports_a_contract_conflict_rather_than_a_database_error(
    tmp_path: Path, capsys
):
    config, db, _repo, base = registered(tmp_path, capsys)
    assert (
        run(
            config, "task", "add", "--project", "alpha", "--id", "TASK-HUMAN-CONFLICT",
            "--title", "Human conflict", "--objective", "First contract.",
            "--base-commit", base,
        )
        == 0
    )
    capsys.readouterr()

    exit_code = run(
        config, "task", "add", "--project", "alpha", "--id", "TASK-HUMAN-CONFLICT",
        "--title", "Human conflict", "--objective", "A different contract.",
        "--base-commit", base,
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "TASK_ID_CONFLICT" in captured.err
    assert only_task(db).objective == "First contract."


def _concurrent_submissions(config: ForgeConfig, bases: list[str]) -> list[tuple[bool, object]]:
    """Run one submission per base simultaneously through the real submission seam."""
    meet = threading.Barrier(len(bases), timeout=5)
    outcomes: list[tuple[bool, object] | None] = [None] * len(bases)

    def submit_one(index: int, base: str) -> None:
        request = build_task_request(
            argparse.Namespace(
                project="alpha",
                title="Concurrent admission",
                objective="Two writers, one task id.",
                base_commit=base,
                id="TASK-CONCURRENT",
            )
        )
        try:
            meet.wait()
        except threading.BrokenBarrierError:
            pass
        try:
            outcomes[index] = (
                True,
                submit_task(config, request, dry_run=False, schema_version="1.3"),
            )
        except TaskSubmissionError as exc:
            outcomes[index] = (False, exc)

    threads = [
        threading.Thread(target=submit_one, args=(index, base), name=f"submit-{index}")
        for index, base in enumerate(bases)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a submission never finished"
    assert all(outcome is not None for outcome in outcomes)
    return [outcome for outcome in outcomes if outcome is not None]


def test_two_simultaneous_admissions_of_the_same_base_converge_on_one_task(
    tmp_path: Path, capsys
):
    config, db, _repo, base = registered(tmp_path, capsys)

    outcomes = _concurrent_submissions(config, [base, base])

    assert all(succeeded for succeeded, _ in outcomes)
    assert sorted(payload["created"] for _, payload in outcomes) == [False, True]
    assert len(db.list_tasks()) == 1
    assert only_task(db).admission_base_commit == base
    assert all(payload["task"]["admission_base_commit"] == base for _, payload in outcomes)


def test_two_simultaneous_admissions_disagreeing_on_the_base_leave_one_winner(
    tmp_path: Path, capsys
):
    """One task, one durable base, and the loser learns exactly why."""
    config, db, repo, base = registered(tmp_path, capsys)
    advanced = _commit(repo, "later.txt", "later\n")

    outcomes = _concurrent_submissions(config, [base, advanced])

    succeeded = [payload for ok, payload in outcomes if ok]
    refused = [error for ok, error in outcomes if not ok]
    assert len(succeeded) == 1
    assert len(refused) == 1
    assert refused[0].code == "TASK_BASE_MISMATCH"
    assert len(db.list_tasks()) == 1
    stored = only_task(db).admission_base_commit
    assert stored in {base, advanced}
    assert succeeded[0]["task"]["admission_base_commit"] == stored
    assert refused[0].details["admission_base_commit"] == stored


def test_requested_base_equals_durable_base_equals_evidence_base(tmp_path: Path, monkeypatch):
    """The end-to-end identity future Shared Chat verifies without opening forge.db."""
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    orchestrator, _project, task, config = _terminal_orchestrator(
        tmp_path, repo, monkeypatch, task_id="SC-ADMISSION-CROSSCHECK", base=base
    )
    orchestrator.runner = ScriptedRunner()
    assert orchestrator._execute_task_body(task) == OUTCOME_COMPLETED

    durable = orchestrator.db.get_task(task.id).admission_base_commit
    projection = _final_result_projection(config, task.id, "1.3")

    assert base == durable == projection["admission_base_commit"] == projection["base_commit"]


# ============================================ 7. a missing base is a repository failure, not a defect


def test_a_missing_base_at_execution_is_classified_as_a_repository_failure(
    tmp_path: Path, monkeypatch
):
    """No fabricated implementation verdict, no repair cycle, no identical-failure counter."""
    repo, _remote_head, base, _advanced = repository_with_moved_default_branch(tmp_path)
    orchestrator, _project, task, _config = _terminal_orchestrator(
        tmp_path, repo, monkeypatch, task_id="SC-ADMISSION-MISSING", base="0" * len(base)
    )
    orchestrator.runner = _ExplodingRunner()

    try:
        orchestrator._execute_task_body(task)
    except BaseCommitUnavailable as exc:
        failure = exc
    else:
        raise AssertionError("the attempt did not fail closed on the missing admission base")
    orchestrator._handle_unexpected_failure(task, failure)

    stored = orchestrator.db.get_task(task.id)
    assert stored.state is TaskState.FAILED
    assert stored.last_error.startswith("REPOSITORY_UNAVAILABLE: ")
    assert stored.failure_fingerprint is None
    assert stored.identical_failures == 0
    assert stored.result == {}
    code, message = _safe_task_error(stored.last_error)
    assert code == "REPOSITORY_UNAVAILABLE"
    assert "not a finding about the task's code" in message


class _ExplodingRunner:
    """Any model call at all would mean the attempt got past the repository fault."""

    def resolve_sandbox(self, requested: str, *, cwd: Path) -> SandboxResolution:
        raise AssertionError("a repository fault must not reach the reviewer")

    def run(self, **_kwargs) -> CodexRunResult:
        raise AssertionError("a repository fault must not reach a model call")


class _NoOpRunner(ScriptedRunner):
    """Changes nothing at all, so the reused branch is the only possible candidate source."""

    writes_candidate = False


def _terminal_orchestrator(
    tmp_path: Path,
    repo: Path,
    monkeypatch,
    *,
    task_id: str,
    base: str | None,
    auto_commit: bool = True,
    auto_push: bool = False,
) -> tuple[Orchestrator, Project, Task, ForgeConfig]:
    config = load_config(configured(tmp_path / f"forge-{task_id}").config_path)
    orchestrator = Orchestrator(config, Path(__file__).parents[1])
    project = project_for(repo, name="terminal")
    project.auto_commit = auto_commit
    project.auto_push = auto_push
    task = task_for(project, base, task_id=task_id)
    orchestrator.db.add_project(project)
    orchestrator.db.add_task(task)
    monkeypatch.setattr(
        orchestrator,
        "_validate",
        lambda *_args, **_kwargs: [
            ValidationResult(
                command="controlled-validation",
                exit_code=0,
                passed=True,
                duration_seconds=0.01,
                stdout_path=tmp_path / "validation.stdout.log",
                stderr_path=tmp_path / "validation.stderr.log",
                summary="passed",
            )
        ],
    )
    return orchestrator, project, task, config


def _terminal_document(config: ForgeConfig, project: Project, task: Task) -> dict:
    return json.loads(
        (
            config.evidence_root / project.name / task.id / "attempt-01" / "task-result.json"
        ).read_text(encoding="utf-8")
    )
