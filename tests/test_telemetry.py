"""Behavioral tests for the OpenTelemetry foundation.

Nothing here needs a running Collector. Enabled telemetry is wired to an in-memory span exporter
through the `processor_factory` seam, so span shape, parentage, attributes, and closure are read
back directly.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_forge import telemetry as telemetry_module
from codex_forge.config import TelemetryConfig, load_config
from codex_forge.models import Task, TaskState
from codex_forge.orchestrator import Orchestrator, TaskUsageBudgetExceeded
from codex_forge.telemetry import (
    AGENT_ROLES,
    ATTEMPT_KINDS,
    ATTR_ACCOUNTING_BASIS,
    ATTR_AGENT_ROLE,
    ATTR_ATTEMPT_KIND,
    ATTR_ATTEMPT_NUMBER,
    ATTR_ATTEMPT_OUTCOME,
    ATTR_ATTEMPT_TERMINATED_BY,
    ATTR_BUDGET_BASIS,
    ATTR_CACHE_HIT_RATIO,
    ATTR_CACHED_INPUT_TOKENS,
    ATTR_ERROR_CODE,
    ATTR_ERROR_TYPE,
    ATTR_FRESH_INPUT_TOKENS,
    ATTR_INPUT_TOKENS,
    ATTR_OUTPUT_TOKENS,
    ATTR_PHASE_CYCLE,
    ATTR_PHASE_DURATION_MS,
    ATTR_PHASE_NAME,
    ATTR_PHASE_OUTCOME,
    ATTR_RAW_TOTAL_TOKENS,
    ATTR_REASONING_TOKENS,
    ATTR_RETRY_BUCKET,
    ATTR_TASK_ID,
    ATTR_TASK_OUTCOME,
    ATTR_TASK_RISK,
    ERROR_CODES,
    ERROR_CODEX_EXIT_NONZERO,
    ERROR_TOKEN_BUDGET_EXCEEDED,
    LOW_CARDINALITY_ATTRIBUTES,
    OUTCOME_BUDGET_EXCEEDED,
    OUTCOME_COMPLETED,
    OUTCOME_ERROR,
    OUTCOME_NEEDS_DECISION,
    OUTCOME_OK,
    PHASE_IMPLEMENTATION,
    PHASE_NAMES,
    PHASE_OTHER,
    PHASE_OUTCOMES,
    PHASE_REVIEW,
    PHASE_REVIEW_REPAIR,
    PHASE_VALIDATION_FINAL,
    PHASE_VALIDATION_INITIAL,
    PHASE_VALIDATION_REPAIR,
    RETRY_BUCKETS,
    ROLE_IMPLEMENTER,
    SPAN_ATTEMPT,
    SPAN_NAMES,
    SPAN_PHASE,
    SPAN_TASK,
    TASK_OUTCOMES,
    TERMINATION_REASONS,
    Telemetry,
    normalize_phase,
    phase_usage_attributes,
    retry_bucket,
    usage_attributes,
)
from codex_forge.usage import ACCOUNTING_BASIS, BUDGET_BASIS, aggregate_token_usage

pytest.importorskip("opentelemetry.sdk.trace", reason="telemetry extra is not installed")

from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)


ENABLED = TelemetryConfig(enabled=True)


class RecordingTelemetry:
    """An enabled Telemetry instance plus the in-memory exporter it writes to."""

    def __init__(self, config: TelemetryConfig = ENABLED, exporter=None):
        self.exporter = exporter or InMemorySpanExporter()
        self.telemetry = Telemetry(
            config, processor_factory=lambda: SimpleSpanProcessor(self.exporter)
        )
        self.telemetry.start()

    def spans(self):
        return list(self.exporter.get_finished_spans())

    def by_name(self, name: str):
        return [span for span in self.spans() if span.name == name]

    def one(self, name: str):
        matches = self.by_name(name)
        assert len(matches) == 1, f"expected exactly one {name} span, got {len(matches)}"
        return matches[0]


# ------------------------------------------------------------------------------------------------
# Disabled by default
# ------------------------------------------------------------------------------------------------


def test_disabled_telemetry_emits_nothing():
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(
        TelemetryConfig(enabled=False), processor_factory=lambda: SimpleSpanProcessor(exporter)
    )
    telemetry.start()
    with telemetry.task_span(task_id="TASK-1", project_id="p", risk="medium", max_attempts=3) as task:
        task.set_outcome(OUTCOME_COMPLETED)
        with telemetry.attempt_span(attempt_number=1):
            with telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
                phase.set_outcome(OUTCOME_OK)
    assert exporter.get_finished_spans() == ()
    assert telemetry.active is False
    assert telemetry.warnings == ()


def test_disabled_telemetry_never_builds_a_provider():
    """A disabled configuration must not construct an exporter or open a socket."""

    def explode():
        raise AssertionError("a disabled configuration must not build a span processor")

    telemetry = Telemetry(TelemetryConfig(enabled=False), processor_factory=explode)
    telemetry.start()
    telemetry.flush()
    telemetry.shutdown()
    assert telemetry.active is False


def test_default_telemetry_config_is_disabled_loopback_only():
    default = TelemetryConfig()
    assert default.enabled is False
    assert default.endpoint == "http://127.0.0.1:4317"
    assert default.sample_ratio == 1.0


# ------------------------------------------------------------------------------------------------
# Span hierarchy
# ------------------------------------------------------------------------------------------------


def test_enabled_telemetry_produces_task_attempt_phase_parentage():
    recorder = RecordingTelemetry()
    telemetry = recorder.telemetry
    with telemetry.task_span(task_id="TASK-abc", project_id="proj", risk="high", max_attempts=3) as task:
        with telemetry.attempt_span(attempt_number=2) as attempt:
            with telemetry.phase_span(
                phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER, model="m", effort="high"
            ) as phase:
                phase.set_outcome(OUTCOME_OK)
            attempt.set_outcome(OUTCOME_COMPLETED)
        task.set_outcome(OUTCOME_COMPLETED)

    assert {span.name for span in recorder.spans()} == {SPAN_TASK, SPAN_ATTEMPT, SPAN_PHASE}
    task_span = recorder.one(SPAN_TASK)
    attempt_span = recorder.one(SPAN_ATTEMPT)
    phase_span = recorder.one(SPAN_PHASE)

    assert task_span.parent is None
    assert attempt_span.parent.span_id == task_span.context.span_id
    assert phase_span.parent.span_id == attempt_span.context.span_id
    # One trace covers the whole task.
    assert task_span.context.trace_id == attempt_span.context.trace_id == phase_span.context.trace_id


def test_sibling_phases_share_one_attempt_parent():
    recorder = RecordingTelemetry()
    telemetry = recorder.telemetry
    with telemetry.task_span(task_id="TASK-abc", project_id="proj", risk="low", max_attempts=1):
        with telemetry.attempt_span(attempt_number=1):
            for name in (PHASE_IMPLEMENTATION, PHASE_VALIDATION_INITIAL, PHASE_REVIEW):
                with telemetry.phase_span(phase_name=name, role=ROLE_IMPLEMENTER) as phase:
                    phase.set_outcome(OUTCOME_OK)

    attempt_span = recorder.one(SPAN_ATTEMPT)
    phases = recorder.by_name(SPAN_PHASE)
    assert len(phases) == 3
    assert {span.parent.span_id for span in phases} == {attempt_span.context.span_id}
    assert [span.attributes[ATTR_PHASE_NAME] for span in phases] == [
        PHASE_IMPLEMENTATION,
        PHASE_VALIDATION_INITIAL,
        PHASE_REVIEW,
    ]


def test_task_and_attempt_attributes_are_recorded():
    recorder = RecordingTelemetry()
    telemetry = recorder.telemetry
    with telemetry.task_span(task_id="TASK-9", project_id="proj-1", risk="critical", max_attempts=5) as task:
        with telemetry.attempt_span(attempt_number=4) as attempt:
            attempt.set_outcome(OUTCOME_COMPLETED)
            attempt.set(ATTR_ATTEMPT_TERMINATED_BY, "completion")
        task.set_outcome(OUTCOME_COMPLETED)

    task_span = recorder.one(SPAN_TASK)
    assert task_span.attributes[ATTR_TASK_ID] == "TASK-9"
    assert task_span.attributes["forge.project.id"] == "proj-1"
    assert task_span.attributes[ATTR_TASK_RISK] == "critical"
    assert task_span.attributes["forge.task.max_attempts"] == 5
    assert task_span.attributes[ATTR_TASK_OUTCOME] == OUTCOME_COMPLETED

    attempt_span = recorder.one(SPAN_ATTEMPT)
    assert attempt_span.attributes[ATTR_ATTEMPT_NUMBER] == 4
    assert attempt_span.attributes[ATTR_ATTEMPT_KIND] == "full"
    assert attempt_span.attributes[ATTR_RETRY_BUCKET] == "3+"
    assert attempt_span.attributes[ATTR_ATTEMPT_OUTCOME] == OUTCOME_COMPLETED
    assert attempt_span.attributes[ATTR_ATTEMPT_TERMINATED_BY] == "completion"


def test_phase_span_records_model_effort_and_duration():
    recorder = RecordingTelemetry()
    with recorder.telemetry.phase_span(
        phase_name=PHASE_REVIEW, cycle=1, role="reviewer", model="gpt-5.6-terra", effort="medium"
    ) as phase:
        phase.set_outcome(OUTCOME_OK)
    span = recorder.one(SPAN_PHASE)
    assert span.attributes[ATTR_PHASE_NAME] == PHASE_REVIEW
    assert span.attributes[ATTR_PHASE_CYCLE] == 1
    assert span.attributes[ATTR_AGENT_ROLE] == "reviewer"
    assert span.attributes["gen_ai.request.model"] == "gpt-5.6-terra"
    assert span.attributes["gen_ai.request.reasoning_effort"] == "medium"
    assert span.attributes[ATTR_PHASE_OUTCOME] == OUTCOME_OK
    assert span.attributes[ATTR_PHASE_DURATION_MS] >= 0


def test_phase_span_omits_model_attributes_for_non_model_phases():
    """A validator or controller phase issues no model request, so it claims no model."""
    recorder = RecordingTelemetry()
    with recorder.telemetry.phase_span(phase_name=PHASE_VALIDATION_INITIAL, role="validator") as phase:
        phase.set_outcome(OUTCOME_OK)
    attributes = recorder.one(SPAN_PHASE).attributes
    assert "gen_ai.request.model" not in attributes
    assert "gen_ai.request.reasoning_effort" not in attributes


# ------------------------------------------------------------------------------------------------
# Lifecycle: idempotency, shutdown, failure isolation
# ------------------------------------------------------------------------------------------------


def test_initialization_is_idempotent():
    builds = []

    def factory():
        builds.append(1)
        return SimpleSpanProcessor(InMemorySpanExporter())

    telemetry = Telemetry(ENABLED, processor_factory=factory)
    telemetry.start()
    provider = telemetry._provider
    tracer = telemetry._tracer
    for _ in range(4):
        telemetry.start()
    assert builds == [1]
    assert telemetry._provider is provider
    assert telemetry._tracer is tracer


def test_shutdown_is_safe_and_repeatable():
    recorder = RecordingTelemetry()
    with recorder.telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
        phase.set_outcome(OUTCOME_OK)
    recorder.telemetry.shutdown()
    recorder.telemetry.shutdown()
    recorder.telemetry.flush()
    assert recorder.telemetry.active is False
    # Spans opened after shutdown are inert rather than an error.
    with recorder.telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        task.set_outcome(OUTCOME_COMPLETED)
    assert len(recorder.by_name(SPAN_PHASE)) == 1
    assert recorder.by_name(SPAN_TASK) == []


def test_every_span_closes_exactly_once():
    """A double end() would double-count durations and log SDK warnings on every task."""

    class CountingProcessor(SimpleSpanProcessor):
        def __init__(self, exporter):
            super().__init__(exporter)
            self.ended: list[str] = []

        def on_end(self, span):
            self.ended.append(span.name)
            super().on_end(span)

    exporter = InMemorySpanExporter()
    processor = CountingProcessor(exporter)
    telemetry = Telemetry(ENABLED, processor_factory=lambda: processor)
    telemetry.start()

    with telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        with telemetry.attempt_span(attempt_number=1) as attempt:
            with telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
                phase.set_outcome(OUTCOME_OK)
            attempt.set_outcome(OUTCOME_COMPLETED)
        task.set_outcome(OUTCOME_COMPLETED)

    assert sorted(processor.ended) == sorted([SPAN_ATTEMPT, SPAN_PHASE, SPAN_TASK])


def test_every_span_closes_exactly_once_on_the_exception_path():
    class CountingProcessor(SimpleSpanProcessor):
        def __init__(self, exporter):
            super().__init__(exporter)
            self.ended: list[str] = []

        def on_end(self, span):
            self.ended.append(span.name)
            super().on_end(span)

    processor = CountingProcessor(InMemorySpanExporter())
    telemetry = Telemetry(ENABLED, processor_factory=lambda: processor)
    telemetry.start()

    with pytest.raises(RuntimeError):
        with telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1):
            with telemetry.attempt_span(attempt_number=1):
                with telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER):
                    raise RuntimeError("boom")

    assert sorted(processor.ended) == sorted([SPAN_ATTEMPT, SPAN_PHASE, SPAN_TASK])


def test_shutdown_is_bounded_by_its_timeout_not_twice_it():
    """The flush and the join share one budget, so the caller waits at most what it asked for."""

    class SlowProcessor(SimpleSpanProcessor):
        def force_flush(self, timeout_millis=30_000):
            time.sleep(2.0)
            return False

        def shutdown(self):
            time.sleep(2.0)

    telemetry = Telemetry(ENABLED, processor_factory=lambda: SlowProcessor(InMemorySpanExporter()))
    telemetry.start()
    started = time.monotonic()
    telemetry.shutdown(timeout_seconds=1.0)
    elapsed = time.monotonic() - started
    assert elapsed < 1.6, f"shutdown took {elapsed:.2f}s for a 1.0s budget"


def test_shutdown_without_start_is_safe():
    Telemetry(TelemetryConfig(enabled=False)).shutdown()
    Telemetry(ENABLED, processor_factory=lambda: SimpleSpanProcessor(InMemorySpanExporter())).shutdown()


def test_initialization_failure_degrades_to_no_op_with_bounded_warning():
    def failing_factory():
        raise OSError(r"cannot reach collector at C:\secret\path with token abc123")

    telemetry = Telemetry(ENABLED, processor_factory=failing_factory)
    telemetry.start()
    assert telemetry.active is False
    with telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        task.set_outcome(OUTCOME_COMPLETED)
    assert len(telemetry.warnings) == 1
    warning = telemetry.warnings[0]
    assert "OSError" in warning
    # The exception message is never carried into a warning an operator might paste elsewhere.
    assert "secret" not in warning and "abc123" not in warning and "\\" not in warning


def test_missing_sdk_degrades_to_no_op(monkeypatch):
    """Enabled telemetry with the optional extra uninstalled must not break the controller."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    telemetry = Telemetry(ENABLED)
    telemetry.start()
    assert telemetry.active is False
    with telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        with telemetry.attempt_span(attempt_number=1) as attempt:
            with telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
                phase.set_outcome(OUTCOME_OK)
            attempt.set_outcome(OUTCOME_COMPLETED)
        task.set_outcome(OUTCOME_COMPLETED)
    telemetry.shutdown()
    assert telemetry.warnings == ("telemetry init unavailable (ModuleNotFoundError)",)


