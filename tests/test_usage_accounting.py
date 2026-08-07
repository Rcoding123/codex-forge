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

from codex_forge.config import TelemetryConfig
from codex_forge.control_plane import (
    DEFAULT_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
    project_token_usage,
)
from codex_forge.db import ForgeDB
from codex_forge.models import CodexRunResult, Project, Task, TaskState
from codex_forge.orchestrator import Orchestrator, TaskUsageBudgetExceeded
from codex_forge.telemetry import Telemetry
from codex_forge.usage import (
    BUDGET_BASIS,
    aggregate_token_usage,
    budget_used_tokens,
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
    assert usage["budget_basis"] == "fresh_input_plus_output"


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
        "budget_basis": "fresh_input_plus_output",
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


# The budgeted quantity of the audited task: 126,733 fresh input + 17,048 output. Its raw traffic
# was 2,262,181 tokens, nearly all of it cached input the provider had already served.
AUDITED_BUDGET_USED = 143_781


def test_budget_counts_new_work_only():
    """The budgeted quantity is fresh input plus output, and nothing else."""
    usage = aggregate_token_usage(FAILED_TASK_RUNS)

    assert budget_used_tokens(usage) == usage["fresh_input_tokens"] + usage["output_tokens"]
    assert budget_used_tokens(usage) == AUDITED_BUDGET_USED
    assert usage["budget_basis"] == BUDGET_BASIS == "fresh_input_plus_output"


def test_cached_only_usage_consumes_no_budget():
    """A run that read nothing but cache and produced no output spends no budget at all."""
    usage = aggregate_token_usage([{"input_tokens": 500_000, "cached_input_tokens": 500_000}])

    assert budget_used_tokens(usage) == 0
    # The traffic is still reported; it just does not decide anything.
    assert usage["raw_total_tokens"] == 500_000
    assert usage["cached_input_tokens"] == 500_000


@pytest.mark.parametrize("cached", [0, 1_000, 1_000_000, 50_000_000])
def test_growing_cached_input_never_consumes_more_budget(cached: int):
    """Fresh input held constant, the budget is indifferent to how much cache was re-read."""
    usage = aggregate_token_usage(
        [{"input_tokens": 400 + cached, "cached_input_tokens": cached, "output_tokens": 50}]
    )

    assert usage["fresh_input_tokens"] == 400
    assert budget_used_tokens(usage) == 450
    # Raw traffic does grow with the cached reads, which is why it cannot be the budget basis.
    assert usage["raw_total_tokens"] == 450 + cached


def test_fresh_input_and_output_each_increase_budget_used():
    baseline = aggregate_token_usage([{"input_tokens": 1_000, "cached_input_tokens": 600, "output_tokens": 100}])
    more_fresh = aggregate_token_usage([{"input_tokens": 1_700, "cached_input_tokens": 600, "output_tokens": 100}])
    more_output = aggregate_token_usage([{"input_tokens": 1_000, "cached_input_tokens": 600, "output_tokens": 350}])

    assert budget_used_tokens(baseline) == 500
    assert budget_used_tokens(more_fresh) == 500 + 700
    assert budget_used_tokens(more_output) == 500 + 250


def test_reasoning_output_is_not_added_on_top_of_output():
    """Reasoning is a subset of output. Charging it again would bill the same tokens twice."""
    with_reasoning = aggregate_token_usage(
        [{"input_tokens": 300, "cached_input_tokens": 100, "output_tokens": 200, "reasoning_output_tokens": 180}]
    )
    without_reasoning = aggregate_token_usage(
        [{"input_tokens": 300, "cached_input_tokens": 100, "output_tokens": 200}]
    )

    assert with_reasoning["reasoning_output_tokens"] == 180
    assert budget_used_tokens(with_reasoning) == 400
    assert budget_used_tokens(with_reasoning) == budget_used_tokens(without_reasoning)


def test_cache_write_input_is_not_double_counted():
    """The reported contract carries cache writes inside `input_tokens`, so fresh input covers them."""
    usage = aggregate_token_usage(
        [
            {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "cache_write_input_tokens": 800,
                "output_tokens": 10,
            }
        ]
    )

    assert usage["cache_write_input_tokens"] == 800
    assert budget_used_tokens(usage) == 810
    assert budget_used_tokens(usage) != 810 + usage["cache_write_input_tokens"]


def test_budget_used_is_additive_across_completed_runs(tmp_path: Path):
    """Aggregated enforcement equals the sum of what each completed run newly consumed."""
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, FAILED_TASK_RUNS)

    per_run = [budget_used_tokens(normalize_run_usage(run)) for run in FAILED_TASK_RUNS]

    assert per_run == [77_899 + 12_739, 48_834 + 4_309]
    assert budget_used_tokens(db.task_token_usage(task.id)) == sum(per_run) == AUDITED_BUDGET_USED


