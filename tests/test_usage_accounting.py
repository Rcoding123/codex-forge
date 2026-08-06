"""Token-usage categorization tests.

The reference fixture is the persisted usage of a real failed task, `SC-R1-051c413a8fdd2ce5e0ce939d`:
two completed runs whose `turn.completed` events were recorded by Codex CLI 0.146.0. It is
reproduced here as fixture data; no test opens a production database or runs a task.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_forge.control_plane import (
    DEFAULT_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
    project_token_usage,
)
from codex_forge.db import ForgeDB
from codex_forge.models import Project, Task, TaskState
from codex_forge.orchestrator import Orchestrator, TaskUsageBudgetExceeded
from codex_forge.usage import (
    aggregate_token_usage,
    cache_hit_ratio,
    coerce_token_count,
    empty_token_usage,
    normalize_run_usage,
)


# The two persisted `turn.completed` usage records of the audited failed task.
FAILED_TASK_RUNS = (
    {
        "input_tokens": 1784907,
        "cached_input_tokens": 1707008,
        "cache_write_input_tokens": 0,
        "output_tokens": 12739,
        "reasoning_output_tokens": 2358,
    },
    {
        "input_tokens": 460226,
        "cached_input_tokens": 411392,
        "cache_write_input_tokens": 0,
        "output_tokens": 4309,
        "reasoning_output_tokens": 2353,
    },
)


def _seed_task(db: ForgeDB, tmp_path: Path, task_id: str = "TASK-usage") -> Task:
    project = Project(id="project-usage", name="usage-demo", repo_path=tmp_path / "repo")
    db.add_project(project)
    task = Task(
        id=task_id,
        project_id=project.id,
        title="Usage",
        objective="record",
        acceptance=[],
        validation_commands=[],
        state=TaskState.READY,
    )
    db.add_task(task)
    return task


def _record_runs(db: ForgeDB, task: Task, tmp_path: Path, usages) -> None:
    for index, usage in enumerate(usages):
        run_id = db.record_run_start(
            task.id, "implementation", "gpt-test", "medium", tmp_path / f"run-{index}"
        )
        db.record_run_end(run_id, thread_id=f"thread-{index}", exit_code=0, usage=usage)


def test_failed_task_fixture_reports_audited_categories():
    usage = aggregate_token_usage(FAILED_TASK_RUNS)

    assert usage["input_tokens"] == 2245133
    assert usage["cached_input_tokens"] == 2118400
    assert usage["fresh_input_tokens"] == 126733
    assert usage["output_tokens"] == 17048
    assert usage["raw_total_tokens"] == 2262181
    assert usage["total_tokens"] == 2262181
    assert usage["accounting_basis"] == "reported_token_categories"
    assert usage["budget_basis"] == "raw_input_plus_output"


def test_failed_task_fixture_aggregates_from_persisted_rows(tmp_path: Path):
    """The same figures come out of persisted `usage_json`, with no normalized columns involved."""
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, FAILED_TASK_RUNS)

    usage = db.task_token_usage(task.id)

    assert usage == aggregate_token_usage(FAILED_TASK_RUNS)
    assert usage["total_tokens"] == 2262181
    assert usage["cache_write_input_tokens"] == 0
    assert usage["reasoning_output_tokens"] == 4711


def test_reasoning_output_is_reported_beside_output_and_never_added():
    """Codex reports reasoning as a subset of output, so it stays out of every total."""
    usage = aggregate_token_usage(FAILED_TASK_RUNS)

    assert usage["reasoning_output_tokens"] == 2358 + 2353
    assert usage["output_tokens"] == 17048
    assert usage["raw_total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert usage["total_tokens"] == usage["raw_total_tokens"]


def test_cache_hit_ratio_is_computed_safely():
    assert cache_hit_ratio(2118400, 2245133) == round(2118400 / 2245133, 6)
    assert aggregate_token_usage(FAILED_TASK_RUNS)["cache_hit_ratio"] == round(2118400 / 2245133, 6)
    # No completed run, no reported input, and inconsistent input can never divide by zero or leave
    # the closed unit interval.
    assert empty_token_usage()["cache_hit_ratio"] == 0.0
    assert cache_hit_ratio(10, 0) == 0.0
    assert cache_hit_ratio(10, -5) == 0.0
    assert cache_hit_ratio(500, 100) == 1.0
    assert cache_hit_ratio(0, 100) == 0.0


def test_cached_input_is_not_counted_twice():
    """Cached input is a subset of input, so it never inflates a total as a sibling category."""
    usage = aggregate_token_usage([{"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 100}])

    assert usage["raw_total_tokens"] == 1100
    assert usage["total_tokens"] == 1100
    assert usage["input_tokens"] == 1000
    assert usage["cached_input_tokens"] == 900
    assert usage["fresh_input_tokens"] == 100
    assert usage["cached_input_tokens"] + usage["fresh_input_tokens"] == usage["input_tokens"]


def test_missing_cached_usage_makes_fresh_input_equal_input():
    """A run recorded before cached input was reported is fully fresh, not fully cached."""
    usage = aggregate_token_usage([{"input_tokens": 400, "output_tokens": 25}])

    assert usage["cached_input_tokens"] == 0
    assert usage["fresh_input_tokens"] == 400
    assert usage["cache_write_input_tokens"] == 0
    assert usage["reasoning_output_tokens"] == 0
    assert usage["cache_hit_ratio"] == 0.0
    assert usage["total_tokens"] == 425


def test_cached_input_greater_than_input_cannot_go_negative():
    inconsistent = {"input_tokens": 100, "cached_input_tokens": 900, "output_tokens": 10}

    single = aggregate_token_usage([inconsistent])
    assert single["fresh_input_tokens"] == 0
    assert single["cache_hit_ratio"] == 1.0
    assert single["raw_total_tokens"] == 110

    # The clamp is per run: an inconsistent run must not subtract from a healthy run's fresh count.
    mixed = aggregate_token_usage([inconsistent, {"input_tokens": 500, "cached_input_tokens": 200}])
    assert mixed["fresh_input_tokens"] == 300


def test_negative_reported_counts_do_not_create_negative_totals():
    usage = aggregate_token_usage(
        [{"input_tokens": -50, "cached_input_tokens": -10, "output_tokens": -7, "reasoning_output_tokens": -3}]
    )

    assert usage["input_tokens"] == 0
    assert usage["cached_input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["reasoning_output_tokens"] == 0
    assert usage["fresh_input_tokens"] == 0
    assert usage["raw_total_tokens"] == 0
    assert usage["total_tokens"] == 0
    assert usage["cache_hit_ratio"] == 0.0


def test_malformed_usage_values_fail_safe():
    assert coerce_token_count(None) == 0
    assert coerce_token_count(True) == 0
    assert coerce_token_count(False) == 0
    assert coerce_token_count("not-a-number") == 0
    assert coerce_token_count("") == 0
    assert coerce_token_count([1, 2]) == 0
    assert coerce_token_count({"input_tokens": 1}) == 0
    assert coerce_token_count(math.inf) == 0
    assert coerce_token_count(math.nan) == 0
    assert coerce_token_count(-1) == 0
    # A numeric value that a worker recorded as text or as a float still counts, as it did before.
    assert coerce_token_count(" 42 ") == 42
    assert coerce_token_count(42.9) == 42


def test_malformed_usage_documents_do_not_crash_aggregation():
    usage = aggregate_token_usage(
        [
            None,
            "",
            "{not json",
            "[1, 2, 3]",
            b"\xff\xfe",
            42,
            {"input_tokens": None, "output_tokens": "oops"},
            '{"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2}',
            {"input_tokens": True, "output_tokens": 5},
        ]
    )

    assert usage["input_tokens"] == 10
    assert usage["cached_input_tokens"] == 4
    assert usage["fresh_input_tokens"] == 6
    assert usage["output_tokens"] == 7
    assert usage["raw_total_tokens"] == 17
    assert usage["total_tokens"] == 17


def test_additive_usage_fields_are_ignored_rather_than_summed():
    """An unknown member of a future usage object must not silently join a total."""
    usage = aggregate_token_usage(
        [
            {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 10,
                "total_tokens": 999_999,
                "cache_creation_input_tokens": 5_000,
                "some_future_category_tokens": 7_000,
            }
        ]
    )

    assert usage["raw_total_tokens"] == 110
    assert usage["total_tokens"] == 110
    assert set(usage) == {
        "input_tokens",
        "cached_input_tokens",
        "fresh_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "raw_total_tokens",
        "total_tokens",
        "cache_hit_ratio",
        "accounting_basis",
        "budget_basis",
    }


def test_malformed_persisted_usage_does_not_break_a_task_read(tmp_path: Path):
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, [{"input_tokens": 12, "output_tokens": 3}])
    run_id = db.record_run_start(task.id, "review", "gpt-test", "medium", tmp_path / "run-broken")
    db.record_run_end(run_id, thread_id=None, exit_code=1, usage={})
    with db.connect() as connection:
        connection.execute("UPDATE runs SET usage_json='{broken' WHERE id=?", (run_id,))

    usage = db.task_token_usage(task.id)

    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 3
    assert usage["total_tokens"] == 15


def test_incomplete_runs_are_still_excluded_from_usage(tmp_path: Path):
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    db.record_run_start(task.id, "implementation", "gpt-test", "medium", tmp_path / "run-open")

    assert db.task_token_usage(task.id) == empty_token_usage()


def test_empty_usage_reports_zero_categories_and_declared_bases():
    assert empty_token_usage() == {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "fresh_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "raw_total_tokens": 0,
        "total_tokens": 0,
        "cache_hit_ratio": 0.0,
        "accounting_basis": "reported_token_categories",
        "budget_basis": "raw_input_plus_output",
    }


def test_projection_selects_fields_without_recomputing_anything():
    """Both projections read the same aggregate; only the declared field set differs."""
    full = aggregate_token_usage(FAILED_TASK_RUNS)

    legacy = project_token_usage(full, DEFAULT_SCHEMA_VERSION)
    categorized = project_token_usage(full, LATEST_SCHEMA_VERSION)

    assert set(legacy) == {"input_tokens", "output_tokens", "total_tokens"}
    assert legacy == {"input_tokens": 2245133, "output_tokens": 17048, "total_tokens": 2262181}
    assert categorized == full
    for field in legacy:
        assert legacy[field] == categorized[field] == full[field]


def test_projection_of_an_empty_aggregate_keeps_the_legacy_zero_shape():
    assert project_token_usage(empty_token_usage(), DEFAULT_SCHEMA_VERSION) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def test_internal_aggregation_always_computes_the_full_shape(tmp_path: Path):
    """The projection is a serialization concern; persistence-level aggregation is unconditional."""
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, FAILED_TASK_RUNS)

    assert set(db.task_token_usage(task.id)) == set(aggregate_token_usage(FAILED_TASK_RUNS))
    assert db.task_token_usage(task.id)["cached_input_tokens"] == 2118400


def _orchestrator(db: ForgeDB, max_task_tokens: int) -> Orchestrator:
    instance = object.__new__(Orchestrator)
    instance.db = db
    instance.config = SimpleNamespace(max_task_tokens=max_task_tokens)
    return instance


def test_budget_still_decides_on_raw_input_plus_output(tmp_path: Path):
    """The budget compares the same raw total it compared before the categories existed.

    The audited task reported 2,262,181 raw tokens but only 126,733 fresh input tokens. If
    categorization had leaked into enforcement, this task would no longer stop at a two-million
    ceiling.
    """
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, FAILED_TASK_RUNS)
    usage = db.task_token_usage(task.id)

    with pytest.raises(TaskUsageBudgetExceeded, match="TOKEN_BUDGET_EXCEEDED: 2262181 used"):
        _orchestrator(db, 2_000_000)._enforce_usage_budget(task)

    # Exactly at the limit still stops, one token above the raw total still runs, and a disabled
    # budget still runs — the pre-existing decisions, on the pre-existing number.
    with pytest.raises(TaskUsageBudgetExceeded):
        _orchestrator(db, usage["raw_total_tokens"])._enforce_usage_budget(task)
    assert _orchestrator(db, usage["raw_total_tokens"] + 1)._enforce_usage_budget(task) is None
    assert _orchestrator(db, 0)._enforce_usage_budget(task) is None
    # A budget that only the fresh-input count would satisfy still stops the task.
    with pytest.raises(TaskUsageBudgetExceeded):
        _orchestrator(db, usage["fresh_input_tokens"] + 1)._enforce_usage_budget(task)


def test_reading_usage_does_not_change_task_or_run_state(tmp_path: Path):
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, FAILED_TASK_RUNS)
    with db.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM runs ORDER BY id")]
    stored_before = db.get_task(task.id)

    db.task_token_usage(task.id)
    db.task_token_usage(task.id)

    with db.connect() as connection:
        after = [tuple(row) for row in connection.execute("SELECT * FROM runs ORDER BY id")]
    stored_after = db.get_task(task.id)
    assert after == before
    assert stored_after == stored_before
    assert stored_after.state is TaskState.READY


def test_normalized_run_usage_reports_only_reported_and_derived_fields():
    assert normalize_run_usage(FAILED_TASK_RUNS[0]) == {
        "input_tokens": 1784907,
        "cached_input_tokens": 1707008,
        "cache_write_input_tokens": 0,
        "output_tokens": 12739,
        "reasoning_output_tokens": 2358,
        "fresh_input_tokens": 77899,
        "raw_total_tokens": 1797646,
    }
