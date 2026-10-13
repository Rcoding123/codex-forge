from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_execution_checkpoints import Worker
from test_final_evidence import git, initialize_repository, make_orchestrator, passing_validation

from codex_forge.cli import cmd_task_retry, main
from codex_forge.db import ForgeDB
from codex_forge.gitops import CandidateStateUnavailable, GitError
from codex_forge.models import Project, TaskState
from codex_forge.orchestrator import Orchestrator
from codex_forge.utils import controller_lock


class ChainWorker(Worker):
    def __init__(self):
        super().__init__()
        self.implementations = 0

    def run(self, **kwargs):
        result = super().run(**kwargs)
        if kwargs["phase"] == "implementation":
            self.implementations += 1
            worktree = Path(kwargs["cwd"])
            if self.implementations == 2:
                assert (worktree / "first.txt").read_text() == "first\n"
            name = "first" if self.implementations == 1 else "second"
            (worktree / f"{name}.txt").write_text(name + "\n", encoding="utf-8")
        return result


def setup(tmp_path, monkeypatch, *, gate="python -c \"print('gate passed')\"", separate_phases=True):
    repo, base = initialize_repository(tmp_path)
    orchestrator, first = make_orchestrator(tmp_path, repo)
    with orchestrator.db.connect() as connection:
        connection.execute("UPDATE tasks SET state='READY',attempts=0 WHERE id=?", (first.id,))
        connection.execute("UPDATE projects SET validation_json='[]'")
    first = orchestrator.db.get_task(first.id)
    second = replace(first, id="second-task", dependencies=[first.id], state=TaskState.BACKLOG, priority=999)
    orchestrator.db.add_task(second)
    orchestrator.runner = ChainWorker()
    monkeypatch.setattr(orchestrator, "_validate", lambda *_, **__: passing_validation(tmp_path, True))
    project = orchestrator.db.get_project(first.project_id)
    phases = [{"name": "P1", "tasks": [first.id], "acceptance_commands": [gate]},
              {"name": "P2", "tasks": [second.id], "acceptance_commands": [gate]}]
    if not separate_phases:
        phases = [{"name": "P1", "tasks": [first.id, second.id], "acceptance_commands": [gate]}]
    definition = {"branch": "forge-project/pilot", "base_commit": base, "phases": phases}
    return orchestrator, project, first, second, definition


def install(orchestrator, project, definition):
    with controller_lock(orchestrator.config.data_root / "controller.lock"):
        return orchestrator.workflows.install(project, definition)


def test_two_phases_integrate_exact_candidates_and_inherit_prior_code(tmp_path, monkeypatch):
    o, project, first, second, definition = setup(tmp_path, monkeypatch)
    install(o, project, definition)
    for _ in range(8):
        o.run_once()
    document = o.workflows.get(project.id)
    assert document["state"] == "COMPLETED"
    assert [gate["status"] for gate in document["gates"]] == ["PASSED", "PASSED"]
    a, b = o.db.get_task(first.id), o.db.get_task(second.id)
    assert a.result["base_commit"] == definition["base_commit"]
    assert b.result["base_commit"] == a.result["commit"]
    assert a.admission_base_commit is b.admission_base_commit is None
    assert document["head_commit"] == b.result["commit"]
    assert git(project.repo_path, "rev-parse", "forge-project/pilot").strip() == b.result["commit"]
    assert git(project.repo_path, "rev-parse", "main").strip() == definition["base_commit"]
    assert git(project.repo_path, "status", "--porcelain") == ""
    assert o.runner.implementations == 2
    assert "Execution base commit" in o.runner.prompts[2]
    assert len(list((o.config.data_root / "project-evidence").rglob("result.json"))) == 2


def test_claims_serialize_project_without_starving_unrelated_ready_work(tmp_path, monkeypatch):
    o, project, first, second, definition = setup(tmp_path, monkeypatch)
    install(o, project, definition)
    other = Project(id="other-project", name="other", repo_path=tmp_path / "other", auto_fetch=False)
    o.db.add_project(other)
    o.db.add_task(replace(first, id="other-task", project_id=other.id, priority=1))
    o.db.add_task(replace(first, id="unplanned-task", priority=1000))
    claimed = o.db.claim_ready_tasks(8)
    assert {task.id for task in claimed} == {first.id, "other-task"}
    assert o.db.claim_ready_tasks(8) == []
    assert o.db.get_task(second.id).attempts == 0


