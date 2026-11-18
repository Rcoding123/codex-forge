"""Exercise the real adapter orchestration with fake model/validation processes."""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_forge.adapter import engine
from codex_forge.adapter.process import ProcessOutcome


@pytest.fixture
def job(tmp_path):
    repo, workspace = tmp_path / "repo", tmp_path / "worker"
    repo.mkdir()
    env = os.environ.copy()
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")

    def git(*argv):
        return subprocess.run(["git", "-C", str(repo), *argv], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-b", "main")
    (repo / "source.py").write_text("answer = 42\n", encoding="utf-8")
    git("add", "source.py")
    git("-c", "user.name=Adapter Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
    git("worktree", "add", "--detach", str(workspace), "HEAD")
    return {
        "contract_version": "adapter/1", "caller_id": "tests", "job_id": "job", "attempt_id": "a1",
        "role": "implementation",
        "workspace": {"path": str(workspace), "base_revision": git("rev-parse", "HEAD"),
                      "expected_state": "clean"},
        "task": {"objective": "Verify source", "acceptance": ["Run the supplied gate"],
                 "constraints": [], "scope": ["source.py"], "out_of_scope": []},
        "policy": {"sandbox": "workspace-write", "allowed_paths": ["source.py"],
                   "tool_network": "unspecified", "search": False, "mcp": False, "plugins": False},
        "context": [], "model": "test-model", "effort": "medium", "deadline_s": 60,
        "artifact_root": str(tmp_path / "evidence"),
        "validation": {"authoritative": True, "commands": [{"argv": [sys.executable, "-c", "pass"],
                                                           "timeout_s": 5}]},
    }


def _outcome(**kwargs):
    return ProcessOutcome(**({"launched": True, "exit_code": 0, "timed_out": False, "cancelled": False,
                              "output_limit": False, "cleanup_verified": True, "error": None,
                              "duration_s": 0.01} | kwargs))


@pytest.mark.skipif(os.name == "nt", reason="POSIX evidence directory permissions")
def test_new_attempt_evidence_is_private_to_the_owner(job):
    artifact = engine._artifacts(job, Path(job["workspace"]["path"]))
    assert artifact.stat().st_mode & 0o077 == 0
    assert artifact.parent.stat().st_mode & 0o077 == 0


def wire(monkeypatch, *, semantic="done", invalid_final=False, model_edit=None,
         validation_edit=None, validation_code=0, transport=None, malformed_events=False, events_override=None):
    calls = []
    monkeypatch.setattr(engine, "inspect_policy", lambda *_args, **_kwargs: {
        "environment": {}, "auth": "chatgpt_subscription", "version": "0.151.0", "executable": "fake",
        "capabilities": {}, "config_digest": "test",
    })
    monkeypatch.setattr(engine, "build_argv", lambda _report, _request, *, schema_path, final_path:
                        ["fake", "-o", str(final_path)])

    def run(argv, **kwargs):
        calls.append(argv)
        kwargs["stdout_path"].write_text("", encoding="utf-8")
        kwargs["stderr_path"].write_text("", encoding="utf-8")
        if argv[0] == "fake":
            if model_edit:
                model_edit(kwargs["cwd"])
            final = {"status": semantic, "summary": "Done", "blockers": [], "decisions": [],
                     "assumptions": [], "patterns": []}
            Path(argv[2]).write_text("{" if invalid_final else json.dumps(final), encoding="utf-8")
            events = [{"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 5,
                                                            "output_tokens": 4, "reasoning_output_tokens": 1}}]
            if events_override is not None:
                events = events_override
            kwargs["stdout_path"].write_text("bad" if malformed_events else
                                             "".join(json.dumps(event) + "\n" for event in events),
                                             encoding="utf-8")
            return transport or _outcome()
        if validation_edit:
            validation_edit(kwargs["cwd"])
        return _outcome(exit_code=validation_code)

    monkeypatch.setattr(engine, "run_owned", run)
    return calls


def test_one_launch_external_gate_and_persisted_result(job, monkeypatch):
    calls = wire(monkeypatch, model_edit=lambda cwd: (cwd / "source.py").write_text("answer = 43\n"))
    result = engine.run_job(job)
    assert result["adapter_status"] == "completed", result
    assert result["factory_acceptance"] == "not_determined"
    assert len(calls) == 2 and calls[1] == job["validation"]["commands"][0]["argv"]
    assert result["semantic"]["is_model_claim"]
    saved = json.loads((Path(result["evidence"]["artifact_root"]) / "result.json").read_text())
    assert saved == result
    assert "environment" not in result["provenance"]


@pytest.mark.parametrize("options,status", [
    ({"invalid_final": True}, "failed_protocol"),
    ({"malformed_events": True}, "failed_protocol"),
    ({"semantic": "blocked"}, "partial"),
    ({"transport": _outcome(exit_code=1)}, "failed_transport"),
    ({"transport": _outcome(cleanup_verified=False)}, "failed_transport"),
])
def test_model_failures_never_retry_or_validate(job, monkeypatch, options, status):
    calls = wire(monkeypatch, **options)
    result = engine.run_job(job)
    assert result["adapter_status"] == status, result
    assert len(calls) == 1
    assert result["validation"]["state"] == "not_run"
    assert Path(result["evidence"]["final"]).exists()


def test_no_authoritative_gate_cannot_complete(job, monkeypatch):
    wire(monkeypatch)
    job.pop("validation")
    result = engine.run_job(job)
    assert result["adapter_status"] == "partial"
    assert result["validation"]["state"] == "not_requested"


def test_gate_failure_stops_without_repairs(job, monkeypatch):
    calls = wire(monkeypatch, validation_code=1)
    job["validation"]["commands"] *= 2
    result = engine.run_job(job)
    assert result["adapter_status"] == "validation_failed"
    assert result["validation"]["run"] == 1
    assert len(calls) == 2


def test_gate_cannot_change_source_and_claim_success(job, monkeypatch):
    wire(monkeypatch, validation_edit=lambda cwd: (cwd / "source.py").write_text("changed by test\n"))
    result = engine.run_job(job)
    assert result["adapter_status"] == "validation_failed"
    assert result["validation"]["state"] == "stale"


def test_out_of_scope_edit_is_reported_without_validation(job, monkeypatch):
    calls = wire(monkeypatch, model_edit=lambda cwd: (cwd / "extra.py").write_text("bad edit\n"))
    result = engine.run_job(job)
    assert result["adapter_status"] == "partial"
    assert result["edit_audit"]["out_of_policy"] == ["extra.py"]
    assert len(calls) == 1


def test_stale_source_no_model_calls(job, monkeypatch):
    calls = wire(monkeypatch)
    job["workspace"]["base_revision"] = "a" * 40
    result = engine.run_job(job)
    assert result["adapter_status"] == "rejected_preflight"
    assert result["error"]["code"] == "stale_source"
    assert not calls


def test_reused_attempt_preserves_existing_evidence(job, monkeypatch):
    calls = wire(monkeypatch)
    first = engine.run_job(job)
    artifact = Path(first["evidence"]["artifact_root"]) / "result.json"
    raw = artifact.read_bytes()
    second = engine.run_job(job)
    assert second["adapter_status"] == "rejected_preflight"
    assert artifact.read_bytes() == raw
    assert len(calls) == 2  # One model + one gate for the first run only.


def test_artifacts_cannot_be_inside_worktree(job, monkeypatch):
    calls = wire(monkeypatch)
    job["artifact_root"] = str(Path(job["workspace"]["path"]) / "evidence")
    result = engine.run_job(job)
    assert result["error"]["code"] == "artifact_location"
    assert not calls


def test_adapter_has_no_controller_imports():
    package = Path(engine.__file__).parent
    forbidden = {"db", "orchestrator", "task_submission", "cli", "knowledge", "telemetry", "config", "runner"}
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not (node.level > 1 and node.module.split(".")[0] in forbidden), source
                if node.module.startswith("codex_forge."):
                    assert node.module.split(".")[1] not in forbidden, source


@pytest.mark.parametrize("events", [[], [{"type": "unknown"}], [{"type": "turn.started"}],
    [{"type": "item.started", "item": {"id": "command1", "type": "command_execution"}},
     {"type": "turn.completed"}],
    [{"type": "turn.completed"}, {"type": "turn.started"}],
    [{"type": "turn.completed"},
     {"type": "item.started", "item": {"id": "command1", "type": "command_execution"}},
     {"type": "item.completed", "item": {"id": "command1", "type": "command_execution"}}],
    [{"type": "item.started", "item": {"id": "", "type": "command_execution"}},
     {"type": "item.completed", "item": {"id": "", "type": "command_execution"}},
     {"type": "turn.completed"}],
])
def test_incomplete_events_never_reach_gate(job, monkeypatch, events):
    calls = wire(monkeypatch, events_override=events)
    result = engine.run_job(job)
    assert result["adapter_status"] == "failed_protocol"
    assert len(calls) == 1


def test_completed_command_events_admitted(job, monkeypatch):
    wire(monkeypatch, events_override=[
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "command1", "type": "command_execution"}},
        {"type": "item.completed", "item": {"id": "command1", "type": "command_execution"}},
        {"type": "turn.completed"},
    ])
    assert engine.run_job(job)["adapter_status"] == "completed"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_initial_source_is_rejected(job, monkeypatch, flag):
    workspace = Path(job["workspace"]["path"])
    engine._git(workspace, "update-index", flag, "source.py")
    (workspace / "source.py").write_text("hidden difference\n")
    calls = wire(monkeypatch)
    result = engine.run_job(job)
    assert result["error"]["code"] == "unsupported_index_flags"
    assert not calls


@pytest.mark.parametrize("action", ["stage", "flag"])
def test_model_cannot_mutate_index_without_detection(job, monkeypatch, action):
    def edit(cwd):
        if action == "stage":
            (cwd / "source.py").write_text("staged difference\n")
            engine._git(cwd, "add", "source.py")
        else:
            engine._git(cwd, "update-index", "--skip-worktree", "source.py")
    calls = wire(monkeypatch, model_edit=edit)
    result = engine.run_job(job)
    assert result["adapter_status"] != "completed"
    assert result["error"]["code"] == "worktree_metadata_changed"
    assert len(calls) == 1
