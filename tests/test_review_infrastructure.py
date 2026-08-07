"""A review that could not run is an environment fault, never a finding about the candidate.

Live canary SC-R3-d4230e61405ec080be73e9ef implemented `is_blank` correctly, passed
`python -m unittest discover -s tests -v` on every attempt, and produced a two-file diff. All three
independent review phases nevertheless reported `changes_required`, because every command the
reviewer attempted — down to `pwd` — exited `-1073741502`.

`-1073741502` is `0xC0000142`, `STATUS_DLL_INIT_FAILED`: the Windows loader aborted the image before
it reached its own entry point. Codex enforces the `read-only` and `workspace-write` sandboxes on
Windows with a restricted token, and a restricted token cannot reach the non-interactive window
station a service-style logon owns, so `USER32.dll` initialization fails. Every executable a reviewer
needs — the shell, `git`, `python` — links `USER32.dll`; `cmd.exe` and `HOSTNAME.EXE`, which do not,
launch fine. Implementation and repair phases run under the configured `danger-full-access` sandbox,
which uses no restricted token, so they executed commands normally in the very same process.

Two defects follow, and this file covers both: the review phase must not be handed a sandbox that
cannot launch a command, and a review that inspected nothing must never be read as `changes_required`
or be answered with a repair model call.

**No test in this file makes a model request.** Every Codex invocation is a recorder script that
writes a canned result and exits.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_forge.config import ForgeConfig, TelemetryConfig
from codex_forge.control_plane import _safe_task_error
from codex_forge.models import CodexRunResult, Project, Task, TaskState, ValidationResult
from codex_forge.orchestrator import (
    REVIEW_INFRASTRUCTURE_BLOCK_FILENAME,
    REVIEW_INFRASTRUCTURE_BLOCKED,
    REVIEW_SANDBOX,
    Orchestrator,
)
from codex_forge.runner import (
    BLOCK_COMMANDS_NEVER_EXECUTED,
    BLOCK_SANDBOX_UNLAUNCHABLE,
    STATUS_DLL_INIT_FAILED,
    CodexRunner,
    SandboxResolution,
    _build_process_command,
    _worker_environment,
    detect_review_infrastructure_block,
    is_process_launch_failure,
    normalize_exit_code,
    sandbox_infrastructure_block,
)
from codex_forge.telemetry import (
    ERROR_REVIEW_INFRASTRUCTURE_BLOCKED,
    OUTCOME_REVIEW_CHANGES_REQUIRED,
    OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED,
    TERMINATED_BY_REVIEW_INFRASTRUCTURE,
    ERROR_CODE_BY_OUTCOME,
    TASK_OUTCOMES,
    TERMINATED_BY_OUTCOME,
    build_telemetry,
)
from codex_forge.usage import BUDGET_BASIS, aggregate_token_usage, budget_used_tokens


# --------------------------------------------------------------------------------------------
# Event-stream fixtures, shaped exactly like the retained canary evidence
# --------------------------------------------------------------------------------------------

CANARY_REVIEW_COMMAND = (
    '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -Command pwd'
)


def _command_event(command: str, exit_code: int | None) -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": command,
                "aggregated_output": "",
                "exit_code": exit_code,
                "status": "failed" if exit_code else "completed",
            },
        }
    )


def write_events(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_result(directory: Path, phase: str, final: dict, events: list[str]) -> CodexRunResult:
    directory.mkdir(parents=True, exist_ok=True)
    jsonl = write_events(directory / f"{phase}.events.jsonl", events)
    final_path = directory / f"{phase}.final.json"
    final_path.write_text(json.dumps(final), encoding="utf-8")
    stderr_path = directory / f"{phase}.stderr.log"
    stderr_path.write_text("", encoding="utf-8")
    return CodexRunResult(
        exit_code=0,
        final=final,
        jsonl_path=jsonl,
        final_path=final_path,
        stderr_path=stderr_path,
        thread_id="thread-1",
        usage={"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10},
    )


BLOCKED_REVIEW_EVENTS = [
    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
    _command_event(CANARY_REVIEW_COMMAND, STATUS_DLL_INIT_FAILED),
    _command_event(CANARY_REVIEW_COMMAND, STATUS_DLL_INIT_FAILED),
    _command_event(CANARY_REVIEW_COMMAND, STATUS_DLL_INIT_FAILED),
]
INSPECTED_REVIEW_EVENTS = [
    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
    _command_event("git diff master...HEAD", 0),
]

# The verdict the canary reviewer actually returned once its commands died.
CANARY_BLOCKED_FINAL = {
    "verdict": "changes_required",
    "summary": "Review could not be completed because the workspace command runner fails.",
    "findings": [
        {
            "severity": "high",
            "file": None,
            "line": None,
            "issue": "The diff could not be inspected due to the execution environment failure.",
            "required_fix": "Restore the workspace command runner, then rerun the review.",
        }
    ],
}
GENUINE_CHANGES_REQUIRED_FINAL = {
    "verdict": "changes_required",
    "summary": "`is_blank` mishandles a non-breaking space.",
    "findings": [
        {
            "severity": "medium",
            "file": "src/utils.py",
            "line": 12,
            "issue": "U+00A0 is not stripped.",
            "required_fix": "Normalize unicode whitespace before stripping.",
        }
    ],
}
PASSING_REVIEW_FINAL = {"verdict": "pass", "summary": "Looks correct.", "findings": []}
COMPLETED_IMPLEMENTATION_FINAL = {
    "status": "completed",
    "summary": "Added `is_blank`.",
    "changed_files": ["src/utils.py", "tests/test_utils.py"],
    "validation_performed": ["unittest: 2 passed"],
    "assumptions": [],
    "blocking_reason": None,
    "decisions_needed": [],
    "reusable_patterns": [],
}


# --------------------------------------------------------------------------------------------
# Part 1 - the Windows process-launch defect
# --------------------------------------------------------------------------------------------


def make_config(tmp_path: Path, **overrides) -> ForgeConfig:
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
        sandbox="danger-full-access",
    )
    return replace(config, **overrides)


def fake_codex(tmp_path: Path, body: str) -> Path:
    """A stand-in Codex whose `sandbox` subcommand behaviour the test dictates."""
    script = tmp_path / "fake-codex.py"
    script.write_text(body, encoding="utf-8")
    return script


SANDBOX_REFUSES_TO_LAUNCH = f"""
import sys
if sys.argv[1] == 'sandbox':
    # The restricted token aborts the child image before it runs; nothing reaches stdout.
    sys.exit({STATUS_DLL_INIT_FAILED})