def test_budget_used_falls_back_to_reported_input_when_the_derivation_is_absent():
    """A record holding only reported fields still counts its own fresh input, never output alone."""
    assert budget_used_tokens({"input_tokens": 900, "cached_input_tokens": 400, "output_tokens": 30}) == 530
    # Malformed and empty records fail safe at zero rather than raising.
    assert budget_used_tokens({}) == 0
    assert budget_used_tokens(None) == 0
    assert budget_used_tokens("{broken") == 0
    assert budget_used_tokens('{"fresh_input_tokens": 7, "output_tokens": 3}') == 10


def test_raw_traffic_above_the_limit_does_not_stop_a_task(tmp_path: Path):
    """The audited task: 2,262,181 raw tokens, 143,781 of them new. A 2M ceiling must not trip.

    This is the correction. Before it, cached reads the provider had already served consumed the
    configured task budget, so this task was stopped for traffic rather than for work.
    """
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, FAILED_TASK_RUNS)
    usage = db.task_token_usage(task.id)

    assert usage["raw_total_tokens"] > 2_000_000
    assert _orchestrator(db, 2_000_000)._enforce_usage_budget(task) is None
    # A limit above the new-token count but far below the raw total still runs.
    assert _orchestrator(db, AUDITED_BUDGET_USED + 1)._enforce_usage_budget(task) is None
    assert _orchestrator(db, 0)._enforce_usage_budget(task) is None


def test_budget_trips_exactly_at_the_configured_new_token_threshold(tmp_path: Path):
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, FAILED_TASK_RUNS)
    used = budget_used_tokens(db.task_token_usage(task.id))

    assert used == AUDITED_BUDGET_USED
    assert _orchestrator(db, used + 1)._enforce_usage_budget(task) is None
    with pytest.raises(TaskUsageBudgetExceeded):
        _orchestrator(db, used)._enforce_usage_budget(task)
    with pytest.raises(TaskUsageBudgetExceeded):
        _orchestrator(db, used - 1)._enforce_usage_budget(task)


def test_budget_error_names_the_enforced_quantity_and_the_traffic_separately(tmp_path: Path):
    """The message must not label raw traffic as budget used."""
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, FAILED_TASK_RUNS)

    with pytest.raises(TaskUsageBudgetExceeded) as excinfo:
        _orchestrator(db, 100_000)._enforce_usage_budget(task)

    message = str(excinfo.value)
    assert message.startswith(f"TOKEN_BUDGET_EXCEEDED: {AUDITED_BUDGET_USED} new tokens used of 100000")
    assert "fresh_input_plus_output" in message
    assert "raw traffic 2262181 tokens" in message
    assert "cached input 2118400 tokens not budgeted" in message
    assert task.id in message


class _PhaseDB:
    """A database stub whose reported usage changes once the completed run has been recorded."""

    def __init__(self, before, after):
        self.before = before
        self.after = after
        self.started: list[str] = []
        self.ended: list[str] = []

    def record_run_start(self, task_id, phase, model, effort, phase_dir):
        self.started.append(phase)
        return f"run-{len(self.started)}"

    def record_run_end(self, run_id, *, thread_id, exit_code, usage):
        self.ended.append(run_id)

    def task_token_usage(self, task_id: str):
        return aggregate_token_usage(self.after if self.ended else self.before)