def test_warnings_are_bounded():
    telemetry = Telemetry(ENABLED)
    for index in range(50):
        telemetry._warn(f"stage-{index}", ValueError("boom"))
    assert len(telemetry.warnings) == telemetry_module.MAX_WARNINGS


def test_export_failure_does_not_escape_the_span_helpers():
    class ExplodingExporter(InMemorySpanExporter):
        def export(self, spans):
            raise RuntimeError("collector refused the connection")

    recorder = RecordingTelemetry(exporter=ExplodingExporter())
    with recorder.telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        with recorder.telemetry.attempt_span(attempt_number=1) as attempt:
            with recorder.telemetry.phase_span(
                phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER
            ) as phase:
                phase.set_outcome(OUTCOME_OK)
            attempt.set_outcome(OUTCOME_COMPLETED)
        task.set_outcome(OUTCOME_COMPLETED)
    recorder.telemetry.shutdown()


def test_span_start_failure_yields_an_inert_handle():
    recorder = RecordingTelemetry()

    class BrokenTracer:
        def start_as_current_span(self, *args, **kwargs):
            raise RuntimeError("tracer is broken")

    recorder.telemetry._tracer = BrokenTracer()
    with recorder.telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
        assert phase.recording is False
        phase.set_outcome(OUTCOME_OK)
        phase.set(ATTR_TASK_ID, "TASK-1")
    assert recorder.spans() == []
    assert any("span_start" in warning for warning in recorder.telemetry.warnings)