sys.exit(0)
"""

SANDBOX_LAUNCHES = """
import subprocess, sys
if sys.argv[1] == 'sandbox':
    sys.exit(subprocess.run(sys.argv[3:]).returncode)
sys.exit(0)
"""

SANDBOX_SUBCOMMAND_MISSING = """
import sys
sys.stderr.write("error: unrecognized subcommand 'sandbox'\\n")
sys.exit(2)
"""


def test_the_windows_launch_failure_codes_are_recognized_as_never_having_run():
    assert is_process_launch_failure(STATUS_DLL_INIT_FAILED)
    assert STATUS_DLL_INIT_FAILED == -1073741502 == -(0x100000000 - 0xC0000142)
    # A command that ran and failed is a different fact and must never be confused with one that
    # could not start.
    assert not is_process_launch_failure(1)
    assert not is_process_launch_failure(0)
    assert not is_process_launch_failure(None)


def test_the_same_failure_is_recognized_in_both_windows_representations():
    """Codex reports the status signed; Python's `subprocess` reports the raw unsigned DWORD.

    `0xC0000142` reaches the controller as `-1073741502` through the reviewer's event stream and as
    `3221225794` through a `subprocess` return code. Recognizing only one of them would let the
    identical fault through on whichever path was not covered.
    """
    unsigned = 0xC0000142
    assert unsigned == 3221225794
    assert normalize_exit_code(unsigned) == STATUS_DLL_INIT_FAILED
    assert is_process_launch_failure(unsigned)
    assert is_process_launch_failure(STATUS_DLL_INIT_FAILED)
    # Ordinary exit codes are never folded.
    assert normalize_exit_code(1) == 1
    assert normalize_exit_code(0) == 0
    assert normalize_exit_code(None) is None


def test_a_blocked_review_is_detected_from_the_unsigned_representation(tmp_path: Path):
    events = [_command_event(CANARY_REVIEW_COMMAND, 0xC0000142)]
    result = run_result(tmp_path / "review", "review", CANARY_BLOCKED_FINAL, events)
    block = detect_review_infrastructure_block(
        result, requested_sandbox="read-only", sandbox="read-only"
    )
    assert block is not None
    # Recorded in the documented signed form regardless of how it arrived.
    assert block.exit_codes == (STATUS_DLL_INIT_FAILED,)


def test_the_review_launch_path_executes_a_trivial_read_only_command_in_the_worktree(tmp_path: Path):
    """The review runner's own construction and environment must run a read-only command.

    This is the check the canary never got to make: same `_build_process_command`, same
    `_worker_environment`, same worktree cwd that the implementation phase used.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    command = _build_process_command(sys.executable, ["-c", "import os; print(os.getcwd())"])
    completed = subprocess.run(
        command,
        cwd=str(worktree),
        env=_worker_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert not is_process_launch_failure(completed.returncode)
    assert Path(completed.stdout.strip()).resolve() == worktree.resolve()


def test_a_sandbox_that_cannot_launch_a_process_is_not_selected_for_review(tmp_path: Path):
    """The modeled canary host: the restricted-token sandbox aborts every image it starts."""
    config = make_config(
        tmp_path, codex_executable=str(fake_codex(tmp_path, SANDBOX_REFUSES_TO_LAUNCH))
    )
    runner = CodexRunner(config, tmp_path / "schemas")
    probe = runner.probe_sandbox_launch(cwd=tmp_path)
    assert probe.exit_code == STATUS_DLL_INIT_FAILED
    assert not probe.launchable

    resolution = runner.resolve_sandbox(REVIEW_SANDBOX, cwd=tmp_path)
    assert resolution.requested == "read-only"
    assert resolution.sandbox == "danger-full-access"
    assert resolution.degraded


def test_the_resolved_review_sandbox_does_not_reproduce_the_dll_init_failure(tmp_path: Path):
    """Running under the resolved mode launches the process the requested mode could not."""
    config = make_config(
        tmp_path, codex_executable=str(fake_codex(tmp_path, SANDBOX_REFUSES_TO_LAUNCH))
    )
    runner = CodexRunner(config, tmp_path / "schemas")
    resolution = runner.resolve_sandbox(REVIEW_SANDBOX, cwd=tmp_path)

    # The requested mode is the broken one; the resolved mode launches commands directly, which is
    # exactly what the implementation phase of the canary did successfully.
    assert resolution.probe is not None and resolution.probe.exit_code == STATUS_DLL_INIT_FAILED
    completed = subprocess.run(
        _build_process_command(sys.executable, ["-c", "print('ok')"]),
        cwd=str(tmp_path),
        env=_worker_environment(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0
    assert not is_process_launch_failure(completed.returncode)


def test_a_launchable_sandbox_keeps_the_requested_read_only_mode(tmp_path: Path):
    config = make_config(tmp_path, codex_executable=str(fake_codex(tmp_path, SANDBOX_LAUNCHES)))
    runner = CodexRunner(config, tmp_path / "schemas")
    resolution = runner.resolve_sandbox(REVIEW_SANDBOX, cwd=tmp_path)
    assert resolution.sandbox == "read-only"
    assert not resolution.degraded


def test_an_inconclusive_probe_never_downgrades_the_requested_sandbox(tmp_path: Path):
    """A Codex build without a `sandbox` subcommand says nothing about launchability."""
    config = make_config(
        tmp_path, codex_executable=str(fake_codex(tmp_path, SANDBOX_SUBCOMMAND_MISSING))
    )
    runner = CodexRunner(config, tmp_path / "schemas")
    probe = runner.probe_sandbox_launch(cwd=tmp_path)
    assert probe.exit_code == 2
    assert probe.launchable
    assert runner.resolve_sandbox(REVIEW_SANDBOX, cwd=tmp_path).sandbox == "read-only"


def test_an_unsandboxed_mode_is_returned_without_probing(tmp_path: Path, monkeypatch):
    config = make_config(tmp_path)
    runner = CodexRunner(config, tmp_path / "schemas")
    monkeypatch.setattr(
        runner, "probe_sandbox_launch", lambda **_: pytest.fail("must not probe an open sandbox")
    )
    resolution = runner.resolve_sandbox("danger-full-access", cwd=tmp_path)
    assert resolution.sandbox == "danger-full-access"
    assert resolution.probe is None


def test_no_launchable_sandbox_reports_a_block_rather_than_an_unusable_runner(tmp_path: Path):
    """A host that only permits restricted sandboxes has no reviewer at all; say so."""
    config = make_config(
        tmp_path,
        sandbox="read-only",
        codex_executable=str(fake_codex(tmp_path, SANDBOX_REFUSES_TO_LAUNCH)),
    )
    runner = CodexRunner(config, tmp_path / "schemas")
    resolution = runner.resolve_sandbox(REVIEW_SANDBOX, cwd=tmp_path)
    assert resolution.sandbox is None

    block = sandbox_infrastructure_block(resolution)
    assert block.reason == BLOCK_SANDBOX_UNLAUNCHABLE
    assert block.exit_codes == (STATUS_DLL_INIT_FAILED,)


def test_the_sandbox_probe_runs_once_per_runner(tmp_path: Path):
    """The probe launches a process; a per-phase probe would be a per-phase cost."""
    marker = tmp_path / "probe-count"
    body = f"""
import sys, pathlib
p = pathlib.Path(r"{marker}")
p.write_text(str(int(p.read_text()) + 1) if p.exists() else "1")
sys.exit({STATUS_DLL_INIT_FAILED})
"""
    config = make_config(tmp_path, codex_executable=str(fake_codex(tmp_path, body)))
    runner = CodexRunner(config, tmp_path / "schemas")
    for _ in range(3):
        runner.resolve_sandbox(REVIEW_SANDBOX, cwd=tmp_path)
    assert marker.read_text() == "1"


# --------------------------------------------------------------------------------------------
# Part 2 - reading a blocked review from its own evidence
# --------------------------------------------------------------------------------------------


def test_a_review_whose_every_command_died_in_launch_is_infrastructure_blocked(tmp_path: Path):
    result = run_result(tmp_path / "review", "review", CANARY_BLOCKED_FINAL, BLOCKED_REVIEW_EVENTS)
    block = detect_review_infrastructure_block(
        result, requested_sandbox="read-only", sandbox="read-only"
    )
    assert block is not None
    assert block.reason == BLOCK_COMMANDS_NEVER_EXECUTED
    assert block.exit_codes == (STATUS_DLL_INIT_FAILED,)
    # The concrete failing command and the retained evidence both survive into the record.
    assert block.commands[0] == CANARY_REVIEW_COMMAND
    assert block.evidence["events"] == str(result.jsonl_path)
    assert block.evidence["stderr"] == str(result.stderr_path)


def test_a_review_that_executed_any_command_keeps_its_own_verdict(tmp_path: Path):
    """One command that actually ran means the reviewer could inspect; do not override it."""
    events = INSPECTED_REVIEW_EVENTS + [_command_event("pytest", STATUS_DLL_INIT_FAILED)]
    result = run_result(tmp_path / "review", "review", GENUINE_CHANGES_REQUIRED_FINAL, events)
    assert (
        detect_review_infrastructure_block(
            result, requested_sandbox="read-only", sandbox="read-only"
        )
        is None
    )


def test_a_review_whose_command_ran_and_failed_is_not_infrastructure_blocked(tmp_path: Path):
    """Exit 1 is a command that ran. Only a launch failure means nothing ran."""
    events = [_command_event("pytest", 1)]
    result = run_result(tmp_path / "review", "review", GENUINE_CHANGES_REQUIRED_FINAL, events)
    assert (
        detect_review_infrastructure_block(
            result, requested_sandbox="read-only", sandbox="read-only"
        )
        is None
    )


def test_a_healthy_review_is_not_infrastructure_blocked(tmp_path: Path):
    result = run_result(tmp_path / "review", "review", PASSING_REVIEW_FINAL, INSPECTED_REVIEW_EVENTS)
    assert (
        detect_review_infrastructure_block(
            result, requested_sandbox="read-only", sandbox="read-only"
        )
        is None
    )


# --------------------------------------------------------------------------------------------
# Part 3 - controller routing: an environment fault is not a code defect
# --------------------------------------------------------------------------------------------


class RecordingRunner:
    """A Codex stand-in that records every phase it is asked to run."""

    def __init__(self, finals: dict[str, tuple[dict, list[str]]], resolution: SandboxResolution):
        self.finals = finals
        self.resolution = resolution
        self.phases: list[str] = []
        self.sandboxes: list[str | None] = []

    def resolve_sandbox(self, requested: str, *, cwd: Path) -> SandboxResolution:
        return self.resolution

    def run(self, *, prompt, cwd, artifact_dir, phase, model, effort, schema_name, sandbox=None):
        self.phases.append(phase)
        self.sandboxes.append(sandbox)
        # An unscripted phase still returns a benign result, so a controller that runs a phase it
        # should not run is reported by the assertion on `phases` rather than by a fixture crash.
        final, events = self.finals.get(phase, (COMPLETED_IMPLEMENTATION_FINAL, []))
        return run_result(Path(artifact_dir), phase, final, events)


class RecordingDB:
    def __init__(self):
        self.updates: list[tuple[str, dict]] = []
        self.runs: list[dict] = []
        self.project = Project(id="project-1", name="canary", repo_path=Path("repo"), auto_commit=False)

    def get_project(self, project_id: str) -> Project:
        return self.project

    def update_task(self, task_id: str, **kwargs) -> None:
        self.updates.append((task_id, kwargs))

    def record_run_start(self, task_id, phase, model, effort, directory) -> int:
        self.runs.append({"phase": phase, "usage": {}})
        return len(self.runs)

    def record_run_end(self, run_id, *, thread_id, exit_code, usage) -> None:
        self.runs[run_id - 1]["usage"] = usage or {}

    def task_token_usage(self, task_id: str):
        return aggregate_token_usage([run["usage"] for run in self.runs if run["usage"]])

    def add_decision(self, *args) -> None:
        raise AssertionError("no decision is expected")

    @property
    def state(self) -> TaskState | None:
        for _, fields in reversed(self.updates):
            if "state" in fields:
                return fields["state"]
        return None

    @property
    def last_error(self) -> str | None:
        for _, fields in reversed(self.updates):
            if fields.get("last_error"):
                return fields["last_error"]
        return None


def build_orchestrator(
    tmp_path: Path,
    monkeypatch,
    *,
    finals: dict[str, tuple[dict, list[str]]],
    resolution: SandboxResolution | None = None,
    validations_pass: bool = True,
) -> tuple[Orchestrator, RecordingDB, RecordingRunner, Task]:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    resolved = resolution or SandboxResolution(
        requested=REVIEW_SANDBOX, sandbox=REVIEW_SANDBOX, probe=None
    )
    db = RecordingDB()
    runner = RecordingRunner(finals, resolved)

    orchestrator = object.__new__(Orchestrator)
    orchestrator.db = db
    orchestrator.runner = runner
    orchestrator.telemetry = build_telemetry(TelemetryConfig())
    orchestrator.knowledge = SimpleNamespace(
        relevant=lambda *args: [], add_patterns=lambda *args: None
    )
    orchestrator.config = SimpleNamespace(
        evidence_root=tmp_path / "evidence",
        workspace_root=tmp_path / "worktrees",
        sandbox="danger-full-access",
        max_repair_cycles=2,
        max_identical_failures=2,
        max_task_tokens=0,
        max_output_chars=12_000,
        task_timeout_minutes=1,
        implementation_model="impl-model",
        implementation_effort="medium",
        review_model="review-model",
        review_effort="medium",
        escalation_model="strong",
        escalation_effort="high",
    )

    task = Task(
        id="SC-R3-canary",
        project_id="project-1",
        title="Add is_blank",
        objective="Add a whitespace helper",
        acceptance=[],
        validation_commands=["python -m unittest discover -s tests -v"],
        state=TaskState.IMPLEMENTING,
        attempts=1,
        max_attempts=3,
    )

    monkeypatch.setattr(
        "codex_forge.orchestrator.prepare_worktree", lambda *args: ("task/branch", worktree)
    )
    monkeypatch.setattr("codex_forge.orchestrator.write_patch", lambda *args: None)
    monkeypatch.setattr("codex_forge.orchestrator.commit_task", lambda *args: "deadbeef")

    def validations(commands, **kwargs):
        return [
            ValidationResult(
                command=command,
                exit_code=0 if validations_pass else 1,
                passed=validations_pass,
                duration_seconds=0.1,
                stdout_path=tmp_path / "out.log",
                stderr_path=tmp_path / "err.log",
                summary="2 tests passed" if validations_pass else "failed",
            )
            for command in commands
        ]

    monkeypatch.setattr("codex_forge.orchestrator.run_validations", validations)
    return orchestrator, db, runner, task


def test_an_infrastructure_blocked_review_never_starts_a_repair_phase(tmp_path: Path, monkeypatch):
    """The token-waste invariant: no implementation model call answers an environment fault.

    The live canary ran review -> review-repair-1 -> review-2 -> review-repair-2 -> review-3 against
    code whose validation passed every time. Two of those five phases were implementation calls that
    could not have repaired a broken window station.
    """
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (CANARY_BLOCKED_FINAL, BLOCKED_REVIEW_EVENTS),
        },
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert runner.phases == ["implementation", "review"]
    assert not [phase for phase in runner.phases if "repair" in phase]
    # The blocked review's `changes_required` verdict is refused as a statement about the code.
    assert outcome != OUTCOME_REVIEW_CHANGES_REQUIRED


def test_an_infrastructure_blocked_review_fails_closed_and_is_never_completed(
    tmp_path: Path, monkeypatch
):
    """Validation passing is not a substitute for the independent review that did not happen."""
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (CANARY_BLOCKED_FINAL, BLOCKED_REVIEW_EVENTS),
        },
        validations_pass=True,
    )

    orchestrator._execute_task_body(task)

    assert db.state == TaskState.FAILED
    assert TaskState.COMPLETED not in [fields.get("state") for _, fields in db.updates]
    # Failing closed, not retrying: another attempt would re-run implementation into the same
    # unusable reviewer.
    assert TaskState.READY not in [fields.get("state") for _, fields in db.updates]