def test_failed_phase_gate_blocks_next_phase_and_preserves_real_command_logs(tmp_path, monkeypatch):
    o, project, first, second, definition = setup(
        tmp_path, monkeypatch, gate="python -c \"print('ACTUAL_GATE_FAILURE'); raise SystemExit(3)\"",
    )
    install(o, project, definition)
    for _ in range(4):
        o.run_once()
    document = o.workflows.get(project.id)
    assert document["state"] == "BLOCKED"
    assert document["last_error"] == "PROJECT_ACCEPTANCE_FAILED"
    assert document["gates"][-1]["validation"][0]["exit_code"] == 3
    assert o.db.get_task(first.id).state == TaskState.COMPLETED
    assert o.db.get_task(second.id).attempts == 0
    assert "ACTUAL_GATE_FAILURE" in next((o.config.data_root / "project-evidence").rglob("summary.txt")).read_text()
    # Restart leaves a failed gate blocked; explicit resume retries only the gate.
    restarted = Orchestrator(o.config, Path(__file__).parents[1])
    assert restarted.run_once() == 0
    restarted.workflows.resume(project.id)
    restarted.run_once()
    assert len(restarted.workflows.get(project.id)["gates"]) == 2
    assert o.runner.implementations == 1


@pytest.mark.parametrize("boundary", ["before_ref", "after_ref", "before_completed"])
def test_restart_reconciles_pending_integration_without_repeating_model_work(tmp_path, monkeypatch, boundary):
    o, project, first, _, definition = setup(tmp_path, monkeypatch)
    install(o, project, definition)
    from codex_forge import workflows
    original_git = workflows._git
    original_update = o.db.update_task

    def crashing_git(repo, *args, **kwargs):
        if args[0] == "update-ref":
            if boundary == "before_ref":
                raise KeyboardInterrupt()
            result = original_git(repo, *args, **kwargs)
            if boundary == "after_ref":
                raise KeyboardInterrupt()
            return result
        return original_git(repo, *args, **kwargs)

    def crashing_completion(task_id, **kwargs):
        if boundary == "before_completed" and kwargs.get("state") == TaskState.COMPLETED:
            raise KeyboardInterrupt()
        return original_update(task_id, **kwargs)

    monkeypatch.setattr(workflows, "_git", crashing_git)
    monkeypatch.setattr(o.db, "update_task", crashing_completion)
    task = o.db.claim_ready_tasks(1)[0]
    with pytest.raises(KeyboardInterrupt):
        o._execute_task_body(task)
    checkpoint = o.db.execution_checkpoint(task.id)
    assert o.workflows.get(project.id)["pending"]["candidate_commit"] == checkpoint["candidate_commit"]
    monkeypatch.setattr(workflows, "_git", original_git)
    restarted = Orchestrator(o.config, Path(__file__).parents[1])
    restarted.runner = o.runner
    monkeypatch.setattr(restarted, "_validate", lambda *_, **__: pytest.fail("candidate already verified"))
    restarted.run_once()
    assert restarted.db.get_task(first.id).state == TaskState.COMPLETED
    assert restarted.workflows.get(project.id)["head_commit"] == checkpoint["candidate_commit"]
    assert restarted.runner.implementations == 1


@pytest.mark.parametrize("change", ["tip", "checked_out", "generation", "specification", "explicit_base"])
def test_execution_rejects_changed_authority_before_worker_start(tmp_path, monkeypatch, change):
    o, project, first, _, definition = setup(tmp_path, monkeypatch)
    install(o, project, definition)
    if change == "tip":
        git(project.repo_path, "commit", "--allow-empty", "-m", "foreign")
        git(project.repo_path, "update-ref", "refs/heads/forge-project/pilot", "HEAD")
    elif change == "checked_out":
        git(project.repo_path, "switch", "forge-project/pilot")
    elif change in ("generation", "specification"):
        with o.db.connect() as connection:
            if change == "generation":
                connection.execute("UPDATE projects SET repository_registration_id='foreign'")
            else:
                connection.execute("UPDATE tasks SET objective='changed'")
    else:
        with o.db.connect() as connection:
            connection.execute("UPDATE tasks SET admission_base_commit='foreign' WHERE id=?", (first.id,))
    task = o.db.claim_ready_tasks(1)[0]
    with pytest.raises(CandidateStateUnavailable):
        o._execute_task_body(task)
    assert o.runner.phases == []


