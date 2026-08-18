"""Tests are evidence for the specification; tests do not redefine the specification.

Regression for the 2026-08-17 full-factory acceptance blocker: the approved specification
required `level` to be one of low|medium|high, the candidate implemented info|warning|critical,
its self-authored tests encoded the wrong values and passed, independent review returned `pass`,
final controller validation passed, and Forge marked the task COMPLETED. A direct probe against
the approved requirement (`--level low`) then failed with exit code 2.

Completion must instead require the independent review to compare the final candidate against
every task acceptance criterion exactly as written. A criterion the review reports unmet, or
never reports against, blocks completion and is handed to repair as a finding naming the
criterion — even when every candidate-authored test is green.

**No test in this file makes a model request.** Reviews and implementations are canned finals.
"""

from __future__ import annotations

from pathlib import Path

from test_review_infrastructure import (
    COMPLETED_IMPLEMENTATION_FINAL,
    INSPECTED_REVIEW_EVENTS,
    build_orchestrator,
)

from codex_forge.cli import main
from codex_forge.config import load_config
from codex_forge.db import ForgeDB
from codex_forge.models import Project, Task, TaskState
from codex_forge.orchestrator import (
    acceptance_conformance_findings,
    enforce_specification_conformance,
)
from codex_forge.prompts import review_prompt
from codex_forge.telemetry import OUTCOME_COMPLETED, OUTCOME_REVIEW_CHANGES_REQUIRED


# --------------------------------------------------------------------------------------------
# Fixture specification, shaped exactly like the blocked factory run
# --------------------------------------------------------------------------------------------

LEVEL_CRITERION = (
    "add accepts exactly the levels low, medium, and high; any other --level value, "
    "including info, warning, and critical, is rejected with exit code 2."
)
STATS_CRITERION = (
    "stats by_level always contains exactly the keys low, medium, and high, including zero counts."
)
CRITERIA = [LEVEL_CRITERION, STATS_CRITERION]

# The candidate the live run produced: wrong levels, declared as an assumption, own tests green.
NONCONFORMING_IMPLEMENTATION_FINAL = {
    "status": "completed",
    "summary": "Implemented the signal CLI with levels info, warning, critical.",
    "changed_files": ["signal_shelf/cli.py", "tests/test_cli.py"],
    "validation_performed": ["python -m pytest -q — 5 passed"],
    "assumptions": ["Supported levels are info, warning, and critical."],
    "blocking_reason": None,
    "decisions_needed": [],
    "reusable_patterns": [],
}

# A reviewer that compared honestly but let the green candidate tests outweigh the contract.
OVERWEIGHTED_PASS_FINAL = {
    "verdict": "pass",
    "summary": "All candidate tests pass.",
    "acceptance_criteria": [
        {
            "criterion": LEVEL_CRITERION,
            "met": False,
            "evidence": "cli.py accepts info|warning|critical; --level low exits 2.",
        },
        {
            "criterion": STATS_CRITERION,
            "met": False,
            "evidence": "stats emits info/warning/critical keys.",
        },
    ],
    "binding_requirements": [],
    "findings": [],
}

# A blind reviewer: schema-valid pass, nothing examined per criterion. (The live fw5a reviewer
# returned this with the criteria keys absent; the strict output schema now forces them to
# exist, so an empty examination is the blind shape that can still arrive.)
BLIND_PASS_FINAL = {
    "verdict": "pass",
    "summary": "No blocking defects found.",
    "acceptance_criteria": [],
    "binding_requirements": [],
    "findings": [],
}

CONFORMING_PASS_FINAL = {
    "verdict": "pass",
    "summary": "Every acceptance criterion verified against the diff.",
    "acceptance_criteria": [
        {"criterion": item, "met": True, "evidence": "Verified against the final candidate."}
        for item in CRITERIA
    ],
    "binding_requirements": [],
    "findings": [],
}