def test_the_terminal_error_is_specific_and_keeps_the_infrastructure_evidence(
    tmp_path: Path, monkeypatch
):
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (CANARY_BLOCKED_FINAL, BLOCKED_REVIEW_EVENTS),
        },
    )

    orchestrator._execute_task_body(task)

    assert db.last_error.startswith(REVIEW_INFRASTRUCTURE_BLOCKED)
    assert str(STATUS_DLL_INIT_FAILED) in db.last_error

    record_path = (
        tmp_path / "evidence" / "canary" / task.id / "attempt-01" / REVIEW_INFRASTRUCTURE_BLOCK_FILENAME
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["error_code"] == REVIEW_INFRASTRUCTURE_BLOCKED
    assert record["reason"] == BLOCK_COMMANDS_NEVER_EXECUTED
    assert record["exit_codes"] == [STATUS_DLL_INIT_FAILED]
    assert CANARY_REVIEW_COMMAND in record["failed_commands"]
    assert record["evidence"]["events"].endswith("review.events.jsonl")


def test_shared_chat_is_told_the_review_environment_failed_not_the_code():
    code, message = _safe_task_error(f"{REVIEW_INFRASTRUCTURE_BLOCKED}: pwsh could not start")
    assert code == REVIEW_INFRASTRUCTURE_BLOCKED
    assert "environment fault" in message
    assert "not a finding about the task's code" in message


def test_a_genuine_changes_required_verdict_still_enters_the_repair_path(tmp_path: Path, monkeypatch):
    """The reviewer inspected the candidate and found a real defect: repair as before."""
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (GENUINE_CHANGES_REQUIRED_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-repair-1": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review-2": (PASSING_REVIEW_FINAL, INSPECTED_REVIEW_EVENTS),
        },
    )

    outcome = orchestrator._execute_task_body(task)

    assert runner.phases == ["implementation", "review", "review-repair-1", "review-2"]
    assert outcome == "completed"
    assert db.state == TaskState.COMPLETED


