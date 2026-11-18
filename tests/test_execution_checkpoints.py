from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_final_evidence import completed_result, git, initialize_repository, make_orchestrator, passing_validation
from test_review_infrastructure import run_result

from codex_forge.db import ForgeDB
from codex_forge.gitops import CandidateStateUnavailable, GitError
from codex_forge.models import TaskState
from codex_forge.orchestrator import Orchestrator, OUTCOME_COMPLETED, OUTCOME_VALIDATION_FAILED
from codex_forge.runner import SandboxResolution


class Worker:
    def __init__(self):
        self.phases = []
        self.prompts = []

    def resolve_sandbox(self, requested, *, cwd):
        return SandboxResolution(requested=requested, sandbox="danger-full-access", probe=None)

    def run(self, *, cwd, artifact_dir, phase, prompt, **_):
        self.phases.append(phase)
        self.prompts.append(prompt)
        if phase == "implementation":
            (Path(cwd) / "candidate.txt").write_text("reviewed work\n", encoding="utf-8")
            final = completed_result(["candidate.txt"], "implemented")
        elif phase.startswith("review-repair"):
            final = completed_result([], "repaired")
        else:
            final = {"verdict": "pass", "summary": "reviewed", "acceptance_criteria": [],
                     "binding_requirements": [], "findings": []}
        return run_result(Path(artifact_dir), phase, final, [])


def setup(tmp_path, monkeypatch, push=False):
    repo, base = initialize_repository(tmp_path)
    orchestrator, task = make_orchestrator(tmp_path, repo)
    worker = Worker()
    orchestrator.runner = worker
    monkeypatch.setattr(orchestrator, "_validate", lambda *_, **__: passing_validation(tmp_path, True))
    if push:
        with orchestrator.db.connect() as connection:
            connection.execute("UPDATE projects SET auto_push=1 WHERE id=?", (task.project_id,))
    return orchestrator, task, base, worker


def test_failed_push_restarts_without_model_work_and_retains_original_delta(tmp_path, monkeypatch):
    orchestrator, task, base, worker = setup(tmp_path, monkeypatch, push=True)
    repo = orchestrator.db.get_project(task.project_id).repo_path
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    from codex_forge import gitops

    real_push = gitops.push_candidate

    def failed(*_):
        raise GitError("temporary publication failure")

    monkeypatch.setattr("codex_forge.orchestrator.push_candidate", failed)
    with pytest.raises(GitError) as error:
        orchestrator._execute_task_body(task)
    orchestrator._handle_unexpected_failure(task, error.value)
    checkpoint = orchestrator.db.execution_checkpoint(task.id)
    candidate = checkpoint["candidate_commit"]
    assert checkpoint["base_commit"] == base
    assert checkpoint["publication_required"] == 1
    assert worker.phases == ["implementation", "review"]

    # Reload the controller and DB, not just the in-memory task.
    restarted = Orchestrator(orchestrator.config, Path(__file__).parents[1])
    restarted.runner = worker
    monkeypatch.setattr("codex_forge.orchestrator.push_candidate", real_push)
    monkeypatch.setattr(restarted, "_validate", lambda *_args, **_kwargs: pytest.fail("already validated"))
    retried = restarted.db.claim_ready_tasks(1)[0]
    assert restarted._execute_task_body(retried) == OUTCOME_COMPLETED
    assert worker.phases == ["implementation", "review"]
    result = restarted.db.get_task(task.id).result
    assert result["base_commit"] == base
    assert result["admission_base_commit"] is None
    assert result["commit"] == candidate
    assert result["changed_files"] == ["candidate.txt"]
    assert Path(result["patch"]).read_text(encoding="utf-8") == git(repo, "diff", "--binary", f"{base}..{candidate}")
    assert git(repo, "ls-remote", "origin", f"refs/heads/{retried.branch}").split()[0] == candidate


