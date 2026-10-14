"""No model requests: exhausted blockers consume a reserved existing repair, never attempt four."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_final_evidence import completed_result, initialize_repository, make_orchestrator, passing_validation
from test_review_infrastructure import run_result

from codex_forge.blockers import queue_resolution, candidate_snapshot
from codex_forge.evidence import evidence_list
from codex_forge.gitops import CandidateStateUnavailable
from codex_forge.models import TaskState
from codex_forge.orchestrator import Orchestrator
from codex_forge.runner import SandboxResolution
from codex_forge.task_submission import task_fingerprint
from codex_forge.utils import ControllerAlreadyRunning, controller_lock


class BlockedWorker:
    def __init__(self, *, validation_break=False):
        self.phases = []
        self.prompts = []
        self.validation_break = validation_break

    def resolve_sandbox(self, requested, *, cwd):
        return SandboxResolution(requested=requested, sandbox="danger-full-access", probe=None)

    def run(self, *, phase, artifact_dir, cwd, prompt, **_):
        self.phases.append(phase)
        self.prompts.append(prompt)
        if phase == "implementation":
            final = {**completed_result([], "operator evidence is required"), "status": "blocked", "blocking_reason": "Execution prerequisite unavailable."}
        elif phase.startswith("validation-repair-"):
            (Path(cwd) / "candidate.txt").write_text("resolved using verified operator evidence\n", encoding="utf-8")
            final = completed_result(["candidate.txt"], "resolved")
        else:
            final = {"verdict": "pass", "summary": "verified", "acceptance_criteria": [], "binding_requirements": [], "findings": []}
        return run_result(Path(artifact_dir), phase, final, [])


def failed_blocker(tmp_path, monkeypatch, *, repair_cycles=2):
    repo, base = initialize_repository(tmp_path)
    o, task = make_orchestrator(tmp_path, repo)
    o.config = replace(o.config, max_repair_cycles=repair_cycles)
    o.runner = BlockedWorker()
    with o.db.connect() as connection:
        connection.execute("UPDATE tasks SET attempts=3 WHERE id=?", (task.id,))
    task = o.db.get_task(task.id)
    monkeypatch.setattr(o, "_validate", lambda *_, **__: passing_validation(tmp_path, True))
    o._execute_task_body(task)
    task = o.db.get_task(task.id)
    assert task.state == TaskState.FAILED
    assert task.attempts == task.max_attempts == 3
    receipt = tmp_path / "operator-receipt.json"
    write_receipt(o, task, receipt)
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    return o, task, receipt, digest


def write_receipt(o, task, receipt, *, legacy=False):
    project = o.db.get_project(task.project_id)
    base = o.db.execution_checkpoint(task.id)["base_commit"]
    output = o.config.evidence_root / project.name / task.id / f"attempt-{task.attempts:02d}" / "implementation" / "implementation.final.json"
    proof = {"task_id": task.id, "attempt": task.attempts, "candidate_base": base,
             "candidate_snapshot_sha256": candidate_snapshot(o.config, project, task, base),
             "implementation_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
             "postintegration_gate": "pending"}
    if legacy:
        proof["legacy_attestation"] = "Supervisor attests this complete legacy candidate; no historical full-tree seal exists."
    receipt.write_text(json.dumps(proof))


def resolve(o, task, receipt, digest):
    return o.resolve_blocker(task.id, "The operator supplied verified prerequisite evidence.", receipt, digest)


def test_exhausted_blocker_continues_same_attempt_and_retains_failed_evidence(tmp_path, monkeypatch):
    o, task, receipt, digest = failed_blocker(tmp_path, monkeypatch)
    fingerprint = task_fingerprint(task)
    original = o.config.evidence_root / "final-evidence" / task.id / "attempt-03"
    original_hashes = {str(p.relative_to(original)): hashlib.sha256(p.read_bytes()).hexdigest() for p in original.rglob("*") if p.is_file()}
    result = resolve(o, task, receipt, digest)
    assert result["task_state"] == "COMPLETED"
    final = o.db.get_task(task.id)
    assert final.attempts == final.max_attempts == 3
    assert task_fingerprint(final) == fingerprint
    assert o.config.max_repair_cycles == 2
    assert o.runner.phases == ["implementation", "validation-repair-1", "review"]
    assert str(receipt) in o.runner.prompts[1]
    assert final.result["base_commit"] == o.db.execution_checkpoint(task.id)["base_commit"]
    assert final.result["admission_base_commit"] is None
    assert "attempt-03-c-" in final.result["patch"]
    assert original_hashes == {str(p.relative_to(original)): hashlib.sha256(p.read_bytes()).hexdigest() for p in original.rglob("*") if p.is_file()}
    listed = evidence_list(o.config, task.id, schema_version="1.4")
    assert any(a["artifact_kind"] == "final_result" and "attempt-03-c-" in a["display_name"] for a in listed["artifacts"])
    phases = list(o.runner.phases)
    assert resolve(o, final, receipt, digest)["state"] == "TERMINAL"
    assert o.runner.phases == phases


def test_resolution_never_exposes_ready_attempt_three_to_legacy_claims(tmp_path, monkeypatch):
    o, task, receipt, digest = failed_blocker(tmp_path, monkeypatch)
    with controller_lock(o.config.data_root / "controller.lock"):
        queued = queue_resolution(o.config, task.id, "resolved", receipt, digest)
    assert queued["state"] == "QUEUED"
    assert o.db.get_task(task.id).state == TaskState.BLOCKED
    assert o.db.claim_ready_tasks(1) == []
    assert o.db.get_task(task.id).attempts == 3


@pytest.mark.parametrize("invalid", ["no_allowance", "generic_error", "malformed", "completed_output", "wrong_receipt", "completed_task", "repair_already_started", "below_ceiling", "ambiguous_run", "orphan_slot"])
def test_ineligible_resolutions_are_refused_without_model_calls(tmp_path, monkeypatch, invalid):
    o, task, receipt, digest = failed_blocker(tmp_path, monkeypatch, repair_cycles=0 if invalid == "no_allowance" else 2)
    with o.db.connect() as connection:
        latest = connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    output = Path(latest["artifact_dir"]) / "implementation.final.json"
    if invalid == "generic_error":
        with o.db.connect() as connection:
            connection.execute("UPDATE runs SET exit_code=1 WHERE id=?", (latest["id"],))
    elif invalid == "malformed":
        output.write_text("{invalid")
    elif invalid == "completed_output":
        output.write_text(json.dumps(completed_result([], "not blocked")))
    elif invalid == "wrong_receipt":
        digest = "0" * 64
    elif invalid == "completed_task":
        o.db.update_task(task.id, state=TaskState.COMPLETED)
    elif invalid == "repair_already_started":
        o.db.record_run_start(task.id, "validation-repair-1", "fake", "high", Path(latest["artifact_dir"]).parent / "validation-repair-1")
    elif invalid == "below_ceiling":
        with o.db.connect() as connection:
            connection.execute("UPDATE tasks SET max_attempts=4 WHERE id=?", (task.id,))
    elif invalid == "ambiguous_run":
        run_id = o.db.record_run_start(task.id, "implementation", "fake", "high", Path(latest["artifact_dir"]))
        o.db.record_run_end(run_id, thread_id=None, exit_code=0, usage={})
    elif invalid == "orphan_slot":
        (Path(latest["artifact_dir"]).parent / "validation-repair-1").mkdir()
    with pytest.raises((ValueError, CandidateStateUnavailable)):
        resolve(o, task, receipt, digest)
    assert o.runner.phases == ["implementation"]
    assert o.db.get_task(task.id).attempts == 3


@pytest.mark.parametrize("changed", ["candidate", "receipt", "sealed_receipt", "historical_output", "base", "specification"])
def test_sealed_resolution_rejects_changed_proof(tmp_path, monkeypatch, changed):
    o, task, receipt, digest = failed_blocker(tmp_path, monkeypatch)
    reason = "The operator supplied verified prerequisite evidence."
    with controller_lock(o.config.data_root / "controller.lock"):
        queue_resolution(o.config, task.id, reason, receipt, digest)
    row = o.db.blocker_continuation(task.id, 3)
    doc = json.loads(row["document_json"])
    if changed == "candidate":
        (task.worktree_path / "base.txt").write_text("modified after sealing")
    elif changed == "receipt":
        receipt.write_text("changed")
    elif changed == "sealed_receipt":
        (Path(doc["artifact_root"]) / "operator-receipt.raw").write_text("changed")
    elif changed == "historical_output":
        Path(doc["implementation_path"]).write_text(json.dumps(completed_result([], "changed")))
    elif changed == "base":
        with o.db.connect() as connection:
            connection.execute("UPDATE execution_checkpoints SET base_commit='missing' WHERE task_id=?", (task.id,))
    else:
        with o.db.connect() as connection:
            connection.execute("UPDATE tasks SET objective='changed' WHERE id=?", (task.id,))
    with pytest.raises((ValueError, CandidateStateUnavailable)):
        resolve(o, task, receipt, digest)
    assert o.runner.phases == ["implementation"]
    assert o.db.get_task(task.id).attempts == 3


def test_unstarted_restart_reuses_reservation_but_consumed_slot_never_replays(tmp_path, monkeypatch):
    o, task, receipt, digest = failed_blocker(tmp_path, monkeypatch)
    reason = "The operator supplied verified prerequisite evidence."
    with controller_lock(o.config.data_root / "controller.lock"):
        queue_resolution(o.config, task.id, reason, receipt, digest)
        o.db.claim_blocker_continuation(task.id, 3)
    o.db.recover_interrupted_tasks()
    restarted = Orchestrator(o.config, Path(__file__).parents[1])
    restarted.runner = o.runner
    monkeypatch.setattr(restarted, "_validate", lambda *_, **__: passing_validation(tmp_path, True))
    assert resolve(restarted, task, receipt, digest)["task_state"] == "COMPLETED"
    assert restarted.db.get_task(task.id).attempts == 3


def test_interrupted_consumed_slot_is_not_replayed(tmp_path, monkeypatch):
    o, task, receipt, digest = failed_blocker(tmp_path, monkeypatch)
    reason = "The operator supplied verified prerequisite evidence."
    with controller_lock(o.config.data_root / "controller.lock"):
        queue_resolution(o.config, task.id, reason, receipt, digest)
        o.db.claim_blocker_continuation(task.id, 3)
        doc = json.loads(o.db.blocker_continuation(task.id, 3)["document_json"])
        o.db.record_run_start(task.id, "validation-repair-1", "fake", "high", Path(doc["artifact_root"]) / "validation-repair-1")
    o.db.recover_interrupted_tasks()
    with pytest.raises(ValueError):
        resolve(o, task, receipt, digest)
    assert o.runner.phases == ["implementation"]
    assert o.db.get_task(task.id).attempts == 3


def test_continuation_uses_only_the_remaining_validation_repair_budget(tmp_path, monkeypatch):
    o, task, receipt, digest = failed_blocker(tmp_path, monkeypatch)
    monkeypatch.setattr(o, "_validate", lambda *_, **__: passing_validation(tmp_path, False))
    result = resolve(o, task, receipt, digest)
    assert result["task_state"] == "FAILED"
    assert o.runner.phases == ["implementation", "validation-repair-1", "validation-repair-2"]
    assert o.db.get_task(task.id).attempts == 3
    assert resolve(o, task, receipt, digest)["state"] == "TERMINAL"
    assert len(o.runner.phases) == 3


def test_running_controller_prevents_resolution(tmp_path, monkeypatch):
    o, task, receipt, digest = failed_blocker(tmp_path, monkeypatch)
    with controller_lock(o.config.data_root / "controller.lock"):
        with pytest.raises(ControllerAlreadyRunning):
            resolve(o, task, receipt, digest)
    assert o.db.blocker_continuation(task.id, 3) is None


def test_frozen_phase_resumes_integrates_then_runs_actual_postintegration_gate(tmp_path, monkeypatch):
    from test_project_workflows import setup, install

    o, project, first, _second, definition = setup(tmp_path, monkeypatch)
    o.config = replace(o.config, max_repair_cycles=2, max_identical_failures=3)
    worker = BlockedWorker()
    o.runner = worker
    install(o, project, definition)
    for _ in range(4):
        o.run_once()
    task = o.db.get_task(first.id)
    assert task.state == TaskState.FAILED and task.attempts == 3
    assert o.workflows.get(project.id)["state"] == "BLOCKED"
    before = o.workflows.get(project.id)["specifications"]
    receipt = tmp_path / "receipt.json"
    write_receipt(o, task, receipt)
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert resolve(o, task, receipt, digest)["task_state"] == "COMPLETED"
    plan = o.workflows.get(project.id)
    assert plan["specifications"] == before
    assert plan["phase_index"] == 1 and plan["gates"][0]["status"] == "PASSED"
    assert plan["gates"][0]["commit"] == o.db.get_task(task.id).result["commit"]
    assert worker.phases == ["implementation", "implementation", "implementation", "validation-repair-1", "review"]


def legacy_blocker(tmp_path, monkeypatch):
    o, task, receipt, _ = failed_blocker(tmp_path, monkeypatch)
    original = o.config.evidence_root / "final-evidence" / task.id / "attempt-03" / "implementation"
    (original / "blocked-candidate.json").unlink()  # Model the pre-feature runtime, only in this fixture.
    with o.db.connect() as connection:
        connection.execute("UPDATE tasks SET scope='docs/evidence/legacy*' WHERE id=?", (task.id,))
    task = o.db.get_task(task.id)
    root = task.worktree_path / "docs/evidence/legacy"
    root.mkdir(parents=True)
    inputs = {"base.txt": hashlib.sha256((task.worktree_path / "base.txt").read_bytes()).hexdigest()}
    manifest = root / "source-hashes.json"
    manifest.write_text(json.dumps([{"path": name, "sha256": digest} for name, digest in inputs.items()]))
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    base = o.db.execution_checkpoint(task.id)["base_commit"]
    validation = root / "results.json"
    validation.write_text(json.dumps({"stage": "candidate", "comparison_base": base, "head": base,
                                     "branch": task.branch, "integration_ref_at_start": base, "source_manifest_sha256": manifest_sha,
                                     "results": [{"command": "controlled-validation", "exit_code": 0, "status": "passed", "log": "01.log"}]}))
    (root / "01.log").write_text("Previously recorded validation passed.")
    write_receipt(o, task, receipt, legacy=True)
    proof = json.loads(receipt.read_bytes())
    proof["legacy_proof"] = {"allowed_change_root": "docs/evidence/legacy", "application_inputs": inputs,
                             "historical_input_manifest": {"path": str(manifest), "sha256": manifest_sha},
                             "historical_validation_record": {"path": str(validation), "sha256": hashlib.sha256(validation.read_bytes()).hexdigest()}}
    receipt.write_text(json.dumps(proof))
    return o, task, receipt, hashlib.sha256(receipt.read_bytes()).hexdigest()


def test_legacy_attestation_is_new_repair_input_not_a_fabricated_historical_snapshot(tmp_path, monkeypatch):
    o, task, receipt, digest = legacy_blocker(tmp_path, monkeypatch)
    result = resolve(o, task, receipt, digest)
    assert result["task_state"] == "COMPLETED"
    assert o.db.get_task(task.id).attempts == 3
    old = o.config.evidence_root / "final-evidence" / task.id / "attempt-03" / "implementation"
    assert not (old / "blocked-candidate.json").exists()
    assert o.runner.phases == ["implementation", "validation-repair-1", "review"]


@pytest.mark.parametrize("change", ["source_before_seal", "docs_before_seal", "no_attestation", "no_legacy_proof", "old_manifest", "old_log_missing", "old_checks"])
def test_legacy_provenance_and_preseal_candidate_are_fail_closed(tmp_path, monkeypatch, change):
    o, task, receipt, digest = legacy_blocker(tmp_path, monkeypatch)
    proof = json.loads(receipt.read_bytes())
    if change == "source_before_seal":
        (task.worktree_path / "base.txt").write_text("changed before resolver")
    elif change == "docs_before_seal":
        (task.worktree_path / "docs/evidence/legacy/01.log").write_text("changed before resolver")
    elif change == "no_attestation":
        del proof["legacy_attestation"]
    elif change == "no_legacy_proof":
        del proof["legacy_proof"]
    elif change == "old_manifest":
        proof["legacy_proof"]["historical_input_manifest"]["sha256"] = "0" * 64
    elif change == "old_log_missing":
        (task.worktree_path / "docs/evidence/legacy/01.log").unlink()
    else:
        record = Path(proof["legacy_proof"]["historical_validation_record"]["path"])
        data = json.loads(record.read_bytes())
        data["results"][0]["command"] = "different command"
        record.write_text(json.dumps(data))
        proof["legacy_proof"]["historical_validation_record"]["sha256"] = hashlib.sha256(record.read_bytes()).hexdigest()
        # Even a fresh operator snapshot cannot change the frozen validation command set.
        proof["candidate_snapshot_sha256"] = candidate_snapshot(o.config, o.db.get_project(task.project_id), task, proof["candidate_base"])
    receipt.write_text(json.dumps(proof))
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    with pytest.raises((ValueError, CandidateStateUnavailable)):
        resolve(o, task, receipt, digest)
    assert o.runner.phases == ["implementation"]
    assert o.db.blocker_continuation(task.id, 3) is None


def test_controller_seal_rejects_changes_even_when_operator_attests_new_bytes(tmp_path, monkeypatch):
    o, task, receipt, _ = failed_blocker(tmp_path, monkeypatch)
    (task.worktree_path / "base.txt").write_text("not the failed candidate")
    write_receipt(o, task, receipt)
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="controller-sealed"):
        resolve(o, task, receipt, digest)
    assert o.runner.phases == ["implementation"]


def test_sealed_completion_recovers_after_project_ref_and_journal_advance(tmp_path, monkeypatch):
    from test_project_workflows import setup, install

    o, project, first, _second, definition = setup(tmp_path, monkeypatch)
    o.config = replace(o.config, max_repair_cycles=2, max_identical_failures=3)
    o.runner = BlockedWorker()
    install(o, project, definition)
    for _ in range(4):
        o.run_once()
    task = o.db.get_task(first.id)
    receipt = tmp_path / "receipt.json"
    write_receipt(o, task, receipt)
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    original = o.workflows.integrate

    def interrupted_after_journal(*args):
        original(*args)
        raise OSError("interrupted after durable integration head")

    monkeypatch.setattr(o.workflows, "integrate", interrupted_after_journal)
    assert resolve(o, task, receipt, digest)["task_state"] == "FAILED"
    saved = o.workflows.get(project.id)
    assert saved["pending"] and saved["head_commit"] == saved["pending"]["candidate_commit"]
    assert o.db.blocker_continuation(task.id, 3)["state"] == "CONSUMED"
    phases = list(o.runner.phases)
    monkeypatch.setattr(o.workflows, "integrate", original)
    assert resolve(o, task, receipt, digest)["task_state"] == "COMPLETED"
    assert o.runner.phases == phases
    assert o.db.get_task(task.id).attempts == o.db.get_task(task.id).max_attempts == 3
    assert o.workflows.get(project.id)["gates"][0]["status"] == "PASSED"


def test_complete_snapshot_rejects_mutation_of_an_already_dirty_earlier_file(tmp_path, monkeypatch):
    from codex_forge import blockers

    o, task, _receipt, _digest = failed_blocker(tmp_path, monkeypatch)
    worktree = task.worktree_path
    (worktree / "base.txt").write_text("dirty before scan")
    (worktree / "z.txt").write_text("later file")
    original = blockers._plain_document

    def concurrent_edit(path, **kwargs):
        result = original(path, **kwargs)
        if path.name == "z.txt":
            (worktree / "base.txt").write_text("dirty after scan")
        return result

    monkeypatch.setattr(blockers, "_plain_document", concurrent_edit)
    with pytest.raises(CandidateStateUnavailable, match="complete candidate scan"):
        candidate_snapshot(o.config, o.db.get_project(task.project_id), task, o.db.execution_checkpoint(task.id)["base_commit"])


def test_cli_json_reports_accepted_operation_separately_from_failed_task(tmp_path, monkeypatch, capsys):
    from codex_forge import cli

    o, task, receipt, digest = failed_blocker(tmp_path, monkeypatch)
    monkeypatch.setattr(o, "_validate", lambda *_, **__: passing_validation(tmp_path, False))
    monkeypatch.setattr(cli, "Orchestrator", lambda *_: o)
    args = SimpleNamespace(task_id=task.id, reason="The operator supplied verified prerequisite evidence.",
                           receipt=receipt, receipt_sha256=digest, json=True)
    assert cli.cmd_task_resolve_blocker(o.config, args, "1.0") == 0
    response = json.loads(capsys.readouterr().out)
    assert response["schema_version"] == "1.0"
    assert response["success"] is True and response["error"] is None
    assert response["data"]["task_state"] == "FAILED"