# ------------------------------------------------------------------------------------------------
# Span closure on every exit path
# ------------------------------------------------------------------------------------------------


def test_exception_closes_every_span_and_records_bounded_error_fields():
    recorder = RecordingTelemetry()
    telemetry = recorder.telemetry
    secret = r"C:\CodexData\worktrees\feature -- stderr tail with token sk-live-1234"
    with pytest.raises(RuntimeError):
        with telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1):
            with telemetry.attempt_span(attempt_number=1):
                with telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER):
                    raise RuntimeError(secret)

    spans = recorder.spans()
    assert {span.name for span in spans} == {SPAN_TASK, SPAN_ATTEMPT, SPAN_PHASE}
    for span in spans:
        assert span.end_time is not None
        assert span.attributes[ATTR_ERROR_TYPE] == "RuntimeError"
        assert span.attributes[ATTR_ERROR_CODE] == "unexpected_error"
        # No exception event, and no status description: both would carry the message.
        assert span.events == ()
        assert not span.status.description
        assert secret not in str(span.attributes)


def test_budget_exception_is_classified_distinctly():
    recorder = RecordingTelemetry()
    with pytest.raises(TaskUsageBudgetExceeded):
        with recorder.telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER):
            raise TaskUsageBudgetExceeded("TOKEN_BUDGET_EXCEEDED: 10 of 5 for TASK-1")
    span = recorder.one(SPAN_PHASE)
    assert span.attributes[ATTR_PHASE_OUTCOME] == OUTCOME_BUDGET_EXCEEDED
    assert span.attributes[ATTR_ERROR_CODE] == ERROR_TOKEN_BUDGET_EXCEEDED


def test_a_specific_error_code_is_not_overwritten_by_the_generic_one():
    recorder = RecordingTelemetry()
    with pytest.raises(RuntimeError):
        with recorder.telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
            phase.set_error_code(ERROR_CODEX_EXIT_NONZERO)
            raise RuntimeError("exit 1")
    span = recorder.one(SPAN_PHASE)
    assert span.attributes[ATTR_ERROR_CODE] == ERROR_CODEX_EXIT_NONZERO
    assert span.attributes[ATTR_PHASE_OUTCOME] == OUTCOME_ERROR


def test_generator_close_still_ends_the_span():
    """A daemon torn down mid-phase must not leave a span open."""
    recorder = RecordingTelemetry()
    manager = recorder.telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER)
    manager.__enter__()
    manager.gen.close()
    assert len(recorder.by_name(SPAN_PHASE)) == 1
    assert recorder.one(SPAN_PHASE).end_time is not None