@pytest.mark.parametrize("change", ["dirty", "head", "specification", "generation", "malformed"])
def test_completion_resume_rejects_changed_identity_or_candidate(tmp_path, monkeypatch, change):
    orchestrator, task, _, worker = setup(tmp_path, monkeypatch, push=True)
    monkeypatch.setattr("codex_forge.orchestrator.push_candidate", lambda *_: (_ for _ in ()).throw(GitError("offline")))
    with pytest.raises(GitError) as error:
        orchestrator._execute_task_body(task)
    orchestrator._handle_unexpected_failure(task, error.value)
    retry = orchestrator.db.claim_ready_tasks(1)[0]
    if change == "dirty":
        (retry.worktree_path / "unreviewed.txt").write_text("no", encoding="utf-8")
    elif change == "head":
        git(retry.worktree_path, "commit", "--allow-empty", "-m", "unreviewed commit")
    elif change == "specification":
        retry = replace(retry, objective="changed objective")
    else:
        with orchestrator.db.connect() as connection:
            if change == "generation":
                connection.execute("UPDATE execution_checkpoints SET repository_registration_id='foreign' WHERE task_id=?", (task.id,))
            else:
                connection.execute("UPDATE execution_checkpoints SET completion_json='{' WHERE task_id=?", (task.id,))
    with pytest.raises(CandidateStateUnavailable):
        orchestrator._execute_task_body(retry)
    assert worker.phases == ["implementation", "review"]


def test_checkpoint_base_is_immutable_concurrent_and_removed_with_operational_task(tmp_path, monkeypatch):
    orchestrator, task, base, _ = setup(tmp_path, monkeypatch)
    registration = orchestrator.db.get_project(task.project_id).repository_registration_id
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: orchestrator.db.establish_execution_base(task.id, registration, base), range(8)))
    with pytest.raises(ValueError, match="immutable"):
        orchestrator.db.establish_execution_base(task.id, registration, "different")
    orchestrator.db.update_task(task.id, state=TaskState.FAILED)
    assert orchestrator.db.delete_task(task.id)[0] == "deleted"
    assert orchestrator.db.execution_checkpoint(task.id) is None


def test_checkpoint_table_migration_is_concurrent_and_preserves_legacy_tasks(tmp_path, monkeypatch):
    orchestrator, task, _, _ = setup(tmp_path, monkeypatch)
    with orchestrator.db.connect() as connection:
        connection.execute("DROP TABLE execution_checkpoints")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ForgeDB(orchestrator.config.database_path).initialize(), range(8)))
    assert orchestrator.db.get_task(task.id).admission_base_commit is None
    assert orchestrator.db.execution_checkpoint(task.id) is None


def test_uncheckpointed_legacy_interruption_fails_closed(tmp_path, monkeypatch):
    orchestrator, task, _, worker = setup(tmp_path, monkeypatch)
    retry = replace(task, attempts=2, branch="forge/legacy", worktree_path=tmp_path / "legacy")
    with pytest.raises(CandidateStateUnavailable, match="verifiable execution base"):
        orchestrator._execute_task_body(retry)
    assert worker.phases == []


def test_review_repair_receives_new_failure_and_keeps_validation_ownership(tmp_path, monkeypatch):
    orchestrator, task, _, worker = setup(tmp_path, monkeypatch)
    orchestrator.config.max_repair_cycles = 2
    original = worker.run

    def review_changes(**kwargs):
        result = original(**kwargs)
        if kwargs["phase"] == "review":
            result.final.update(verdict="changes_required", findings=[{
                "severity": "high", "file": "candidate.txt", "line": 1,
                "issue": "ORIGINAL_REVIEW_FINDING", "required_fix": "Repair it.",
            }])
            result.final_path.write_text(json.dumps(result.final), encoding="utf-8")
        return result

    worker.run = review_changes

    def validation(*_, suffix="initial", **__):
        result = passing_validation(tmp_path, suffix == "initial")
        if suffix != "initial":
            result[0].summary = "NEW_REGRESSION_AFTER_REPAIR"
        return result

    monkeypatch.setattr(orchestrator, "_validate", validation)
    assert orchestrator._execute_task_body(task) == OUTCOME_VALIDATION_FAILED
    assert worker.phases == ["implementation", "review", "review-repair-1", "review-repair-2"]
    assert "NEW_REGRESSION_AFTER_REPAIR" in worker.prompts[-1]
    assert "ORIGINAL_REVIEW_FINDING" in worker.prompts[-1]
    assert "NEW_REGRESSION_AFTER_REPAIR" in orchestrator.db.get_task(task.id).last_error