def record_prompts(runner) -> dict[str, str]:
    """Capture the exact prompt each phase receives, keyed by phase name."""
    prompts: dict[str, str] = {}
    original = runner.run

    def wrapped(**kwargs):
        prompts[kwargs["phase"]] = kwargs["prompt"]
        return original(**kwargs)

    runner.run = wrapped
    return prompts


def make_task(acceptance: list[str]) -> Task:
    return Task(
        id="TASK-CONFORMANCE",
        project_id="project-1",
        title="Signal Shelf CLI",
        objective="Record operational signals with add/list/search/stats.",
        acceptance=acceptance,
        validation_commands=[],
        state=TaskState.REVIEWING,
    )


def never_completed(db) -> bool:
    return all(fields.get("state") != TaskState.COMPLETED for _, fields in db.updates)


# --------------------------------------------------------------------------------------------
# Part 1 - the pipeline regression fixture
# --------------------------------------------------------------------------------------------


def test_green_candidate_tests_cannot_override_an_unmet_acceptance_criterion(
    tmp_path: Path, monkeypatch
):
    """The blocker fixture: nonconforming candidate, green tests, review inclined to pass.

    The controller must treat the review as `changes_required`, never mark the task COMPLETED,
    name the unmet criterion in a finding, and hand that finding to repair.
    """
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (NONCONFORMING_IMPLEMENTATION_FINAL, []),
            "review": (OVERWEIGHTED_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-2": (OVERWEIGHTED_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-3": (OVERWEIGHTED_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
        },
    )
    task.acceptance = list(CRITERIA)
    prompts = record_prompts(runner)

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_CHANGES_REQUIRED
    assert never_completed(db)
    # Repair ran and received the finding that names the unmet criterion verbatim.
    assert "review-repair-1" in runner.phases
    assert "Unmet acceptance criterion" in prompts["review-repair-1"]
    assert LEVEL_CRITERION in prompts["review-repair-1"]
    # The terminal error retains the criterion, so the operator sees the contract that failed.
    assert LEVEL_CRITERION in (db.last_error or "")


def test_a_pass_that_never_examined_the_criteria_cannot_complete(tmp_path: Path, monkeypatch):
    """The live reviewer's exact shape — `pass`, no findings, nothing checked per criterion.

    A review that does not account for a criterion has not reviewed the contract, so the
    controller refuses the pass and repair receives the unexamined criterion by name.
    """
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (NONCONFORMING_IMPLEMENTATION_FINAL, []),
            "review": (BLIND_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-2": (BLIND_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
            "review-3": (BLIND_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
        },
    )
    task.acceptance = list(CRITERIA)
    prompts = record_prompts(runner)

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_CHANGES_REQUIRED
    assert never_completed(db)
    assert "review-repair-1" in runner.phases
    assert "Acceptance criterion not verified by review" in prompts["review-repair-1"]
    assert LEVEL_CRITERION in prompts["review-repair-1"]


def test_a_conforming_candidate_with_green_tests_still_completes(tmp_path: Path, monkeypatch):
    orchestrator, db, runner, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (CONFORMING_PASS_FINAL, INSPECTED_REVIEW_EVENTS),
        },
    )
    task.acceptance = list(CRITERIA)
    prompts = record_prompts(runner)

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_COMPLETED
    assert db.state == TaskState.COMPLETED
    assert runner.phases == ["implementation", "review"]
    # The reviewer's per-criterion confirmation is retained untouched in the terminal evidence.
    completed = [fields for _, fields in db.updates if fields.get("state") == TaskState.COMPLETED]
    assert completed[-1]["result"]["review"] == CONFORMING_PASS_FINAL
    # Every criterion reached the reviewer verbatim, alongside the governing law.
    for criterion in CRITERIA:
        assert criterion in prompts["review"]
    assert "tests do not redefine the specification" in prompts["review"]


# --------------------------------------------------------------------------------------------
# Part 2 - the conformance gate itself
# --------------------------------------------------------------------------------------------