@pytest.mark.parametrize("outcome", sorted(TASK_OUTCOMES))
def test_every_task_outcome_closes_the_span_with_a_bounded_value(outcome):
    recorder = RecordingTelemetry()
    with recorder.telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        task.set_outcome(outcome)
    span = recorder.one(SPAN_TASK)
    assert span.end_time is not None
    assert span.attributes[ATTR_TASK_OUTCOME] in TASK_OUTCOMES
    if outcome in {OUTCOME_COMPLETED, OUTCOME_NEEDS_DECISION}:
        # A completed task and a task waiting on a person are both non-error terminations.
        assert ATTR_ERROR_CODE not in span.attributes
    else:
        assert span.attributes[ATTR_ERROR_CODE] in ERROR_CODES


@pytest.mark.parametrize("outcome", sorted(PHASE_OUTCOMES))
def test_every_phase_outcome_closes_the_span_with_a_bounded_value(outcome):
    recorder = RecordingTelemetry()
    with recorder.telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
        phase.set_outcome(outcome)
    assert recorder.one(SPAN_PHASE).attributes[ATTR_PHASE_OUTCOME] in PHASE_OUTCOMES


# ------------------------------------------------------------------------------------------------
# Phase normalization and cardinality
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "expected"),
    [
        ("implementation", "codex", (PHASE_IMPLEMENTATION, 0)),
        ("validation-repair-1", "codex", (PHASE_VALIDATION_REPAIR, 1)),
        ("validation-repair-2", "codex", (PHASE_VALIDATION_REPAIR, 2)),
        ("review", "codex", (PHASE_REVIEW, 0)),
        ("review-2", "codex", (PHASE_REVIEW, 1)),
        ("review-repair-1", "codex", (PHASE_REVIEW_REPAIR, 1)),
        ("evidence", "codex", ("evidence", 0)),
        ("completion", "codex", ("completion", 0)),
        ("initial", "validation", (PHASE_VALIDATION_INITIAL, 0)),
        ("repair-1", "validation", (PHASE_VALIDATION_REPAIR, 1)),
        ("review-repair-2", "validation", (PHASE_VALIDATION_REPAIR, 2)),
        ("final", "validation", (PHASE_VALIDATION_FINAL, 0)),
        ("", "codex", (PHASE_OTHER, 0)),
        ("something-new-9", "codex", (PHASE_OTHER, 9)),
    ],
)
def test_phase_normalization(raw, kind, expected):
    assert normalize_phase(raw, kind=kind) == expected


def test_phase_names_stay_bounded_for_arbitrary_labels():
    labels = [
        "implementation",
        "review",
        "review-99",
        "validation-repair-100000",
        "review-repair-3",
        r"C:\CodexData\worktrees\feature-branch",
        "TASK-0123456789",
        "deadbeefcafe",
        "",
        "  ",
        "totally-unknown-phase",
    ]
    for kind in ("codex", "validation"):
        for label in labels:
            name, cycle = normalize_phase(label, kind=kind)
            assert name in PHASE_NAMES
            assert isinstance(cycle, int) and 0 <= cycle <= telemetry_module.MAX_PHASE_CYCLE


def test_normalize_phase_never_raises_on_a_long_digit_run():
    """An unbounded digit group would hand `int()` more than CPython's 4300-digit limit."""
    for label in ("review-" + "9" * 5_000, "validation-repair-" + "1" * 10_000, "-" * 500 + "7" * 99):
        name, cycle = normalize_phase(label)
        assert name in PHASE_NAMES
        assert 0 <= cycle <= telemetry_module.MAX_PHASE_CYCLE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("low", "low"),
        ("HIGH", "high"),
        ("  critical  ", "critical"),
        # The inbox YAML importer accepts `str(data.get("risk", "medium"))` unvalidated, so these
        # are all reachable values for `task.risk`.
        (r"C:\CodexData\evidence\TASK-1", "other"),
        ("the reviewer requires changes to error handling", "other"),
        ("", "other"),
        (None, "other"),
        (12, "other"),
    ],
)
def test_risk_is_bounded_on_every_ingestion_path(raw, expected):
    assert telemetry_module.normalize_risk(raw) == expected
    assert telemetry_module.normalize_risk(raw) in telemetry_module.RISK_VALUES


def test_unbounded_risk_cannot_reach_a_span():
    recorder = RecordingTelemetry()
    with recorder.telemetry.task_span(
        task_id="T", project_id="p", risk=r"C:\CodexData\secret plan", max_attempts=1
    ) as task:
        task.set_outcome(OUTCOME_COMPLETED)
    assert recorder.one(SPAN_TASK).attributes[ATTR_TASK_RISK] == "other"


@pytest.mark.parametrize(
    "raw",
    ["TASK-0123456789", "project-abc.def_1", "T", "a" * telemetry_module.MAX_IDENTIFIER_CHARS],
)
def test_identifier_shaped_values_pass_through(raw):
    """Forge's own opaque ids must stay readable."""
    assert telemetry_module.safe_identifier(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\CodexData\worktrees\feature",
        "https://github.com/example/repo",
        "has spaces and prose",
        "You are the implementer. Repo https://github.com/acme/private",
        "-leading-dash",
        "a" * (telemetry_module.MAX_IDENTIFIER_CHARS + 1),
        "",
    ],
)
def test_non_identifier_values_are_replaced_wholesale(raw):
    """Scrubbing separators would leave the words: `github.com` and `CodexData` must not survive."""
    result = telemetry_module.safe_identifier(raw)
    assert result.startswith("opaque-")
    assert telemetry_module._SAFE_IDENTIFIER.fullmatch(result)
    for word in ("CodexData", "github", "implementer", "spaces", "prose", "worktrees"):
        assert word not in result


def test_identifier_replacement_is_stable_so_spans_still_correlate():
    first = telemetry_module.safe_identifier(r"C:\CodexData\forge.db")
    assert first == telemetry_module.safe_identifier(r"C:\CodexData\forge.db")
    assert first != telemetry_module.safe_identifier(r"C:\CodexData\other.db")


def test_identifier_sanitization_reaches_the_span():
    recorder = RecordingTelemetry()
    with recorder.telemetry.task_span(
        task_id=r"C:\CodexData\forge.db",
        project_id="Repo https://github.com/acme/private",
        risk="low",
        max_attempts=1,
    ) as task:
        task.set_outcome(OUTCOME_COMPLETED)
    attributes = recorder.one(SPAN_TASK).attributes
    for value in (attributes[ATTR_TASK_ID], attributes["forge.project.id"]):
        assert value.startswith("opaque-")
        assert len(value) <= telemetry_module.MAX_IDENTIFIER_CHARS
    rendered = str(dict(attributes))
    for word in ("CodexData", "github", "forge.db", "acme"):
        assert word not in rendered