@pytest.mark.parametrize("gate", [
    "python -c \"open('unreviewed.txt','w').write('bad')\"",
    "git commit --allow-empty -m unreviewed",
])
def test_gate_cannot_release_next_phase_after_modifying_the_checked_candidate(tmp_path, monkeypatch, gate):
    o, project, _, second, definition = setup(tmp_path, monkeypatch, gate=gate)
    install(o, project, definition)
    for _ in range(4):
        o.run_once()
    assert o.workflows.get(project.id)["state"] == "BLOCKED"
    assert o.db.get_task(second.id).attempts == 0


@pytest.mark.parametrize("change", ["main", "existing", "duplicate", "empty_gate", "unstarted", "foreign_task", "order"])
def test_invalid_plan_is_refused_without_installing_state(tmp_path, monkeypatch, change):
    o, project, first, second, definition = setup(tmp_path, monkeypatch)
    if change == "main":
        definition["branch"] = "main"
    elif change == "existing":
        git(project.repo_path, "branch", definition["branch"])
    elif change == "duplicate":
        definition["phases"][1]["tasks"] = [first.id]
    elif change == "empty_gate":
        definition["phases"][0]["acceptance_commands"] = []
    elif change == "unstarted":
        with o.db.connect() as connection:
            connection.execute("UPDATE tasks SET attempts=1 WHERE id=?", (first.id,))
    elif change == "foreign_task":
        definition["phases"][0]["tasks"] = ["missing-task"]
    else:
        definition["phases"].reverse()
    with pytest.raises((ValueError, GitError)):
        install(o, project, definition)
    assert o.workflows.get(project.id) is None
    assert o.db.get_task(second.id).attempts == 0


def test_workflow_migration_is_concurrent_and_deletion_retains_project_evidence(tmp_path, monkeypatch):
    o, project, first, second, definition = setup(tmp_path, monkeypatch)
    with o.db.connect() as connection:
        connection.execute("DROP TABLE project_workflows")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ForgeDB(o.config.database_path).initialize(), range(8)))
    document = install(o, project, definition)
    for task in (first, second):
        o.db.delete_task(task.id)
    o.db.delete_project(project.id)
    assert o.workflows.get(project.id) is None
    assert (o.config.data_root / "project-evidence" / document["id"] / "plan.json").is_file()