def test_a_second_review_blocked_by_infrastructure_stops_the_repair_loop(tmp_path: Path, monkeypatch):
    """A runtime that dies mid-cycle is caught too, and no second repair is paid for."""
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (GENUINE_CHANGES_REQUIRED_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-repair-1": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review-2": (CANARY_BLOCKED_FINAL, BLOCKED_REVIEW_EVENTS),
        },
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert runner.phases == ["implementation", "review", "review-repair-1", "review-2"]
    assert "review-repair-2" not in runner.phases
    assert db.state == TaskState.FAILED


def test_no_review_model_call_is_made_when_no_sandbox_can_launch(tmp_path: Path, monkeypatch):
    """With nothing launchable, even the reviewer's own tokens are not spent."""
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={"implementation": (COMPLETED_IMPLEMENTATION_FINAL, [])},
        resolution=SandboxResolution(requested=REVIEW_SANDBOX, sandbox=None, probe=None),
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert runner.phases == ["implementation"]
    assert db.state == TaskState.FAILED
    record_path = (
        tmp_path / "evidence" / "canary" / task.id / "attempt-01" / REVIEW_INFRASTRUCTURE_BLOCK_FILENAME
    )
    assert json.loads(record_path.read_text(encoding="utf-8"))["reason"] == BLOCK_SANDBOX_UNLAUNCHABLE


