"""Architecture decisions and constraints are binding; tests are evidence, not authority.

Extension of the 2026-08-17 acceptance-conformance regression to the rest of the specification
contract: the approved specification carried `Supported levels are low, medium, and high.` as an
architecture decision, the candidate implemented info|warning|critical, its self-authored tests
encoded the wrong values and passed, and the reviewer's verdict said `pass`. Completion must
require the independent review to confirm every architecture decision and constraint — the
binding requirements — exactly as written, alongside every acceptance criterion. A binding
requirement reported unmet, or never reported against, blocks completion and is handed to repair
as a finding naming the requirement verbatim.

This file also pins the canonical Shared Chat -> Forge boundary-contract fixture by SHA-256, so
the same contract can be mirrored byte-identically in the Shared Chat repository.

**No test in this file makes a model request.** Reviews and implementations are canned finals.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from test_review_infrastructure import (
    COMPLETED_IMPLEMENTATION_FINAL,
    INSPECTED_REVIEW_EVENTS,
    build_orchestrator,
)
from test_specification_conformance import (
    NONCONFORMING_IMPLEMENTATION_FINAL,
    never_completed,
    record_prompts,
)

from codex_forge.cli import build_parser, main
from codex_forge.config import load_config
from codex_forge.db import ForgeDB
from codex_forge.models import Project, Task, TaskState
from codex_forge.orchestrator import (
    binding_conformance_findings,
    enforce_specification_conformance,
)
from codex_forge.prompts import implementation_prompt, review_prompt
from codex_forge.task_submission import build_task_request, task_fingerprint
from codex_forge.telemetry import OUTCOME_COMPLETED, OUTCOME_REVIEW_CHANGES_REQUIRED


# --------------------------------------------------------------------------------------------
# Fixture contract, shaped exactly like the blocked factory run
# --------------------------------------------------------------------------------------------

LEVELS_DECISION = "Supported levels are low, medium, and high."
STORAGE_DECISION = "Signals persist in a single SQLite database file."
STDLIB_CONSTRAINT = "Python standard library only; no new runtime dependencies."
SCOPE_TEXT = "The signal-shelf command-line interface and its SQLite persistence."
OUT_OF_SCOPE_TEXT = "No web UI, no network transport, and no changes outside signal_shelf/."
BINDING_REQUIREMENTS = [LEVELS_DECISION, STORAGE_DECISION, STDLIB_CONSTRAINT]

# A reviewer that compared honestly but let the green candidate tests outweigh the contract.
OVERWEIGHTED_BINDING_PASS_FINAL = {
    "verdict": "pass",
    "summary": "All candidate tests pass.",
    "acceptance_criteria": [],
    "binding_requirements": [
        {
            "text": LEVELS_DECISION,
            "met": False,
            "evidence": "cli.py accepts info|warning|critical; --level low exits 2.",
        },
        {"text": STORAGE_DECISION, "met": True, "evidence": "One signals.db file is used."},
        {"text": STDLIB_CONSTRAINT, "met": True, "evidence": "No new dependency was declared."},
    ],
    "findings": [],
}

# A blind reviewer: schema-valid pass whose binding examination is empty. (The transport's
# strict output schema forces the key to exist; declining to examine anything is the shape that
# can still arrive.)
BLIND_BINDING_PASS_FINAL = {
    "verdict": "pass",
    "summary": "No blocking defects found.",
    "acceptance_criteria": [],
    "binding_requirements": [],
    "findings": [],
}

CONFORMING_BINDING_PASS_FINAL = {
    "verdict": "pass",
    "summary": "Every binding requirement verified against the diff.",
    "acceptance_criteria": [],
    "binding_requirements": [
        {"text": item, "met": True, "evidence": "Verified against the final candidate."}
        for item in BINDING_REQUIREMENTS
    ],
    "findings": [],
}


def make_task(
    *,
    decisions: list[str] | None = None,
    constraints: list[str] | None = None,
    scope: str = "",
    out_of_scope: str = "",
    acceptance: list[str] | None = None,
) -> Task:
    return Task(
        id="TASK-BINDING",
        project_id="project-1",
        title="Signal Shelf CLI",
        objective="Record operational signals with add/list/search/stats.",
        acceptance=list(acceptance or []),
        validation_commands=[],
        state=TaskState.REVIEWING,
        architecture_decisions=list(decisions or []),
        constraints=list(constraints or []),
        scope=scope,
        out_of_scope=out_of_scope,
    )


# --------------------------------------------------------------------------------------------
# Part 1 - the pipeline regression fixture
# --------------------------------------------------------------------------------------------


def test_green_candidate_tests_cannot_override_an_unmet_architecture_decision(
    tmp_path: Path, monkeypatch
):
    """The blocker fixture: the decision names low|medium|high, the candidate ships
    info|warning|critical, its own tests are green, and the reviewer is inclined to pass.

    The controller must treat the review as `changes_required`, never mark the task COMPLETED,
    name the unmet architecture decision in a finding, and hand that exact text to repair.
    """
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (NONCONFORMING_IMPLEMENTATION_FINAL, []),
            "review": (OVERWEIGHTED_BINDING_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-2": (OVERWEIGHTED_BINDING_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-3": (OVERWEIGHTED_BINDING_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
        },
    )
    task.architecture_decisions = [LEVELS_DECISION, STORAGE_DECISION]
    task.constraints = [STDLIB_CONSTRAINT]
    prompts = record_prompts(runner)

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_CHANGES_REQUIRED
    assert never_completed(db)
    # Repair ran and received the finding that names the unmet decision verbatim.
    assert "review-repair-1" in runner.phases
    assert "Unmet binding requirement" in prompts["review-repair-1"]
    assert LEVELS_DECISION in prompts["review-repair-1"]
    # The terminal error retains the decision, so the operator sees the contract that failed.
    assert LEVELS_DECISION in (db.last_error or "")


def test_green_tests_cannot_override_a_contradictory_conformance_finding(
    tmp_path: Path, monkeypatch
):
    """The bijection blocker: the reviewer echoes the decision exactly, met on the strength of
    the candidate's green tests, while the same report carries a contradictory paraphrased
    second version of the decision, unmet. Filtering the report to exact matches would let the
    met=true echo complete the task; the effective verdict must instead be decided after every
    conformance finding is known, and no test evidence can restore `pass`."""
    contradictory_extra_final = {
        "verdict": "pass",
        "summary": "All candidate tests pass.",
        "acceptance_criteria": [],
        "binding_requirements": [
            {"text": LEVELS_DECISION, "met": True, "evidence": "All candidate tests pass."},
            {
                "text": "The supported levels are low/medium/high.",
                "met": False,
                "evidence": "cli.py accepts info|warning|critical; --level low exits 2.",
            },
        ],
        "findings": [],
    }
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (NONCONFORMING_IMPLEMENTATION_FINAL, []),
            "review": (contradictory_extra_final, INSPECTED_REVIEW_EVENTS),
            "review-2": (contradictory_extra_final, INSPECTED_REVIEW_EVENTS),
            "review-3": (contradictory_extra_final, INSPECTED_REVIEW_EVENTS),
        },
    )
    task.architecture_decisions = [LEVELS_DECISION]
    prompts = record_prompts(runner)

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_CHANGES_REQUIRED
    assert never_completed(db)
    # Repair received the rejected extra entry by its exact reported text.
    assert "review-repair-1" in runner.phases
    assert "does not match any contract item" in prompts["review-repair-1"]
    assert "The supported levels are low/medium/high." in prompts["review-repair-1"]


def test_a_pass_that_never_reports_binding_requirements_fails_closed(tmp_path: Path, monkeypatch):
    """For a task WITH binding requirements, a review without `binding_requirements` has not
    reviewed the contract; the controller refuses the pass rather than assuming conformance."""
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (NONCONFORMING_IMPLEMENTATION_FINAL, []),
            "review": (BLIND_BINDING_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-2": (BLIND_BINDING_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-3": (BLIND_BINDING_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
        },
    )
    task.architecture_decisions = [LEVELS_DECISION]
    prompts = record_prompts(runner)

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_CHANGES_REQUIRED
    assert never_completed(db)
    assert "review-repair-1" in runner.phases
    assert "Binding requirement not verified by review" in prompts["review-repair-1"]
    assert LEVELS_DECISION in prompts["review-repair-1"]


def test_a_conforming_candidate_with_green_tests_still_completes(tmp_path: Path, monkeypatch):
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (CONFORMING_BINDING_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
        },
    )
    task.architecture_decisions = [LEVELS_DECISION, STORAGE_DECISION]
    task.constraints = [STDLIB_CONSTRAINT]
    task.scope = SCOPE_TEXT
    task.out_of_scope = OUT_OF_SCOPE_TEXT
    prompts = record_prompts(runner)

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_COMPLETED
    assert db.state == TaskState.COMPLETED
    assert runner.phases == ["implementation", "review"]
    # The reviewer's per-requirement confirmation is retained untouched in the terminal evidence.
    completed = [fields for _, fields in db.updates if fields.get("state") == TaskState.COMPLETED]
    assert completed[-1]["result"]["review"] == CONFORMING_BINDING_PASS_FINAL
    # The whole contract reached both workers verbatim through the real controller path.
    for text in (*BINDING_REQUIREMENTS, SCOPE_TEXT, OUT_OF_SCOPE_TEXT):
        assert text in prompts["implementation"]
        assert text in prompts["review"]


# --------------------------------------------------------------------------------------------
# Part 2 - the binding-requirement gate itself
# --------------------------------------------------------------------------------------------


def test_an_unmet_binding_requirement_forces_changes_required_and_names_it():
    effective = enforce_specification_conformance(
        make_task(decisions=[LEVELS_DECISION, STORAGE_DECISION], constraints=[STDLIB_CONSTRAINT]),
        OVERWEIGHTED_BINDING_PASS_FINAL,
    )
    assert effective["verdict"] == "changes_required"
    issues = [finding["issue"] for finding in effective["findings"]]
    assert any(LEVELS_DECISION in issue and "Unmet binding requirement" in issue for issue in issues)
    # The reviewer's own evidence for the nonconformance travels with the finding.
    assert any("--level low exits 2" in issue for issue in issues)
    unmet = [finding for finding in effective["findings"] if "Unmet" in finding["issue"]]
    assert all(finding["severity"] == "critical" for finding in unmet)


def test_the_gate_enforces_the_union_of_decisions_and_constraints():
    """A report that covers every decision but omits a constraint has not reviewed the contract."""
    decisions_only = {
        "verdict": "pass",
        "summary": "Decisions verified.",
        "acceptance_criteria": [],
        "binding_requirements": [
            {"text": LEVELS_DECISION, "met": True, "evidence": "Checked."},
            {"text": STORAGE_DECISION, "met": True, "evidence": "Checked."},
        ],
        "findings": [],
    }
    effective = enforce_specification_conformance(
        make_task(decisions=[LEVELS_DECISION, STORAGE_DECISION], constraints=[STDLIB_CONSTRAINT]),
        decisions_only,
    )
    assert effective["verdict"] == "changes_required"
    assert any(
        STDLIB_CONSTRAINT in finding["issue"] and "not verified" in finding["issue"]
        for finding in effective["findings"]
    )


def test_a_paraphrased_binding_echo_does_not_count_as_coverage():
    """Summarizing a requirement away is refused: only the exact text matches the contract."""
    paraphrased = {
        "verdict": "pass",
        "summary": "Levels validated.",
        "acceptance_criteria": [],
        "binding_requirements": [{"text": "level values are validated", "met": True, "evidence": "Checked."}],
        "findings": [],
    }
    effective = enforce_specification_conformance(make_task(decisions=[LEVELS_DECISION]), paraphrased)
    assert effective["verdict"] == "changes_required"
    assert any(
        LEVELS_DECISION in finding["issue"] and "not verified" in finding["issue"]
        for finding in effective["findings"]
    )


def test_a_whitespace_altered_echo_is_not_coverage():
    """Verbatim means verbatim: a rewrapped echo is no longer the stored requirement text."""
    wrapped_echo = {
        "verdict": "pass",
        "summary": "Verified.",
        "acceptance_criteria": [],
        "binding_requirements": [
            {
                "text": LEVELS_DECISION.replace(", ", ",\n  "),
                "met": True,
                "evidence": "Checked add with each level.",
            }
        ],
        "findings": [],
    }
    task = make_task(decisions=[LEVELS_DECISION])
    effective = enforce_specification_conformance(task, wrapped_echo)
    assert effective["verdict"] == "changes_required"
    assert binding_conformance_findings(task, wrapped_echo) != []
    assert any(
        LEVELS_DECISION in finding["issue"] and "not verified" in finding["issue"]
        for finding in effective["findings"]
    )


def test_missing_binding_requirements_fail_closed_when_the_task_has_requirements():
    effective = enforce_specification_conformance(
        make_task(decisions=[LEVELS_DECISION]), BLIND_BINDING_PASS_FINAL
    )
    assert effective["verdict"] == "changes_required"
    assert any(LEVELS_DECISION in finding["issue"] for finding in effective["findings"])


def test_a_task_without_binding_requirements_keeps_the_reviewer_verdict():
    """Backward compatibility: absent or empty `binding_requirements` stays a pass-through."""
    task = make_task()
    assert enforce_specification_conformance(task, BLIND_BINDING_PASS_FINAL) == BLIND_BINDING_PASS_FINAL
    assert binding_conformance_findings(task, BLIND_BINDING_PASS_FINAL) == []
    with_empty = {**BLIND_BINDING_PASS_FINAL, "binding_requirements": []}
    assert enforce_specification_conformance(task, with_empty) == with_empty


def test_acceptance_criteria_and_binding_requirements_are_enforced_together():
    """One choke point: an unexamined criterion and an unexamined decision each become findings."""
    criterion = "stats by_level always contains exactly the keys low, medium, and high."
    effective = enforce_specification_conformance(
        make_task(decisions=[LEVELS_DECISION], acceptance=[criterion]), BLIND_BINDING_PASS_FINAL
    )
    assert effective["verdict"] == "changes_required"
    issues = [finding["issue"] for finding in effective["findings"]]
    assert any(criterion in issue and "Acceptance criterion" in issue for issue in issues)
    assert any(LEVELS_DECISION in issue and "Binding requirement" in issue for issue in issues)


# --------------------------------------------------------------------------------------------
# Part 3 - the contract renders verbatim in both prompts, and never as bogus sections
# --------------------------------------------------------------------------------------------


def _contract_task() -> Task:
    return make_task(
        decisions=[LEVELS_DECISION, STORAGE_DECISION],
        constraints=[STDLIB_CONSTRAINT],
        scope=SCOPE_TEXT,
        out_of_scope=OUT_OF_SCOPE_TEXT,
        acceptance=["The existing test suite passes."],
    )


def test_the_full_contract_appears_verbatim_in_the_implementation_prompt(tmp_path: Path):
    project = Project(id="project-1", name="signal-shelf", repo_path=tmp_path)
    prompt = implementation_prompt(project, _contract_task(), tmp_path, [])
    for text in (*BINDING_REQUIREMENTS, SCOPE_TEXT, OUT_OF_SCOPE_TEXT):
        assert text in prompt
    # The hierarchy is stated: boundaries, binding requirements, evidence.
    assert "ARCHITECTURE DECISIONS (binding)" in prompt
    assert "CONSTRAINTS (binding)" in prompt
    assert "binding implementation requirements" in prompt
    assert "Tests are evidence for this contract; tests do not redefine it." in prompt


def test_the_full_contract_appears_verbatim_in_the_review_prompt(tmp_path: Path):
    project = Project(id="project-1", name="signal-shelf", repo_path=tmp_path)
    prompt = review_prompt(project, _contract_task(), tmp_path)
    for text in (*BINDING_REQUIREMENTS, SCOPE_TEXT, OUT_OF_SCOPE_TEXT):
        assert text in prompt
    # The reviewer is told the exact reporting shape the controller will enforce.
    assert "BINDING REQUIREMENT CONFORMANCE" in prompt
    assert "`binding_requirements`" in prompt
    assert "copied verbatim" in prompt


def test_a_task_without_contract_fields_renders_no_bogus_sections(tmp_path: Path):
    """Old tasks predate these fields; their prompts must read exactly as they always did."""
    project = Project(id="project-1", name="signal-shelf", repo_path=tmp_path)
    task = make_task(acceptance=["The existing test suite passes."])
    for prompt in (implementation_prompt(project, task, tmp_path, []), review_prompt(project, task, tmp_path)):
        assert "TASK CONTRACT" not in prompt
        assert "SCOPE" not in prompt
        assert "OUT OF SCOPE" not in prompt
        assert "ARCHITECTURE DECISIONS" not in prompt
        assert "CONSTRAINTS" not in prompt
    assert "BINDING REQUIREMENT CONFORMANCE" not in review_prompt(project, task, tmp_path)
    assert "binding_requirements" not in review_prompt(project, task, tmp_path)


# --------------------------------------------------------------------------------------------
# Part 4 - contract fields survive submission, persistence, identity, and old databases
# --------------------------------------------------------------------------------------------


def _configured(tmp_path: Path):
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
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    db = ForgeDB(config.database_path)
    db.initialize()
    project = Project(
        id="project-binding",
        name="signal-shelf",
        repo_path=tmp_path / "repositories" / "signal-shelf",
    )
    db.add_project(project)
    return config_path, db, project


CONTRACT_ARGV = [
    "task",
    "add",
    "--project",
    "signal-shelf",
    "--title",
    "Signal Shelf CLI",
    "--objective",
    "Record operational signals with add/list/search/stats.",
    "--scope",
    SCOPE_TEXT,
    "--out-of-scope",
    OUT_OF_SCOPE_TEXT,
    "--decision",
    LEVELS_DECISION,
    "--decision",
    STORAGE_DECISION,
    "--constraint",
    STDLIB_CONSTRAINT,
    "--acceptance",
    "The existing test suite passes.",
    "--id",
    "SC-R1-BINDING",
    "--json",
]


def test_contract_fields_survive_submission_persistence_and_both_prompts(tmp_path: Path, capsys):
    """End to end through the real submission entry point Shared Chat uses."""
    config_path, db, project = _configured(tmp_path)

    exit_code = main(["--config", str(config_path), *CONTRACT_ARGV])
    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0

    # The response echoes the resolved contract, so Shared Chat can verify what Forge holds.
    normalized = document["data"]["normalized_request"]
    assert normalized["architecture_decisions"] == [LEVELS_DECISION, STORAGE_DECISION]
    assert normalized["constraints"] == [STDLIB_CONSTRAINT]
    assert normalized["scope"] == SCOPE_TEXT
    assert normalized["out_of_scope"] == OUT_OF_SCOPE_TEXT
    payload = document["data"]["task"]
    assert payload["architecture_decisions"] == [LEVELS_DECISION, STORAGE_DECISION]
    assert payload["constraints"] == [STDLIB_CONSTRAINT]
    assert payload["scope"] == SCOPE_TEXT
    assert payload["out_of_scope"] == OUT_OF_SCOPE_TEXT

    # The persisted roundtrip preserves the exact text.
    task = db.get_task("SC-R1-BINDING")
    assert task is not None
    assert task.architecture_decisions == [LEVELS_DECISION, STORAGE_DECISION]
    assert task.constraints == [STDLIB_CONSTRAINT]
    assert task.scope == SCOPE_TEXT
    assert task.out_of_scope == OUT_OF_SCOPE_TEXT

    # Prompts built from the persisted task carry the contract verbatim, never summarized.
    for prompt in (
        implementation_prompt(project, task, tmp_path / "worktree", []),
        review_prompt(project, task, tmp_path / "worktree"),
    ):
        for text in (*BINDING_REQUIREMENTS, SCOPE_TEXT, OUT_OF_SCOPE_TEXT):
            assert text in prompt


def test_an_identical_resubmission_with_contract_fields_is_idempotent(tmp_path: Path, capsys):
    config_path, db, _ = _configured(tmp_path)

    first_code = main(["--config", str(config_path), *CONTRACT_ARGV])
    first = json.loads(capsys.readouterr().out)
    second_code = main(["--config", str(config_path), *CONTRACT_ARGV])
    second = json.loads(capsys.readouterr().out)

    assert (first_code, second_code) == (0, 0)
    assert first["data"]["created"] is True
    assert second["data"]["created"] is False
    assert len(db.list_tasks()) == 1


def test_changing_any_binding_field_is_a_conflict(tmp_path: Path, capsys):
    """The task fingerprint covers the binding fields, so one task identity can never silently
    represent two different binding contracts."""
    config_path, db, _ = _configured(tmp_path)
    main(["--config", str(config_path), *CONTRACT_ARGV])
    capsys.readouterr()

    for changed in (
        ["--decision", "A different decision."],
        ["--constraint", "A different constraint."],
        ["--scope", "A different scope."],
        ["--out-of-scope", "A different boundary."],
    ):
        exit_code = main(["--config", str(config_path), *CONTRACT_ARGV, *changed])
        document = json.loads(capsys.readouterr().out)
        assert exit_code == 2, changed
        assert document["error"]["code"] == "TASK_ID_CONFLICT", changed
    assert len(db.list_tasks()) == 1


def test_the_fingerprint_changes_when_any_binding_field_changes():
    def build(**overrides) -> Task:
        base = dict(
            decisions=[LEVELS_DECISION],
            constraints=[STDLIB_CONSTRAINT],
            scope=SCOPE_TEXT,
            out_of_scope=OUT_OF_SCOPE_TEXT,
        )
        base.update(overrides)
        return make_task(**base)

    baseline = task_fingerprint(build())
    assert task_fingerprint(build()) == baseline
    assert task_fingerprint(build(decisions=[STORAGE_DECISION])) != baseline
    assert task_fingerprint(build(constraints=[])) != baseline
    assert task_fingerprint(build(scope="Another scope.")) != baseline
    assert task_fingerprint(build(out_of_scope="Another boundary.")) != baseline


# The tasks table exactly as it existed before the contract columns, so the migration and the
# unmigrated read path are both proven against the real historical shape.
OLD_TASKS_TABLE = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    risk TEXT NOT NULL DEFAULT 'medium',
    tags_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    branch TEXT,
    worktree_path TEXT,
    last_error TEXT,
    failure_fingerprint TEXT,
    identical_failures INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def test_old_tasks_without_contract_columns_remain_readable(tmp_path: Path):
    database_path = tmp_path / "data" / "forge.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(OLD_TASKS_TABLE)
        connection.execute(
            "INSERT INTO tasks(id,project_id,title,objective,acceptance_json,validation_json,"
            "state,tags_json,dependencies_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "TASK-OLD",
                "project-1",
                "Old task",
                "An objective recorded before the contract fields existed.",
                '["The old criterion still holds."]',
                "[]",
                "READY",
                "[]",
                "[]",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    db = ForgeDB(database_path)

    # Readable before any migration ran: the columns are simply absent.
    unmigrated = db.get_task("TASK-OLD")
    assert unmigrated is not None
    assert unmigrated.architecture_decisions == []
    assert unmigrated.constraints == []
    assert unmigrated.scope == ""
    assert unmigrated.out_of_scope == ""
    assert unmigrated.acceptance == ["The old criterion still holds."]

    # The additive migration preserves the row and its defaults; no rewrite occurs.
    db.initialize()
    migrated = db.get_task("TASK-OLD")
    assert migrated is not None
    assert migrated.architecture_decisions == []
    assert migrated.constraints == []
    assert migrated.scope == ""
    assert migrated.out_of_scope == ""
    assert migrated.title == "Old task"

    # Prompts for the old task render without bogus contract sections.
    project = Project(id="project-1", name="legacy", repo_path=tmp_path)
    prompt = implementation_prompt(project, migrated, tmp_path, [])
    assert "TASK CONTRACT" not in prompt
    assert "ARCHITECTURE DECISIONS" not in prompt


# --------------------------------------------------------------------------------------------
# Part 5 - the canonical cross-repo boundary fixture
# --------------------------------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "shared-chat-task-contract.json"
FIXTURE_SHA256 = "e98baafb3befd929ba520019549b111969c81380a4eaf58e369c3945154b8f95"


def test_the_boundary_fixture_is_byte_pinned():
    """The fixture is mirrored byte-identically in Shared Chat; the pin makes drift loud."""
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_the_boundary_fixture_argv_produces_its_expected_request():
    """Forge's side of the shared contract: the fixture argv, parsed by the real CLI parser and
    normalized by the real request builder, is exactly the fixture's request representation."""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    request = build_task_request(build_parser().parse_args(fixture["expected_argv"]))
    actual = {
        "project": request.project,
        "title": request.title,
        "objective": request.objective,
        "acceptance_criteria": list(request.acceptance),
        "architecture_decisions": list(request.architecture_decisions),
        "constraints": list(request.constraints),
        "scope": request.scope,
        "out_of_scope": request.out_of_scope,
        "validation_commands": list(request.validation_commands),
        "tags": list(request.tags),
        "dependencies": list(request.dependencies),
        "risk": request.risk,
        "priority": request.priority,
        "max_attempts": request.max_attempts,
        "task_id": request.task_id,
        "defaults_applied": list(request.defaults_applied),
    }
    assert actual == fixture["expected_request"]
    # The declarative fields and the request representation describe the same contract.
    assert fixture["architecture_decisions"] == list(request.architecture_decisions)
    assert fixture["constraints"] == list(request.constraints)
    assert fixture["scope"] == request.scope
    assert fixture["out_of_scope"] == request.out_of_scope
    assert fixture["acceptance_criteria"] == list(request.acceptance)
    assert fixture["validation_commands"] == list(request.validation_commands)