@pytest.mark.parametrize(
    ("attempts", "expected"), [(0, "0"), (1, "0"), (2, "1"), (3, "2"), (4, "3+"), (99, "3+"), (None, "0")]
)
def test_retry_bucket_is_bounded(attempts, expected):
    assert retry_bucket(attempts) == expected
    assert retry_bucket(attempts) in RETRY_BUCKETS


def test_span_names_are_constant_and_contain_no_identifiers():
    """Span names must not grow with tasks, paths, branches, commits, or repair cycles."""
    recorder = RecordingTelemetry()
    telemetry = recorder.telemetry
    for index in range(3):
        with telemetry.task_span(
            task_id=f"TASK-{index:010x}", project_id="project-abc", risk="medium", max_attempts=3
        ) as task:
            with telemetry.attempt_span(attempt_number=index + 1) as attempt:
                for label in ("implementation", f"validation-repair-{index}", f"review-{index + 1}"):
                    name, cycle = normalize_phase(label)
                    with telemetry.phase_span(phase_name=name, cycle=cycle, role=ROLE_IMPLEMENTER) as phase:
                        phase.set_outcome(OUTCOME_OK)
                attempt.set_outcome(OUTCOME_COMPLETED)
            task.set_outcome(OUTCOME_COMPLETED)

    names = {span.name for span in recorder.spans()}
    assert names == SPAN_NAMES
    for name in names:
        assert not re.search(r"\d", name), name
        assert "TASK-" not in name and "/" not in name and "\\" not in name


def test_low_cardinality_attributes_never_carry_identifiers():
    recorder = RecordingTelemetry()
    telemetry = recorder.telemetry
    allowed = (
        {"low", "medium", "high", "critical"}
        | TASK_OUTCOMES
        | PHASE_OUTCOMES
        | PHASE_NAMES
        | AGENT_ROLES
        | ATTEMPT_KINDS
        | TERMINATION_REASONS
        | ERROR_CODES
        | set(RETRY_BUCKETS)
        | {ACCOUNTING_BASIS, BUDGET_BASIS}
    )
    with telemetry.task_span(
        task_id="TASK-0123456789", project_id="project-deadbeef", risk="high", max_attempts=3
    ) as task:
        with telemetry.attempt_span(attempt_number=2) as attempt:
            with telemetry.phase_span(
                phase_name=PHASE_VALIDATION_REPAIR, cycle=2, role=ROLE_IMPLEMENTER, model="gpt-5.6-sol"
            ) as phase:
                phase.set_outcome(OUTCOME_OK)
            attempt.set_outcome(OUTCOME_COMPLETED)
            attempt.set(ATTR_ATTEMPT_TERMINATED_BY, "completion")
        task.set_outcome(OUTCOME_COMPLETED)

    seen = set()
    for span in recorder.spans():
        for key, value in span.attributes.items():
            if key in LOW_CARDINALITY_ATTRIBUTES:
                seen.add(key)
                assert value in allowed, f"{key}={value!r} is outside the bounded vocabulary"
    assert seen, "no low-cardinality attributes were exercised"


def test_attribute_values_are_length_capped():
    """Two independent caps: identifiers are bounded first, and every attribute again after that."""
    recorder = RecordingTelemetry()
    with recorder.telemetry.task_span(
        task_id="T" * 5_000, project_id="p", risk="low", max_attempts=1
    ) as task:
        task.set("forge.probe.freeform", "x" * 5_000)
        task.set_outcome(OUTCOME_COMPLETED)
    attributes = recorder.one(SPAN_TASK).attributes
    assert len(attributes[ATTR_TASK_ID]) <= telemetry_module.MAX_IDENTIFIER_CHARS
    assert len(attributes["forge.probe.freeform"]) == telemetry_module.MAX_ATTRIBUTE_CHARS


# ------------------------------------------------------------------------------------------------
# Usage attributes come from the shared aggregator
# ------------------------------------------------------------------------------------------------


def test_phase_usage_attributes_match_the_shared_normalizer():
    reported = {
        "input_tokens": 1000,
        "cached_input_tokens": 250,
        "cache_write_input_tokens": 40,
        "output_tokens": 300,
        "reasoning_output_tokens": 120,
    }
    attributes = phase_usage_attributes(reported)
    assert attributes[ATTR_INPUT_TOKENS] == 1000
    assert attributes[ATTR_CACHED_INPUT_TOKENS] == 250
    assert attributes[ATTR_FRESH_INPUT_TOKENS] == 750
    assert attributes[ATTR_OUTPUT_TOKENS] == 300
    assert attributes[ATTR_REASONING_TOKENS] == 120
    assert attributes[ATTR_RAW_TOTAL_TOKENS] == 1300
    assert attributes[ATTR_CACHE_HIT_RATIO] == 0.25
    assert attributes[ATTR_ACCOUNTING_BASIS] == ACCOUNTING_BASIS
    assert attributes[ATTR_BUDGET_BASIS] == BUDGET_BASIS


def test_task_usage_attributes_equal_the_control_plane_aggregate():
    """Telemetry must report the same totals the JSON contract and the budget report."""
    usages = [
        '{"input_tokens": 400, "cached_input_tokens": 100, "output_tokens": 50,'
        ' "reasoning_output_tokens": 10}',
        '{"input_tokens": 600, "cached_input_tokens": 300, "output_tokens": 70,'
        ' "reasoning_output_tokens": 20}',
    ]
    totals = aggregate_token_usage(usages)
    attributes = usage_attributes(totals)
    assert attributes[ATTR_INPUT_TOKENS] == totals["input_tokens"] == 1000
    assert attributes[ATTR_CACHED_INPUT_TOKENS] == totals["cached_input_tokens"] == 400
    assert attributes[ATTR_FRESH_INPUT_TOKENS] == totals["fresh_input_tokens"] == 600
    assert attributes[ATTR_OUTPUT_TOKENS] == totals["output_tokens"] == 120
    assert attributes[ATTR_REASONING_TOKENS] == totals["reasoning_output_tokens"] == 30
    assert attributes[ATTR_RAW_TOTAL_TOKENS] == totals["raw_total_tokens"] == totals["total_tokens"]
    assert attributes[ATTR_CACHE_HIT_RATIO] == totals["cache_hit_ratio"]