def _phase_orchestrator(db: _PhaseDB, max_task_tokens: int, tmp_path: Path) -> Orchestrator:
    telemetry = Telemetry(TelemetryConfig(enabled=False))
    telemetry.start()
    instance = object.__new__(Orchestrator)
    instance.db = db
    instance.config = SimpleNamespace(max_task_tokens=max_task_tokens)
    instance.telemetry = telemetry
    instance.runner = SimpleNamespace(
        run=lambda **kwargs: CodexRunResult(
            exit_code=0,
            final={"status": "completed"},
            jsonl_path=tmp_path / "events.jsonl",
            final_path=tmp_path / "final.json",
            stderr_path=tmp_path / "stderr.log",
            thread_id="thread-1",
            usage=dict(db.after[-1]),
        )
    )
    return instance


def _run_phase(orchestrator: Orchestrator, tmp_path: Path):
    task = Task(
        id="TASK-phase",
        project_id="project-usage",
        title="Usage",
        objective="record",
        acceptance=[],
        validation_commands=[],
        state=TaskState.IMPLEMENTING,
    )
    project = Project(id="project-usage", name="usage-demo", repo_path=tmp_path / "repo")
    return orchestrator._run_codex_phase(
        task,
        project,
        tmp_path / "artifacts",
        phase="implementation",
        model="gpt-test",
        effort="medium",
        prompt="do the work",
        schema="implementation.schema.json",
    )


def test_pre_phase_check_uses_the_new_basis(tmp_path: Path):
    """The check before a phase starts refuses on new tokens, not on raw traffic."""
    # Raw traffic of 2,262,181 against a 2M limit: budgeted work is 143,781, so the phase runs.
    permissive = _PhaseDB(FAILED_TASK_RUNS, FAILED_TASK_RUNS)
    assert _run_phase(_phase_orchestrator(permissive, 2_000_000, tmp_path), tmp_path).exit_code == 0
    assert permissive.started == ["implementation"]

    # Spent on new work, the same check refuses before any run is recorded.
    exhausted = _PhaseDB(FAILED_TASK_RUNS, FAILED_TASK_RUNS)
    with pytest.raises(TaskUsageBudgetExceeded, match=f"{AUDITED_BUDGET_USED} new tokens used"):
        _run_phase(_phase_orchestrator(exhausted, AUDITED_BUDGET_USED, tmp_path), tmp_path)
    assert exhausted.started == []
    assert exhausted.ended == []


def test_post_phase_check_uses_the_new_basis(tmp_path: Path):
    """The check after a completed phase is recorded also enforces new tokens only."""
    cheap_start = ({"input_tokens": 10, "output_tokens": 5},)

    # The recorded phase moved 2.2M raw tokens but only 143,781 new ones: the task continues.
    traffic_only = _PhaseDB(cheap_start, FAILED_TASK_RUNS)
    assert _run_phase(_phase_orchestrator(traffic_only, 2_000_000, tmp_path), tmp_path).exit_code == 0
    assert traffic_only.ended == ["run-1"]

    # Once the recorded phase has spent the new-token budget, the post-phase check stops the task.
    spent = _PhaseDB(cheap_start, FAILED_TASK_RUNS)
    with pytest.raises(TaskUsageBudgetExceeded, match=f"{AUDITED_BUDGET_USED} new tokens used"):
        _run_phase(_phase_orchestrator(spent, AUDITED_BUDGET_USED, tmp_path), tmp_path)
    assert spent.started == ["implementation"]
    assert spent.ended == ["run-1"]


def test_raw_and_compatibility_totals_keep_their_traffic_semantics(tmp_path: Path):
    """Making the budget count new work must not restate either raw total."""
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    task = _seed_task(db, tmp_path)
    _record_runs(db, task, tmp_path, FAILED_TASK_RUNS)
    usage = db.task_token_usage(task.id)

    assert usage["raw_total_tokens"] == usage["input_tokens"] + usage["output_tokens"] == 2_262_181
    assert usage["total_tokens"] == usage["raw_total_tokens"]
    assert usage["total_tokens"] != budget_used_tokens(usage)
    # Schema 1.0 still serves the raw traffic total it always served.
    assert project_token_usage(usage, DEFAULT_SCHEMA_VERSION)["total_tokens"] == 2_262_181


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