def test_the_review_runs_under_the_resolved_sandbox(tmp_path: Path, monkeypatch):
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (PASSING_REVIEW_FINAL, INSPECTED_REVIEW_EVENTS),
        },
        resolution=SandboxResolution(
            requested=REVIEW_SANDBOX, sandbox="danger-full-access", probe=None
        ),
    )

    orchestrator._execute_task_body(task)

    # Implementation keeps the configured sandbox (passed as None, meaning "the configured one");
    # review runs under the mode that was proven able to launch a command.
    assert runner.sandboxes == [None, "danger-full-access"]


# --------------------------------------------------------------------------------------------
# Part 4 - invariants this change must not disturb
# --------------------------------------------------------------------------------------------


def test_max_attempts_semantics_are_unchanged(tmp_path: Path, monkeypatch):
    """`_retry_or_fail` still retries below the limit and fails at it, on the ordinary paths."""
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path, monkeypatch, finals={"implementation": (COMPLETED_IMPLEMENTATION_FINAL, [])}
    )
    orchestrator._retry_or_fail(replace(task, attempts=2), "a validation failure")
    assert db.updates[-1][1]["state"] == TaskState.READY
    orchestrator._retry_or_fail(replace(task, attempts=3), "a different validation failure")
    assert db.updates[-1][1]["state"] == TaskState.FAILED