def test_malformed_usage_degrades_to_zero_rather_than_raising():
    for bad in (None, "", "not json", [1, 2, 3], {"input_tokens": "abc"}):
        attributes = phase_usage_attributes(bad)
        assert attributes[ATTR_INPUT_TOKENS] == 0
        assert attributes[ATTR_RAW_TOTAL_TOKENS] == 0
        assert attributes[ATTR_CACHE_HIT_RATIO] == 0.0


def test_usage_attributes_are_total_for_hostile_input():
    """`usage_attributes` must never raise; instrumentation cannot be a source of task failures."""
    hostile = {
        "input_tokens": "not a number",
        "cached_input_tokens": None,
        "fresh_input_tokens": -5,
        "output_tokens": True,
        "reasoning_output_tokens": float("nan"),
        "raw_total_tokens": object(),
        "cache_hit_ratio": "high",
    }
    attributes = usage_attributes(hostile)
    assert all(value >= 0 for value in (attributes[ATTR_INPUT_TOKENS], attributes[ATTR_OUTPUT_TOKENS]))
    assert attributes[ATTR_CACHE_HIT_RATIO] == 0.0
    assert usage_attributes({})[ATTR_RAW_TOTAL_TOKENS] == 0


# ------------------------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------------------------


BASE_CONFIG = """
[paths]
data_root = "data"

[codex]
approval_policy = "never"
sandbox = "danger-full-access"
"""


def write_config(tmp_path: Path, telemetry_section: str = "") -> Path:
    path = tmp_path / "forge.toml"
    path.write_text(BASE_CONFIG + telemetry_section, encoding="utf-8")
    return path


def test_config_without_telemetry_section_preserves_current_behavior(tmp_path: Path):
    config = load_config(write_config(tmp_path))
    assert config.telemetry == TelemetryConfig()
    assert config.telemetry.enabled is False
    # Everything else resolves exactly as it did before the section existed.
    assert config.approval_policy == "never"
    assert config.max_task_tokens == 2_000_000


def test_full_telemetry_section_round_trips(tmp_path: Path):
    path = write_config(
        tmp_path,
        """
[telemetry]
enabled = true
service_name = "codex-forge"
endpoint = "http://127.0.0.1:4317"
protocol = "grpc"
exporter = "otlp"
sample_ratio = 0.5
environment = "vps-1"
""",
    )
    telemetry = load_config(path).telemetry
    assert telemetry == TelemetryConfig(
        enabled=True,
        service_name="codex-forge",
        endpoint="http://127.0.0.1:4317",
        protocol="grpc",
        exporter="otlp",
        sample_ratio=0.5,
        environment="vps-1",
    )


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ('[telemetry]\nenabled = "yes"\n', "must be true or false"),
        ("[telemetry]\nenabled = 1\n", "must be true or false"),
        ('[telemetry]\nservice_name = ""\n', "must not be empty"),
        ('[telemetry]\nservice_name = "a b"\n', "may contain only"),
        ('[telemetry]\nservice_name = "' + "x" * 65 + '"\n', "at most 64 characters"),
        ('[telemetry]\nendpoint = "http://example.com:4317"\n', "loopback"),
        ('[telemetry]\nendpoint = "http://10.0.0.5:4317"\n', "loopback"),
        ('[telemetry]\nendpoint = "https://127.0.0.1:4317"\n', "must use http"),
        ('[telemetry]\nendpoint = "http://user:pw@127.0.0.1:4317"\n', "must not embed credentials"),
        ('[telemetry]\nendpoint = "http://127.0.0.1:4317/v1/traces"\n', "bare host and port"),
        ('[telemetry]\nendpoint = "http://127.0.0.1"\n', "explicit port"),
        ("[telemetry]\nsample_ratio = 1.5\n", "between 0.0 and 1.0"),
        ("[telemetry]\nsample_ratio = -0.1\n", "between 0.0 and 1.0"),
        ('[telemetry]\nsample_ratio = "1.0"\n', "must be a number"),
        ('[telemetry]\nprotocol = "http"\n', "telemetry.protocol must be one of"),
        ('[telemetry]\nexporter = "jaeger"\n', "telemetry.exporter must be one of"),
        ("[telemetry]\ncollector_headers = { a = 1 }\n", "Unknown telemetry option"),
        ('[telemetry]\nendpoint_url = "http://127.0.0.1:4317"\n', "Unknown telemetry option"),
        ('[telemetry]\nenvironment = "has space"\n', "may contain only"),
    ],
)
def test_invalid_telemetry_configuration_fails_clearly(tmp_path: Path, section: str, message: str):
    with pytest.raises(ValueError, match=re.escape(message)):
        load_config(write_config(tmp_path, section))


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_hosts_are_accepted(tmp_path: Path, host: str):
    path = write_config(tmp_path, f'[telemetry]\nendpoint = "http://{host}:4317"\n')
    assert load_config(path).telemetry.endpoint == f"http://{host}:4317"


def test_sample_ratio_bounds_are_inclusive(tmp_path: Path):
    for ratio in ("0.0", "1.0", "0"):
        path = write_config(tmp_path, f"[telemetry]\nsample_ratio = {ratio}\n")
        assert 0.0 <= load_config(path).telemetry.sample_ratio <= 1.0


def test_empty_environment_omits_the_resource_attribute(tmp_path: Path):
    path = write_config(tmp_path, '[telemetry]\nenvironment = ""\n')
    assert load_config(path).telemetry.environment == ""


# ------------------------------------------------------------------------------------------------
# Resource and exporter isolation
# ------------------------------------------------------------------------------------------------


def test_environment_restoration_is_exact(monkeypatch):
    """Every original key and value returns, and a key created inside the window does not survive."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer secret")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", raising=False)
    monkeypatch.setenv("OTEL_UNRELATED", "untouched")
    monkeypatch.setenv("PATH_LIKE_CONTROL", "untouched")
    before = dict(os.environ)

    with telemetry_module._without_exporter_headers():
        assert "OTEL_EXPORTER_OTLP_HEADERS" not in os.environ
        assert os.environ["OTEL_UNRELATED"] == "untouched"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = "created-inside"

    assert dict(os.environ) == before


def test_environment_is_restored_when_construction_raises(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer secret")
    before = dict(os.environ)
    with pytest.raises(RuntimeError):
        with telemetry_module._without_exporter_headers():
            raise RuntimeError("exporter construction failed")
    assert dict(os.environ) == before


def test_only_header_variables_are_suppressed(monkeypatch):
    """The window must not touch settings that carry no operator data, nor the SDK kill switch."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "k=v")
    with telemetry_module._without_exporter_headers():
        assert os.environ["OTEL_SDK_DISABLED"] == "false"
        assert os.environ["OTEL_TRACES_SAMPLER"] == "always_on"
        assert os.environ["OTEL_RESOURCE_ATTRIBUTES"] == "k=v"


