from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from codex_forge.models import Task, TaskState
from codex_forge.orchestrator import Orchestrator, TaskUsageBudgetExceeded


class FakeDB:
    def __init__(self, usage: int = 0):
        self.updates: list[tuple[str, dict]] = []
        self.usage = usage
        self.decisions: list[tuple] = []

    def update_task(self, task_id: str, **kwargs):
        self.updates.append((task_id, kwargs))

    def task_token_usage(self, task_id: str):
        return {"input_tokens": self.usage, "output_tokens": 0, "total_tokens": self.usage}

    def add_decision(self, *args):
        self.decisions.append(args)


def make_task(**overrides) -> Task:
    task = Task(
        id="TASK-1",
        project_id="project-1",
        title="Task",
        objective="work",
        acceptance=[],
        validation_commands=[],
        state=TaskState.IMPLEMENTING,
        attempts=1,
        max_attempts=3,
    )
    return replace(task, **overrides)


def make_orchestrator(db: FakeDB | None = None) -> Orchestrator:
    instance = object.__new__(Orchestrator)
    instance.db = db or FakeDB()
    instance.config = SimpleNamespace(
        max_output_chars=12_000,
        max_identical_failures=2,
        max_task_tokens=100,
        implementation_model="balanced",
        implementation_effort="medium",
        review_model="balanced-review",
        review_effort="medium",
        escalation_model="strongest",
        escalation_effort="high",
    )
    return instance


def test_identical_failure_stops_on_second_diagnosis():
    db = FakeDB()
    orchestrator = make_orchestrator(db)
    task = make_task()
    orchestrator._retry_or_fail(task, "same deterministic failure")
    first = db.updates[-1][1]
    assert first["state"] == TaskState.READY
    repeated = replace(
        task,
        attempts=2,
        failure_fingerprint=first["failure_fingerprint"],
        identical_failures=first["identical_failures"],
    )
    orchestrator._retry_or_fail(repeated, "same   deterministic\nfailure")
    second = db.updates[-1][1]
    assert second["state"] == TaskState.FAILED
    assert second["last_error"].startswith("LOOP_DETECTED")


def test_transient_failure_retries_only_below_attempt_limit():
    db = FakeDB()
    orchestrator = make_orchestrator(db)
    orchestrator._retry_or_fail(make_task(attempts=2), "transient one")
    assert db.updates[-1][1]["state"] == TaskState.READY
    orchestrator._retry_or_fail(make_task(attempts=3), "different final failure")
    assert db.updates[-1][1]["state"] == TaskState.FAILED


def test_usage_budget_is_enforced_when_configured():
    orchestrator = make_orchestrator(FakeDB(usage=100))
    with pytest.raises(TaskUsageBudgetExceeded, match="TOKEN_BUDGET_EXCEEDED"):
        orchestrator._enforce_usage_budget(make_task())


def test_model_and_effort_routing_reflects_risk():
    orchestrator = make_orchestrator()
    normal = make_task(risk="medium")
    high = make_task(risk="high")
    assert (orchestrator._implementation_model(normal), orchestrator._implementation_effort(normal)) == (
        "balanced",
        "medium",
    )
    assert (orchestrator._review_model(normal), orchestrator._review_effort(normal)) == (
        "balanced-review",
        "medium",
    )
    assert (orchestrator._implementation_model(high), orchestrator._implementation_effort(high)) == (
        "strongest",
        "high",
    )


def test_noninferable_ambiguity_becomes_needs_decision():
    db = FakeDB()
    orchestrator = make_orchestrator(db)
    final = {
        "status": "needs_decision",
        "decisions_needed": [
            {"question": "Choose format", "options": ["A", "B"], "recommendation": "A"}
        ],
    }
    assert orchestrator._handle_noncompletion(make_task(), final)
    assert len(db.decisions) == 1
    assert db.updates[-1][1]["state"] == TaskState.NEEDS_DECISION