def test_a_genuine_review_failure_still_retries_within_max_attempts(tmp_path: Path, monkeypatch):
    """An exhausted repair budget on a real verdict keeps the old retry behaviour."""
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (GENUINE_CHANGES_REQUIRED_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-repair-1": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review-2": (GENUINE_CHANGES_REQUIRED_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-repair-2": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review-3": (GENUINE_CHANGES_REQUIRED_FINAL, INSPECTED_REVIEW_EVENTS),
        },
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_CHANGES_REQUIRED
    # attempts (1) is below max_attempts (3), so the task goes back to READY exactly as before.
    assert db.state == TaskState.READY


def test_new_token_accounting_is_unchanged(tmp_path: Path, monkeypatch):
    """The budget basis stays fresh input plus output; cached input is still excluded."""
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (CANARY_BLOCKED_FINAL, BLOCKED_REVIEW_EVENTS),
        },
    )

    orchestrator._execute_task_body(task)

    usage = db.task_token_usage(task.id)
    # Two runs of {input 100, cached 40, output 10}: fresh input 60 each, output 10 each.
    assert usage["cached_input_tokens"] == 80
    assert usage["fresh_input_tokens"] == 120
    assert budget_used_tokens(usage) == 140
    assert BUDGET_BASIS == "fresh_input_plus_output"


def test_the_new_outcome_joins_the_bounded_telemetry_vocabulary():
    assert OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED in TASK_OUTCOMES
    assert (
        ERROR_CODE_BY_OUTCOME[OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED]
        == ERROR_REVIEW_INFRASTRUCTURE_BLOCKED
    )
    assert (
        TERMINATED_BY_OUTCOME[OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED]
        == TERMINATED_BY_REVIEW_INFRASTRUCTURE
    )
    # Distinct from the verdict-driven outcome, which is the whole point of the distinction.
    assert OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED != OUTCOME_REVIEW_CHANGES_REQUIRED