def test_an_unmet_criterion_forces_changes_required_and_names_the_criterion():
    effective = enforce_specification_conformance(make_task(CRITERIA), OVERWEIGHTED_PASS_FINAL)
    assert effective["verdict"] == "changes_required"
    issues = [finding["issue"] for finding in effective["findings"]]
    assert any(LEVEL_CRITERION in issue and "Unmet acceptance criterion" in issue for issue in issues)
    # The reviewer's own evidence for the nonconformance travels with the finding.
    assert any("--level low exits 2" in issue for issue in issues)
    unmet = [finding for finding in effective["findings"] if "Unmet" in finding["issue"]]
    assert all(finding["severity"] == "critical" for finding in unmet)


def test_a_paraphrased_criterion_echo_does_not_count_as_coverage():
    """Summarizing a criterion away is refused: only the exact text matches the contract."""
    paraphrased = {
        "verdict": "pass",
        "summary": "Levels validated.",
        "acceptance_criteria": [
            {"criterion": "level values are validated", "met": True, "evidence": "Checked."}
        ],
        "findings": [],
    }
    effective = enforce_specification_conformance(make_task([LEVEL_CRITERION]), paraphrased)
    assert effective["verdict"] == "changes_required"
    assert any(
        LEVEL_CRITERION in finding["issue"] and "not verified" in finding["issue"]
        for finding in effective["findings"]
    )


def test_a_whitespace_altered_echo_is_not_coverage():
    """Verbatim means verbatim: a rewrapped echo is no longer the stored criterion text."""
    wrapped_echo = {
        "verdict": "pass",
        "summary": "Verified.",
        "acceptance_criteria": [
            {
                "criterion": LEVEL_CRITERION.replace("; ", ";\n  "),
                "met": True,
                "evidence": "Checked add with each level.",
            }
        ],
        "findings": [],
    }
    effective = enforce_specification_conformance(make_task([LEVEL_CRITERION]), wrapped_echo)
    assert effective["verdict"] == "changes_required"
    assert acceptance_conformance_findings(make_task([LEVEL_CRITERION]), wrapped_echo) != []
    assert any(
        LEVEL_CRITERION in finding["issue"] and "not verified" in finding["issue"]
        for finding in effective["findings"]
    )


def test_a_fully_confirmed_review_result_passes_through_unchanged():
    effective = enforce_specification_conformance(make_task(CRITERIA), CONFORMING_PASS_FINAL)
    assert effective == CONFORMING_PASS_FINAL


def test_a_task_without_criteria_keeps_the_reviewer_verdict():
    effective = enforce_specification_conformance(make_task([]), BLIND_PASS_FINAL)
    assert effective == BLIND_PASS_FINAL


# --------------------------------------------------------------------------------------------
# Part 3 - criterion text survives the exact submission payload into the review prompt
# --------------------------------------------------------------------------------------------


def test_criterion_text_survives_the_exact_task_payload_to_the_review_prompt(
    tmp_path: Path, capsys
):
    """End to end through the real submission entry point Shared Chat uses.

    `forge task add --json` normalizes and persists the payload; the review prompt built from
    the persisted task must contain every submitted criterion verbatim, never summarized.
    """
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
        id="project-conformance",
        name="signal-shelf",
        repo_path=tmp_path / "repositories" / "signal-shelf",
    )
    db.add_project(project)

    exit_code = main(
        [
            "--config",
            str(config_path),
            "task",
            "add",
            "--project",
            "signal-shelf",
            "--title",
            "Signal Shelf CLI",
            "--objective",
            "Record operational signals with add/list/search/stats.",
            "--acceptance",
            LEVEL_CRITERION,
            "--acceptance",
            STATS_CRITERION,
            "--validation",
            "python -m pytest -q",
            "--id",
            "SC-R1-CONFORMANCE",
            "--json",
        ]
    )
    capsys.readouterr()
    assert exit_code == 0

    task = db.get_task("SC-R1-CONFORMANCE")
    assert task is not None
    assert task.acceptance == CRITERIA

    prompt = review_prompt(project, task, tmp_path / "worktree")
    for criterion in CRITERIA:
        assert criterion in prompt
    assert "Tests are evidence for the specification" in prompt
    assert "tests do not redefine the specification" in prompt