def test_initialization_and_shutdown_leave_the_environment_untouched(monkeypatch):
    """A child process spawned later must inherit the operator's original OTEL_* configuration."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-api-key=abc123")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment=operator-choice")
    before = dict(os.environ)

    recorder = RecordingTelemetry()
    with recorder.telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
        phase.set_outcome(OUTCOME_OK)
    recorder.telemetry.flush()
    recorder.telemetry.shutdown()

    assert dict(os.environ) == before


def test_real_exporter_construction_restores_the_environment(monkeypatch):
    """Exercise the genuine OTLP exporter path, not just the injected test processor."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer secret")
    before = dict(os.environ)
    telemetry = Telemetry(ENABLED)
    telemetry.start()
    try:
        assert telemetry.active is True
        assert dict(os.environ) == before
    finally:
        telemetry.shutdown()
    assert dict(os.environ) == before


def test_resource_carries_only_declared_attributes(monkeypatch):
    """`OTEL_*` variables must not reach the resource, and no host detector may run."""
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "leaked.key=leaked-value,host.name=secret-host")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "not-forge")
    recorder = RecordingTelemetry(TelemetryConfig(enabled=True, environment="vps-1"))
    with recorder.telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
        phase.set_outcome(OUTCOME_OK)
    attributes = dict(recorder.one(SPAN_PHASE).resource.attributes)
    assert attributes["service.name"] == "codex-forge"
    assert attributes["deployment.environment"] == "vps-1"
    assert "leaked.key" not in attributes
    assert "host.name" not in attributes
    assert set(attributes) <= {"service.name", "service.version", "deployment.environment"}


def test_environment_is_restored_after_initialization(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer secret")
    RecordingTelemetry()
    import os

    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "authorization=Bearer secret"


def test_sample_ratio_zero_drops_whole_traces():
    recorder = RecordingTelemetry(TelemetryConfig(enabled=True, sample_ratio=0.0))
    telemetry = recorder.telemetry
    with telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        with telemetry.attempt_span(attempt_number=1) as attempt:
            with telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
                phase.set_outcome(OUTCOME_OK)
            attempt.set_outcome(OUTCOME_COMPLETED)
        task.set_outcome(OUTCOME_COMPLETED)
    # ParentBased sampling drops the whole trace rather than orphaning children.
    assert recorder.spans() == []


# ------------------------------------------------------------------------------------------------
# Orchestrator wiring: outcomes and budgets are unchanged
# ------------------------------------------------------------------------------------------------


class FakeDB:
    def __init__(self, usage: int = 0):
        self.updates: list[tuple[str, dict]] = []
        self.usage = usage
        self.decisions: list[tuple] = []

    def update_task(self, task_id: str, **kwargs):
        self.updates.append((task_id, kwargs))

    def task_token_usage(self, task_id: str):
        return aggregate_token_usage(
            [{"input_tokens": self.usage, "output_tokens": 0, "cached_input_tokens": 0}]
        )

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


def make_orchestrator(telemetry: Telemetry, db: FakeDB | None = None) -> Orchestrator:
    instance = object.__new__(Orchestrator)
    instance.db = db or FakeDB()
    instance.telemetry = telemetry
    instance.config = SimpleNamespace(max_output_chars=12_000, max_identical_failures=2, max_task_tokens=100)
    return instance


def test_execute_task_records_outcome_and_usage_on_the_task_span():
    recorder = RecordingTelemetry()
    db = FakeDB(usage=1234)
    orchestrator = make_orchestrator(recorder.telemetry, db)
    orchestrator._execute_task_body = lambda task: OUTCOME_COMPLETED

    orchestrator._execute_task(make_task(attempts=2))

    task_span = recorder.one(SPAN_TASK)
    attempt_span = recorder.one(SPAN_ATTEMPT)
    assert attempt_span.parent.span_id == task_span.context.span_id
    assert task_span.attributes[ATTR_TASK_OUTCOME] == OUTCOME_COMPLETED
    assert attempt_span.attributes[ATTR_ATTEMPT_OUTCOME] == OUTCOME_COMPLETED
    assert attempt_span.attributes[ATTR_ATTEMPT_TERMINATED_BY] == "completion"
    assert attempt_span.attributes[ATTR_RETRY_BUCKET] == "1"
    assert task_span.attributes[ATTR_INPUT_TOKENS] == 1234
    assert task_span.attributes[ATTR_RAW_TOTAL_TOKENS] == 1234


def test_execute_task_propagates_exceptions_after_closing_spans():
    recorder = RecordingTelemetry()
    orchestrator = make_orchestrator(recorder.telemetry)

    def explode(task):
        raise TaskUsageBudgetExceeded("TOKEN_BUDGET_EXCEEDED: 200 of 100 for TASK-1")

    orchestrator._execute_task_body = explode
    with pytest.raises(TaskUsageBudgetExceeded):
        orchestrator._execute_task(make_task())

    for span in recorder.spans():
        assert span.end_time is not None
        assert span.attributes["error.code"] == ERROR_TOKEN_BUDGET_EXCEEDED
        assert span.attributes[ATTR_ERROR_TYPE] == "TaskUsageBudgetExceeded"
    assert recorder.one(SPAN_TASK).attributes[ATTR_TASK_OUTCOME] == OUTCOME_BUDGET_EXCEEDED


def test_task_usage_read_is_skipped_when_telemetry_is_disabled():
    """A disabled configuration must not issue the extra usage query."""

    class RefusingDB(FakeDB):
        def task_token_usage(self, task_id: str):
            raise AssertionError("usage must not be read when telemetry is disabled")

    telemetry = Telemetry(TelemetryConfig(enabled=False))
    telemetry.start()
    orchestrator = make_orchestrator(telemetry, RefusingDB())
    orchestrator._execute_task_body = lambda task: OUTCOME_COMPLETED
    orchestrator._execute_task(make_task())


@pytest.mark.parametrize("returned", [None, {"input_tokens": object()}])
def test_usage_read_failure_does_not_fail_the_task(returned):
    """Neither a failing usage query nor an unusable result may change a task's outcome."""

    class BrokenDB(FakeDB):
        def task_token_usage(self, task_id: str):
            if returned is None:
                raise RuntimeError("database is locked")
            return returned

    recorder = RecordingTelemetry()
    orchestrator = make_orchestrator(recorder.telemetry, BrokenDB())
    orchestrator._execute_task_body = lambda task: OUTCOME_COMPLETED
    orchestrator._execute_task(make_task())
    assert recorder.one(SPAN_TASK).attributes[ATTR_TASK_OUTCOME] == OUTCOME_COMPLETED


def test_error_recording_failure_preserves_the_original_exception():
    """A fault inside instrumentation must not replace the exception the orchestrator handles."""
    recorder = RecordingTelemetry()

    def broken_record_error(self, exc):
        raise ValueError("instrumentation is broken")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(telemetry_module.SpanHandle, "record_error", broken_record_error)
    try:
        with pytest.raises(TaskUsageBudgetExceeded):
            with recorder.telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER):
                raise TaskUsageBudgetExceeded("TOKEN_BUDGET_EXCEEDED")
    finally:
        monkey.undo()
    assert recorder.one(SPAN_PHASE).end_time is not None