def test_project_plan_cli_uses_supported_envelopes_and_does_not_expose_raw_gate_output(tmp_path, monkeypatch, capsys):
    o, project, _, _, definition = setup(tmp_path, monkeypatch)
    definition["phases"][0]["acceptance_commands"] = ["tool --password top-secret-value"]
    monkeypatch.setattr("codex_forge.cli.load_config", lambda *_: o.config)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(definition), encoding="utf-8")
    assert main(["project", "plan", project.id, "--file", str(plan_file), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "project.plan" and result["schema_version"] == "1.0"
    assert result["data"]["plan"]["state"] == "RUNNING"
    assert "top-secret-value" not in json.dumps(result)
    assert "top-secret-value" in o.workflows.get(project.id)["phases"][0]["acceptance_commands"][0]
    assert main(["project", "progress", project.id, "--json", "--schema-version", "1.4"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == "1.4"
    assert "top-secret-value" not in json.dumps(result)
    assert "specifications" not in result["data"]["plan"]
    assert main(["project", "plan", project.id, "--file", str(plan_file), "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "INVALID_ARGUMENT"


def test_interrupted_gate_reruns_in_a_new_checkout_and_preserves_the_previous_attempt(tmp_path, monkeypatch):
    o, project, _, _, definition = setup(tmp_path, monkeypatch)
    install(o, project, definition)
    o.run_once()
    o.run_once()
    from codex_forge import workflows
    original = workflows.run_validations

    def interrupted(*args, **kwargs):
        (kwargs["artifact_dir"] / "partial.log").write_text("partial gate evidence", encoding="utf-8")
        raise KeyboardInterrupt()

    monkeypatch.setattr(workflows, "run_validations", interrupted)
    with pytest.raises(KeyboardInterrupt):
        o.workflows.advance_all()
    prior = o.workflows.get(project.id)["gates"][-1]["id"]
    monkeypatch.setattr(workflows, "run_validations", original)
    restarted = Orchestrator(o.config, Path(__file__).parents[1])
    restarted.workflows.advance_all()
    gates = restarted.workflows.get(project.id)["gates"]
    assert gates[0]["id"] == prior and gates[0]["status"] == "INTERRUPTED"
    assert gates[1]["id"] != prior and gates[1]["status"] == "PASSED"
    assert next((o.config.data_root / "project-evidence").rglob("partial.log")).read_text() == "partial gate evidence"
    assert o.runner.implementations == 1


def test_multiple_tasks_in_one_phase_use_the_integrated_tip(tmp_path, monkeypatch):
    o, project, _, _, definition = setup(tmp_path, monkeypatch, separate_phases=False)
    install(o, project, definition)
    for _ in range(6):
        o.run_once()
    document = o.workflows.get(project.id)
    assert document["state"] == "COMPLETED"
    assert len(document["integrations"]) == 2 and len(document["gates"]) == 1


def test_deleting_required_task_blocks_plan_and_does_not_delete_evidence(tmp_path, monkeypatch):
    o, project, first, second, definition = setup(tmp_path, monkeypatch)
    document = install(o, project, definition)
    o.run_once()
    o.db.delete_task(first.id)
    o.run_once()
    assert o.workflows.get(project.id)["state"] == "BLOCKED"
    assert o.db.get_task(second.id).attempts == 0
    assert (o.config.data_root / "project-evidence" / document["id"] / "plan.json").exists()


def test_workflow_save_refuses_another_plan_generation(tmp_path, monkeypatch):
    o, project, _, _, definition = setup(tmp_path, monkeypatch)
    document = install(o, project, definition)
    with o.db.connect() as connection:
        connection.execute(
            "UPDATE project_workflows SET document_json=json_set(document_json,'$.id',?)",
            ("workflow-" + "f" * 32,),
        )
    with pytest.raises(CandidateStateUnavailable):
        with o.db.connect() as connection:
            o.workflows._save(connection, project.id, document)
    assert o.workflows.get(project.id)["id"] == "workflow-" + "f" * 32


def test_gate_refuses_reopened_completion_and_relocation_is_blocked(tmp_path, monkeypatch):
    o, project, first, second, definition = setup(tmp_path, monkeypatch)
    install(o, project, definition)
    o.run_once()
    o.run_once()
    from codex_forge import workflows
    original = workflows.run_validations

    def raced_gate(*args, **kwargs):
        assert cmd_task_retry(o.config, first.id) == 2
        assert o.db.relocate_project(project.id, tmp_path / "relocated").outcome == "active_workflow"
        # Bypass public APIs to exercise the final transaction's independent proof as well.
        o.db.update_task(first.id, state=TaskState.READY)
        return original(*args, **kwargs)

    monkeypatch.setattr(workflows, "run_validations", raced_gate)
    o.run_once()
    assert o.workflows.get(project.id)["state"] == "BLOCKED"
    assert o.db.get_task(second.id).attempts == 0


def test_completed_plan_can_extend_without_changing_the_previous_evidence(tmp_path, monkeypatch):
    o, project, first, _, definition = setup(tmp_path, monkeypatch)
    prior = install(o, project, definition)
    prior_plan_path = o.config.data_root / "project-evidence" / prior["id"] / "plan.json"
    original_plan = prior_plan_path.read_bytes()
    with pytest.raises(ValueError, match="completed"):
        o.workflows.install(project, definition, extend=True)
    for _ in range(8):
        o.run_once()
    completed = o.workflows.get(project.id)
    third = replace(first, id="third-task", state=TaskState.READY)
    o.db.add_task(third)
    next_plan = {"branch": definition["branch"], "base_commit": completed["head_commit"],
                 "phases": [{"name": "P3", "tasks": [third.id], "acceptance_commands": ["python -c \"print('ok')\""]}]}
    with controller_lock(o.config.data_root / "controller.lock"):
        extended = o.workflows.install(project, next_plan, extend=True)
    assert extended["previous_workflow_id"] == completed["id"]
    assert prior_plan_path.read_bytes() == original_plan
    archived = json.loads((prior_plan_path.parent / "completed-plan.json").read_text())
    assert archived == completed
    for _ in range(5):
        o.run_once()
    assert o.workflows.get(project.id)["state"] == "COMPLETED"
    assert o.db.get_task(third.id).result["base_commit"] == completed["head_commit"]


def test_corrupt_completed_identity_cannot_escape_the_project_evidence_root(tmp_path, monkeypatch):
    o, project, first, _, definition = setup(tmp_path, monkeypatch)
    install(o, project, definition)
    for _ in range(8):
        o.run_once()
    document = o.workflows.get(project.id)
    o.db.add_task(replace(first, id="next-task", state=TaskState.READY))
    extension = {"branch": definition["branch"], "base_commit": document["head_commit"],
                 "phases": [{"name": "Next", "tasks": ["next-task"], "acceptance_commands": ["echo check"]}]}
    with o.db.connect() as connection:
        connection.execute("UPDATE project_workflows SET document_json=json_set(document_json,'$.id','../escape')")
    with pytest.raises(CandidateStateUnavailable, match="identity"):
        o.workflows.install(project, extension, extend=True)
    assert not (o.config.data_root / "escape").exists()


def test_deleting_project_during_gate_does_not_abort_unrelated_queue_progress(tmp_path, monkeypatch):
    o, project, first, second, definition = setup(tmp_path, monkeypatch)
    install(o, project, definition)
    o.run_once()
    o.run_once()
    other = Project(id="other-project", name="other", repo_path=tmp_path / "other", auto_fetch=False)
    o.db.add_project(other)
    o.db.add_task(replace(first, id="other-task", project_id=other.id))
    from codex_forge import workflows
    original = workflows.run_validations

    def deleting_gate(*args, **kwargs):
        for task in (first, second):
            o.db.delete_task(task.id)
        assert o.db.delete_project(project.id)[0] == "deleted"
        return original(*args, **kwargs)

    monkeypatch.setattr(workflows, "run_validations", deleting_gate)
    o.workflows.advance_all()
    assert o.workflows.get(project.id) is None
    assert [task.id for task in o.db.claim_ready_tasks(1)] == ["other-task"]


def test_final_gate_evidence_write_failure_keeps_a_resumable_cursor(tmp_path, monkeypatch):
    o, project, _, _, definition = setup(tmp_path, monkeypatch, separate_phases=False)
    install(o, project, definition)
    o.run_once()
    o.run_once()
    o.run_once()
    original = Path.write_text

    def failed_write(path, *args, **kwargs):
        if path.name == "result.json":
            raise OSError("evidence storage unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failed_write)
    o.run_once()
    document = o.workflows.get(project.id)
    assert document["state"] == "BLOCKED" and document["phase_index"] == 0
    assert document["task_index"] == 2
    monkeypatch.setattr(Path, "write_text", original)
    o.workflows.resume(project.id)
    o.run_once()
    assert o.workflows.get(project.id)["state"] == "COMPLETED"
    assert o.runner.implementations == 2


def test_deletion_between_workflow_inventory_and_read_is_safe(tmp_path, monkeypatch):
    o, project, first, second, definition = setup(tmp_path, monkeypatch)
    install(o, project, definition)
    original = o.workflows.get

    def deletion_before_read(project_id):
        for task in (first, second):
            o.db.delete_task(task.id)
        o.db.delete_project(project_id)
        return original(project_id)

    monkeypatch.setattr(o.workflows, "get", deletion_before_read)
    assert o.workflows.advance_all() == 0


@pytest.mark.parametrize("state", [TaskState.COMPLETED, TaskState.IMPLEMENTING, TaskState.VALIDATING,
                                    TaskState.REVIEWING, TaskState.REPAIRING, TaskState.READY])
def test_manual_retry_never_reopens_completed_active_or_queued_work(tmp_path, monkeypatch, state):
    o, _, first, _, _ = setup(tmp_path, monkeypatch)
    o.db.update_task(first.id, state=state)
    assert cmd_task_retry(o.config, first.id) == 2
    assert o.db.get_task(first.id).state == state


def test_project_publication_failure_restarts_without_repeating_workers(tmp_path, monkeypatch):
    o, project, first, _, definition = setup(tmp_path, monkeypatch)
    with o.db.connect() as connection:
        connection.execute("UPDATE projects SET auto_push=1")
    project = o.db.get_project(project.id)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(project.repo_path, "remote", "add", "origin", str(remote))
    install(o, project, definition)
    from codex_forge import workflows
    original = workflows.push_candidate
    monkeypatch.setattr(workflows, "push_candidate", lambda *_: (_ for _ in ()).throw(GitError("offline")))
    o.run_once()
    assert o.db.get_task(first.id).state == TaskState.READY
    candidate = o.db.execution_checkpoint(first.id)["candidate_commit"]
    monkeypatch.setattr(workflows, "push_candidate", original)
    restarted = Orchestrator(o.config, Path(__file__).parents[1])
    restarted.runner = o.runner
    restarted.run_once()
    assert restarted.db.get_task(first.id).state == TaskState.COMPLETED
    assert restarted.runner.implementations == 1
    assert git(project.repo_path, "ls-remote", "origin", "refs/heads/forge-project/pilot").split()[0] == candidate