def test_budget_enforcement_is_identical_with_telemetry_enabled_and_disabled():
    """Enabling telemetry must not move the budget threshold in either direction."""
    for telemetry in (
        Telemetry(TelemetryConfig(enabled=False)),
        RecordingTelemetry().telemetry,
    ):
        under = make_orchestrator(telemetry, FakeDB(usage=99))
        under._enforce_usage_budget(make_task())

        at_limit = make_orchestrator(telemetry, FakeDB(usage=100))
        with pytest.raises(TaskUsageBudgetExceeded, match="TOKEN_BUDGET_EXCEEDED"):
            at_limit._enforce_usage_budget(make_task())


def test_retry_and_failure_decisions_are_identical_with_telemetry_enabled():
    for telemetry in (
        Telemetry(TelemetryConfig(enabled=False)),
        RecordingTelemetry().telemetry,
    ):
        db = FakeDB()
        orchestrator = make_orchestrator(telemetry, db)
        orchestrator._retry_or_fail(make_task(attempts=2), "transient")
        assert db.updates[-1][1]["state"] == TaskState.READY
        orchestrator._retry_or_fail(make_task(attempts=3), "final")
        assert db.updates[-1][1]["state"] == TaskState.FAILED


def test_noncompletion_outcome_reads_the_status_the_orchestrator_acted_on():
    from codex_forge.orchestrator import _noncompletion_outcome

    assert _noncompletion_outcome({"status": "needs_decision"}) == "needs_decision"
    assert _noncompletion_outcome({"status": "blocked"}) == "noncompletion"
    assert _noncompletion_outcome({}) == "noncompletion"


# ------------------------------------------------------------------------------------------------
# No sensitive material anywhere in a span
# ------------------------------------------------------------------------------------------------


FORBIDDEN = (
    "CodexData",
    "/home/",
    "sk-live-",
    "Bearer ",
    "PATH=",
    "git commit",
    "github.com",
    "You are the implementer",
    "the reviewer requires changes to",
    "forge.db",
)


def test_no_span_attribute_carries_sensitive_material():
    """Drive a realistic task shape and assert nothing sensitive reached a span."""
    recorder = RecordingTelemetry()
    telemetry = recorder.telemetry
    prompt = "You are the implementer. Repository: https://github.com/example/repo"
    review_prose = "the reviewer requires changes to error handling"

    # Hostile identifiers on purpose: `--id`, inbox `id:`, and inbox `risk:` are all unvalidated,
    # so the task span is a real ingress for operator text.
    with telemetry.task_span(
        task_id=r"C:\CodexData\forge.db",
        project_id=prompt,
        risk="the reviewer requires changes to error handling",
        max_attempts=3,
    ) as task:
        with telemetry.attempt_span(attempt_number=1) as attempt:
            with telemetry.phase_span(
                phase_name=PHASE_IMPLEMENTATION,
                role=ROLE_IMPLEMENTER,
                model="gpt-5.6-sol",
                effort="high",
            ) as phase:
                phase.set_many(
                    phase_usage_attributes(
                        {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}
                    )
                )
                phase.set_outcome(OUTCOME_OK)
            try:
                with telemetry.phase_span(phase_name=PHASE_REVIEW, role="reviewer"):
                    raise RuntimeError(f"{prompt}\n{review_prose}\nPATH={'x' * 20}")
            except RuntimeError:
                pass
            attempt.set_outcome(OUTCOME_COMPLETED)
        task.set_outcome(OUTCOME_COMPLETED)

    for span in recorder.spans():
        rendered = f"{span.name} {dict(span.attributes)} {span.events} {span.status.description}"
        for needle in FORBIDDEN:
            assert needle not in rendered, f"{needle!r} leaked into {span.name}"
        for value in span.attributes.values():
            if isinstance(value, str):
                assert "\\" not in value and "://" not in value
                assert len(value) <= telemetry_module.MAX_ATTRIBUTE_CHARS


def test_span_attribute_keys_are_exactly_the_documented_set():
    """A new attribute must be a deliberate, reviewed addition rather than an accident."""
    recorder = RecordingTelemetry()
    telemetry = recorder.telemetry
    with telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        with telemetry.attempt_span(attempt_number=1) as attempt:
            with telemetry.phase_span(
                phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER, model="m", effort="high"
            ) as phase:
                phase.set_many(phase_usage_attributes({"input_tokens": 1, "output_tokens": 1}))
                phase.set_outcome(OUTCOME_OK)
            attempt.set_outcome(OUTCOME_COMPLETED)
            attempt.set(ATTR_ATTEMPT_TERMINATED_BY, "completion")
        task.set_outcome(OUTCOME_COMPLETED)

    keys = {key for span in recorder.spans() for key in span.attributes}
    assert keys == {
        ATTR_TASK_ID,
        "forge.project.id",
        ATTR_TASK_RISK,
        "forge.task.max_attempts",
        ATTR_TASK_OUTCOME,
        ATTR_ATTEMPT_NUMBER,
        ATTR_ATTEMPT_KIND,
        ATTR_RETRY_BUCKET,
        ATTR_ATTEMPT_OUTCOME,
        ATTR_ATTEMPT_TERMINATED_BY,
        ATTR_PHASE_NAME,
        ATTR_PHASE_CYCLE,
        ATTR_AGENT_ROLE,
        "gen_ai.request.model",
        "gen_ai.request.reasoning_effort",
        ATTR_PHASE_OUTCOME,
        ATTR_PHASE_DURATION_MS,
        ATTR_INPUT_TOKENS,
        ATTR_CACHED_INPUT_TOKENS,
        ATTR_FRESH_INPUT_TOKENS,
        ATTR_OUTPUT_TOKENS,
        ATTR_REASONING_TOKENS,
        ATTR_RAW_TOTAL_TOKENS,
        ATTR_CACHE_HIT_RATIO,
        ATTR_ACCOUNTING_BASIS,
        ATTR_BUDGET_BASIS,
    }
