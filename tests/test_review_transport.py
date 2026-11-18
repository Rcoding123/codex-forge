"""The review transport must be executable, and its failure must never read as a code defect.

Live acceptance run fw5a (task SC-R1-c12b3cbc38cb6cd526266c8e, 2026-08-17) implemented nothing and
still exposed two defects in the review boundary at 966cb29:

1. `review-result.schema.json` declared `binding_requirements` in `properties` without listing it
   in top-level `required`. The provider validates the `--output-schema` document in strict mode
   before any model answer, so every review request was refused with 400 `invalid_json_schema`
   ("'required' is required to be supplied and to be an array including every key in properties.
   Missing 'binding_requirements'."). No review could ever complete.
2. That transport failure surfaced as a generic `RuntimeError`, was retried into two identical
   failures, and terminally classified `LOOP_DETECTED` — an implementation-shaped failure — even
   though the reviewer never said anything about the candidate. Forge already owns the correct
   class (`REVIEW_INFRASTRUCTURE_BLOCKED` / `review_infrastructure_blocked`); loop detection erased
   it.

Part 1 validates the exact schemas the runner passes to `codex exec --output-schema` against the
STRUCTURAL strict rules (`tests/strict_schema.py` states its exact scope), with
`binding_requirements` required in the review OUTPUT while an empty `[]` stays valid for tasks
with no binding fields, and statically pins the schema documents inside the most restrictive
numeric limits the provider has documented. Part 2 replays the exact fw5a failure shape against
the controller and pins the terminal behavior: never COMPLETED, never an implementation-failure
classification, bounded retry preserved, raw failure evidence retained in its original artifacts,
and no repair phase — there is no semantic review verdict to repair from. Part 3 proves the
provider-text law with no trust exemption: no provider-generated string — the structured `code`,
`type` and `param` fields exactly as much as the free-form message — is ever copied into durable
state. The raw event/stderr artifacts are the only owners of verbatim provider output;
`review-infrastructure-block.json` carries only controller facts (reason, exit code, numeric HTTP
status, evidence paths) and `last_error` carries only the deterministic controller summary
(classification, exit code, numeric status). Part 4 proves failure identity is autonomous and
controller-owned: the identical-failure fingerprint is built solely from the canonical controller
tuple (phase, reason, exit code, numeric HTTP status), so arbitrary provider codes, request IDs,
secrets, paths and other provider prose can neither split one failure class into many nor defeat
bounded loop detection — while genuinely identical failures still trip it — and a provider error
Forge has never seen before is bounded automatically, with no operator-maintained certification
table or allowlist anywhere. Part 5 proves the trusted-review boundary for the exit-0 leg: a
semantic review exists if and only if the run's raw final output decodes and validates into a
ReviewResult, so missing, empty, malformed or schema-invalid exit-0 output is classified
`review_output_unusable` under the same `REVIEW_INFRASTRUCTURE_BLOCKED` ownership — no semantic
blocking_reason, no repair, a controller-owned kind as the only new identity fact, and the raw
attempt artifacts keeping sole custody of the unusable bytes.

Part 6 closes the gap W3R2 exposed between those two checks. That review launched the full gate,
its own event stream never recorded an outcome for it, and it answered `pass` anyway: the
launch-dead detector reads only `item.completed` events and never saw the abandoned command, while
the decoder validates only the final bytes and passed a well-formed report. So a schema-valid
verdict resting on evidence the run never produced was trusted. Admission now also requires the
run's own stream to account for every command it launched — refused as `commands_unclosed` under
the same bounded `REVIEW_INFRASTRUCTURE_BLOCKED` ownership, verdict-agnostically, so an
unobserved gate can neither certify conformance nor launch paid repair against phantom findings.
The rule is one-directional and evidential: an unobserved *outcome* impeaches, an unobserved
launch does not, and closure of the recorded stream is never a claim about operating-system
process exit.

**No test in this file makes a model request, and none validates against the live provider.**
"""

from __future__ import annotations

import json
from pathlib import Path

from codex_forge.schema_resources import load_schema as load_packaged_schema, schema_file
from codex_forge.control_plane import _safe_task_error
from codex_forge.models import CodexRunResult, TaskState
from codex_forge.orchestrator import (
    REVIEW_INFRASTRUCTURE_BLOCK_FILENAME,
    REVIEW_INFRASTRUCTURE_BLOCKED,
)
from codex_forge.runner import (
    BLOCK_COMMANDS_NEVER_EXECUTED,
    BLOCK_REVIEW_TRANSPORT_FAILED,
    REVIEW_OUTPUT_COMMANDS_UNCLOSED,
    REVIEW_OUTPUT_INVALID_JSON,
    REVIEW_OUTPUT_MISSING,
    REVIEW_OUTPUT_SCHEMA_INVALID,
    BLOCK_REVIEW_OUTPUT_UNUSABLE,
    _MAX_RECORDED_COMMAND_CHARS,
    _MAX_RECORDED_COMMANDS,
    decode_review_result,
    review_command_closure,
    transport_http_status,
)
from codex_forge.telemetry import OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
from strict_schema import instance_violations, schema_metrics, structural_strict_schema_violations
from test_review_infrastructure import (
    BLOCKED_REVIEW_EVENTS,
    CANARY_BLOCKED_FINAL,
    COMPLETED_IMPLEMENTATION_FINAL,
    GENUINE_CHANGES_REQUIRED_FINAL,
    INSPECTED_REVIEW_EVENTS,
    PASSING_REVIEW_FINAL,
    RecordingRunner,
    build_orchestrator,
)


# --------------------------------------------------------------------------------------------
# Part 1 - the transported output schemas satisfy strict structured-output rules
# --------------------------------------------------------------------------------------------

# Resolve the exact packaged bytes used by both transport and controller decoding.
TRANSPORTED_SCHEMAS = ("task-result.schema.json", "review-result.schema.json")

REVIEW_REPORT_KEYS = {"verdict", "summary", "acceptance_criteria", "binding_requirements", "findings"}


def load_schema(name: str) -> dict:
    with schema_file(name) as path:
        schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema == load_packaged_schema(name)
    return schema


def test_every_transported_output_schema_is_structurally_strict():
    for name in TRANSPORTED_SCHEMAS:
        assert structural_strict_schema_violations(load_schema(name)) == [], name


# The most restrictive numeric limits the provider has documented for strict structured outputs
# (its original launch contract): 100 object properties total, 5 levels of nesting, 500 enum
# values, and 15,000 characters of property/definition/enum strings. Later contract revisions only
# raised these budgets, so a schema inside the original envelope is inside every revision — and
# `schema_metrics` overcounts nesting relative to the provider's object-nesting measure, keeping
# the check conservative. This is a static budget check on the schema documents Forge ships,
# not live-provider certification.
MOST_RESTRICTIVE_DOCUMENTED_LIMITS = {
    "max_nesting_depth": 5,
    "total_properties": 100,
    "total_enum_values": 500,
    "total_string_chars": 15_000,
}


def test_transported_schemas_fit_the_most_restrictive_documented_limits():
    for name in TRANSPORTED_SCHEMAS:
        metrics = schema_metrics(load_schema(name))
        for key, limit in MOST_RESTRICTIVE_DOCUMENTED_LIMITS.items():
            assert metrics[key] <= limit, (name, key, metrics)


def test_review_result_required_covers_every_property():
    schema = load_schema("review-result.schema.json")
    assert set(schema["properties"]) == REVIEW_REPORT_KEYS
    # binding_requirements is required, and verdict/summary/acceptance_criteria/findings remain so.
    assert set(schema["required"]) == REVIEW_REPORT_KEYS


def test_the_exact_966cb29_defect_is_caught():
    """A property present in `properties` but absent from `required` must be a violation."""
    schema = load_schema("review-result.schema.json")
    schema["required"] = [key for key in schema["required"] if key != "binding_requirements"]
    violations = structural_strict_schema_violations(schema)
    assert any("binding_requirements" in item and "required" in item for item in violations)


def test_the_defect_class_is_caught_in_nested_objects_too():
    """The rule is recursive, not a one-off assertion about one field name at the top level."""
    schema = load_schema("review-result.schema.json")
    items = schema["properties"]["binding_requirements"]["items"]
    items["required"] = [key for key in items["required"] if key != "evidence"]
    violations = structural_strict_schema_violations(schema)
    assert any("evidence" in item and "binding_requirements" in item for item in violations)


CONFORMING_REVIEW_RESULT = {
    "verdict": "pass",
    "summary": "The candidate satisfies the whole contract.",
    "acceptance_criteria": [
        {"criterion": "Every supported level is accepted.", "met": True, "evidence": "pytest -q: 4 passed"}
    ],
    "binding_requirements": [
        {
            "text": "Supported levels are low, medium, and high.",
            "met": True,
            "evidence": "triage.py defines exactly those levels; tests exercise all three.",
        }
    ],
    "findings": [
        {
            "severity": "low",
            "file": None,
            "line": None,
            "issue": "Optional observation with no blocking effect.",
            "required_fix": "None required.",
        }
    ],
}


def test_a_fully_conforming_review_result_is_schema_valid():
    schema = load_schema("review-result.schema.json")
    assert instance_violations(CONFORMING_REVIEW_RESULT, schema) == []


def test_empty_binding_requirements_is_schema_valid():
    """A task with no decisions/constraints reports `binding_requirements: []` — required, empty."""
    schema = load_schema("review-result.schema.json")
    report = {**CONFORMING_REVIEW_RESULT, "acceptance_criteria": [], "binding_requirements": [], "findings": []}
    assert instance_violations(report, schema) == []


def test_a_report_omitting_binding_requirements_is_schema_invalid():
    """The field is required in the structured OUTPUT — emptiness, not absence, is the compat shape."""
    schema = load_schema("review-result.schema.json")
    report = {key: value for key, value in CONFORMING_REVIEW_RESULT.items() if key != "binding_requirements"}
    assert any("binding_requirements" in item for item in instance_violations(report, schema))


# --------------------------------------------------------------------------------------------
# Part 2 - the fw5a failure shape: provider rejects the review request, twice
# --------------------------------------------------------------------------------------------

FW5A_SCHEMA_REJECTION = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_json_schema",
            "message": (
                "Invalid schema for response_format 'codex_output_schema': In context=(), "
                "'required' is required to be supplied and to be an array including every key "
                "in properties. Missing 'binding_requirements'."
            ),
            "param": "text.format.schema",
        },
        "status": 400,
    },
    indent=2,
)


class TransportFailingRunner(RecordingRunner):
    """Reviews fail exactly as fw5a's did: the provider refuses the request itself.

    For each phase named in `fail_phases`, the event stream carries the provider's error
    (`error_message`), stderr carries `stderr_text` (empty by default, as fw5a's was), no final
    file is created, and the CLI exits `exit_code` (1 by default, as fw5a's did). Every other phase behaves like the benign recorder.
    """

    def __init__(
        self,
        finals,
        resolution,
        fail_phases: set[str],
        *,
        error_message: str = FW5A_SCHEMA_REJECTION,
        stderr_text: str = "",
        exit_code: int = 1,
    ):
        super().__init__(finals, resolution)
        self.fail_phases = fail_phases
        self.error_message = error_message
        self.stderr_text = stderr_text
        self.exit_code = exit_code

    def run(self, *, prompt, cwd, artifact_dir, phase, model, effort, schema_name, sandbox=None):
        if phase not in self.fail_phases:
            return super().run(
                prompt=prompt,
                cwd=cwd,
                artifact_dir=artifact_dir,
                phase=phase,
                model=model,
                effort=effort,
                schema_name=schema_name,
                sandbox=sandbox,
            )
        self.phases.append(phase)
        self.sandboxes.append(sandbox)
        directory = Path(artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        events = [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "error", "message": self.error_message}),
            json.dumps({"type": "turn.failed", "error": {"message": self.error_message}}),
        ]
        jsonl = directory / f"{phase}.events.jsonl"
        jsonl.write_text("\n".join(events) + "\n", encoding="utf-8")
        stderr = directory / f"{phase}.stderr.log"
        stderr.write_text(self.stderr_text, encoding="utf-8")
        return CodexRunResult(
            exit_code=self.exit_code,
            final={
                "status": "blocked",
                "summary": "Codex did not create a final result file.",
                "blocking_reason": "",
                "changed_files": [],
                "validation_performed": [],
                "assumptions": [],
                "decisions_needed": [],
                "reusable_patterns": [],
            },
            jsonl_path=jsonl,
            # Never created: the provider refused the request before any model answer.
            final_path=directory / f"{phase}.final.json",
            stderr_path=stderr,
            thread_id="thread-1",
            usage={"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 0},
        )


def build_transport_failing(
    tmp_path, monkeypatch, *, finals=None, fail_phases=None, error_message=None, stderr_text=""
):
    orchestrator, db, recorder, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals=finals or {"implementation": (COMPLETED_IMPLEMENTATION_FINAL, [])},
    )
    runner = TransportFailingRunner(
        recorder.finals,
        recorder.resolution,
        fail_phases or {"review"},
        error_message=error_message if error_message is not None else FW5A_SCHEMA_REJECTION,
        stderr_text=stderr_text,
    )
    orchestrator.runner = runner
    return orchestrator, db, runner, task


def reclaim_for_next_attempt(db, task) -> None:
    """Apply the recorded durable updates and the claim semantics the real DB would apply."""
    for _, fields in db.updates:
        for key in ("failure_fingerprint", "identical_failures", "last_error"):
            if key in fields:
                setattr(task, key, fields[key])
    task.attempts += 1
    task.state = TaskState.IMPLEMENTING


def never_completed(db) -> bool:
    return all(fields.get("state") != TaskState.COMPLETED for _, fields in db.updates)


def block_path(tmp_path, task, attempt: int) -> Path:
    return (
        tmp_path
        / "evidence"
        / "canary"
        / task.id
        / f"attempt-{attempt:02d}"
        / REVIEW_INFRASTRUCTURE_BLOCK_FILENAME
    )


def block_record(tmp_path, task, attempt: int) -> dict:
    path = block_path(tmp_path, task, attempt)
    assert path.is_file(), f"missing block record: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_rejected_review_request_is_classified_infrastructure_not_implementation(
    tmp_path, monkeypatch
):
    orchestrator, db, runner, task = build_transport_failing(tmp_path, monkeypatch)

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    # Bounded retry is preserved: first failure within attempt/identical-failure bounds retries.
    assert db.state == TaskState.READY
    assert db.last_error.startswith(f"{REVIEW_INFRASTRUCTURE_BLOCKED}: ")
    # No repair phase: no semantic review verdict exists to repair from.
    assert runner.phases == ["implementation", "review"]
    assert not [phase for phase in runner.phases if "repair" in phase]
    # The provider's own rejection remains available, verbatim, in its one raw owner: the event
    # log the record points at. The durable record itself carries only controller facts.
    record = block_record(tmp_path, task, attempt=1)
    assert record["error_code"] == REVIEW_INFRASTRUCTURE_BLOCKED
    assert record["reason"] == BLOCK_REVIEW_TRANSPORT_FAILED
    assert record["detail"].startswith("review transport failed; exit_code=1; status=400")
    assert record["http_status"] == 400
    assert "provider" not in record
    events_path = Path(record["evidence"]["events"])
    events_text = events_path.read_text(encoding="utf-8")
    assert events_path.is_file()
    # No provider string — not even the machine-shaped error code — is duplicated out of the raw
    # event log into the durable record or last_error.
    block_text = block_path(tmp_path, task, attempt=1).read_text(encoding="utf-8")
    for provider_string in (
        "invalid_json_schema",
        "invalid_request_error",
        "text.format.schema",
        "'required' is required to be supplied",
    ):
        assert provider_string in events_text
        assert provider_string not in block_text
        assert provider_string not in db.last_error


def test_the_fw5a_terminal_shape_retry_same_failure_stays_infrastructure(tmp_path, monkeypatch):
    """attempt review -> provider rejection -> retry -> identical rejection -> terminal.

    Loop detection still bounds the retrying, but it no longer erases the owning failure class:
    the terminal task reads `REVIEW_INFRASTRUCTURE_BLOCKED`, never a LOOP_DETECTED-classified
    implementation failure, and is never COMPLETED.
    """
    orchestrator, db, runner, task = build_transport_failing(tmp_path, monkeypatch)

    first = orchestrator._execute_task_body(task)
    reclaim_for_next_attempt(db, task)
    second = orchestrator._execute_task_body(task)

    assert first == second == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert db.state == TaskState.FAILED
    assert never_completed(db)
    # The class owns the prefix; loop detection contributes information, not classification.
    assert db.last_error.startswith(f"{REVIEW_INFRASTRUCTURE_BLOCKED}: ")
    assert "LOOP_DETECTED after 2 identical failures" in db.last_error
    code, message = _safe_task_error(db.last_error)
    assert code == REVIEW_INFRASTRUCTURE_BLOCKED
    assert code != "LOOP_DETECTED"
    assert "environment fault" in message
    # Both attempts kept their evidence; neither started a repair phase.
    assert runner.phases == ["implementation", "review", "implementation", "review"]
    assert not [phase for phase in runner.phases if "repair" in phase]
    assert block_record(tmp_path, task, attempt=1)["reason"] == BLOCK_REVIEW_TRANSPORT_FAILED
    assert block_record(tmp_path, task, attempt=2)["reason"] == BLOCK_REVIEW_TRANSPORT_FAILED


def test_a_transport_failure_in_the_re_review_is_classified_the_same_way(tmp_path, monkeypatch):
    """A genuine verdict starts the repair loop; the re-review's transport failure is still
    an environment fault, not a second finding about the candidate."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (GENUINE_CHANGES_REQUIRED_FINAL, INSPECTED_REVIEW_EVENTS),
        },
        fail_phases={"review-2"},
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert db.last_error.startswith(f"{REVIEW_INFRASTRUCTURE_BLOCKED}: ")
    assert never_completed(db)
    # The genuine verdict was allowed its repair; the transport failure ended the attempt after it.
    assert runner.phases == ["implementation", "review", "review-repair-1", "review-2"]


def test_transport_http_status_is_the_only_fact_lifted_from_the_provider_error(
    tmp_path, monkeypatch
):
    """stderr was empty for the fw5a rejection; the provider's statement lives in the events.

    The controller lifts exactly one fact out of it — the numeric HTTP status — and leaves every
    provider string behind in the raw log, which stays its only owner."""
    orchestrator, db, runner, task = build_transport_failing(tmp_path, monkeypatch)
    result = runner.run(
        prompt="p",
        cwd=tmp_path,
        artifact_dir=tmp_path / "probe",
        phase="review",
        model="m",
        effort="medium",
        schema_name="review-result.schema.json",
    )
    assert transport_http_status(result) == 400


def status_probe(tmp_path, message: str | None) -> CodexRunResult:
    """A minimal refused-run shape whose event stream carries `message` as its error event."""
    directory = tmp_path / "status-probe"
    directory.mkdir(parents=True, exist_ok=True)
    events = [json.dumps({"type": "thread.started", "thread_id": "t"})]
    if message is not None:
        events.append(json.dumps({"type": "error", "message": message}))
    jsonl = directory / "review.events.jsonl"
    jsonl.write_text("\n".join(events) + "\n", encoding="utf-8")
    stderr = directory / "review.stderr.log"
    stderr.write_text("", encoding="utf-8")
    return CodexRunResult(
        exit_code=1,
        final={},
        jsonl_path=jsonl,
        final_path=directory / "review.final.json",
        stderr_path=stderr,
        thread_id="t",
        usage={},
    )


def test_transport_http_status_admits_only_json_integers(tmp_path):
    """A status is a controller fact only when it is numerically typed: strings, booleans and
    absent or unstructured messages all yield None rather than smuggling provider text through
    the numeric slot."""
    assert transport_http_status(status_probe(tmp_path, json.dumps({"status": 429}))) == 429
    for message in (
        json.dumps({"status": "400"}),  # string-typed status is provider text, not a fact
        json.dumps({"status": True}),  # bool is an int subclass; still not a status
        json.dumps({"error": {"code": "x"}}),  # no status at all
        json.dumps(["status", 400]),  # not an object
        "transport wedged before any request was issued",  # unstructured prose
        None,  # no error event
    ):
        assert transport_http_status(status_probe(tmp_path, message)) is None


# --------------------------------------------------------------------------------------------
# Part 3 - secret-leak regressions: raw artifacts keep evidence, durable summaries never do
# --------------------------------------------------------------------------------------------

SECRET_VALUE = "sk-livekey-LEAKCANARY-0123456789abcdefghij"
SECRET_HEADER = f"Authorization: Bearer {SECRET_VALUE}"

SECRET_BEARING_REJECTION = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_api_key",
            "message": f"Incorrect API key provided: {SECRET_VALUE}. {SECRET_HEADER}",
            "param": None,
        },
        "status": 401,
    }
)

UNSTRUCTURED_SECRET_FAILURE = f"""transport wedged before any request was issued
{SECRET_HEADER}
api_key={SECRET_VALUE}"""


def test_a_secret_in_the_provider_rejection_never_reaches_durable_summaries(tmp_path, monkeypatch):
    """A structured rejection whose free-form message carries credential material.

    The message is never copied out of the raw event log, so the block record and `last_error`
    stay clean by construction while the numeric status and the owning classification survive."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=SECRET_BEARING_REJECTION
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    record = block_record(tmp_path, task, attempt=1)
    # Raw evidence law: the event artifact keeps the provider's original statement, verbatim.
    assert SECRET_VALUE in Path(record["evidence"]["events"]).read_text(encoding="utf-8")
    # Durable summaries never copy it.
    assert SECRET_VALUE not in block_path(tmp_path, task, attempt=1).read_text(encoding="utf-8")
    assert SECRET_VALUE not in db.last_error
    # Only controller facts and the owning classification survive: no provider metadata at all.
    assert "provider" not in record
    assert "invalid_api_key" not in block_path(tmp_path, task, attempt=1).read_text(encoding="utf-8")
    assert record["http_status"] == 401
    assert record["reason"] == BLOCK_REVIEW_TRANSPORT_FAILED
    assert record["error_code"] == REVIEW_INFRASTRUCTURE_BLOCKED
    assert db.last_error.startswith(f"{REVIEW_INFRASTRUCTURE_BLOCKED}: ")


def test_loop_detection_detail_stays_safe_when_the_secret_bearing_failure_repeats(
    tmp_path, monkeypatch
):
    """Loop detection may add information to the terminal error, never leaked provider text."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=SECRET_BEARING_REJECTION
    )

    orchestrator._execute_task_body(task)
    reclaim_for_next_attempt(db, task)
    orchestrator._execute_task_body(task)

    assert db.state == TaskState.FAILED
    assert db.last_error.startswith(f"{REVIEW_INFRASTRUCTURE_BLOCKED}: ")
    assert "LOOP_DETECTED after 2 identical failures" in db.last_error
    assert SECRET_VALUE not in db.last_error
    for attempt in (1, 2):
        assert SECRET_VALUE not in block_path(tmp_path, task, attempt).read_text(encoding="utf-8")
        events = Path(block_record(tmp_path, task, attempt)["evidence"]["events"])
        assert SECRET_VALUE in events.read_text(encoding="utf-8")


def test_an_unstructured_transport_failure_leaves_no_provider_text_in_durable_state(
    tmp_path, monkeypatch
):
    """When no structured provider error exists there is no excerpt fallback: the durable record
    still carries only controller facts, and the raw artifacts alone own the failure text."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path,
        monkeypatch,
        error_message=UNSTRUCTURED_SECRET_FAILURE,
        stderr_text=UNSTRUCTURED_SECRET_FAILURE,
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    record = block_record(tmp_path, task, attempt=1)
    # Both raw owners keep the original, secrets included.
    assert SECRET_VALUE in Path(record["evidence"]["events"]).read_text(encoding="utf-8")
    assert SECRET_VALUE in Path(record["evidence"]["stderr"]).read_text(encoding="utf-8")
    # The durable record copies none of it — not even a redacted excerpt.
    block_text = block_path(tmp_path, task, attempt=1).read_text(encoding="utf-8")
    assert "transport wedged" not in block_text
    assert SECRET_VALUE not in block_text
    assert record["detail"] == (
        "review transport failed; exit_code=1. The provider's verbatim output is retained in "
        "the attempt's review event log and stderr log."
    )
    assert "http_status" not in record
    assert SECRET_VALUE not in db.last_error
    assert db.last_error.startswith(f"{REVIEW_INFRASTRUCTURE_BLOCKED}: ")


def latest_fingerprint(db) -> str | None:
    for _, fields in reversed(db.updates):
        if "failure_fingerprint" in fields:
            return fields["failure_fingerprint"]
    return None


def secret_field_rejection(secret: str) -> str:
    """A rejection whose every structured field carries credential material."""
    return json.dumps(
        {
            "type": "error",
            "error": {
                # Nothing guarantees these keys hold short machine tokens; each one here carries
                # credential material in a different secret shape.
                "type": f"provider_fault Bearer {secret}",
                "code": f"rejected_key_{secret}",
                "message": "Request rejected.",
                "param": f"api_key={secret}",
            },
            "status": 401,
        }
    )


SECRET_FIELD_REJECTION = secret_field_rejection(SECRET_VALUE)


def test_secrets_in_structured_provider_fields_never_reach_durable_state(tmp_path, monkeypatch):
    """Code, type and param get no trust exemption: they are not persisted at all.

    A secret embedded in any structured provider field survives only in the raw event log; the
    block record carries no provider field values in any form, and `last_error` carries only the
    deterministic controller summary."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=SECRET_FIELD_REJECTION
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    record = block_record(tmp_path, task, attempt=1)
    # Raw evidence law: the event artifact keeps all three tainted fields verbatim.
    assert Path(record["evidence"]["events"]).read_text(encoding="utf-8").count(SECRET_VALUE) >= 3
    # The block JSON as a whole carries none of them - no field survives, redacted or otherwise.
    block_text = block_path(tmp_path, task, attempt=1).read_text(encoding="utf-8")
    assert SECRET_VALUE not in block_text
    for field_fragment in ("rejected_key_", "provider_fault", "api_key="):
        assert field_fragment not in block_text
    assert "provider" not in record
    assert record["http_status"] == 401
    # last_error is exactly the deterministic controller summary.
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review transport failed; exit_code=1; status=401"
    )


def test_loop_detected_stays_safe_when_structured_fields_carry_secrets(tmp_path, monkeypatch):
    """Regression G: the terminal LOOP_DETECTED detail appends no provider text, tainted or not."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=SECRET_FIELD_REJECTION
    )

    orchestrator._execute_task_body(task)
    reclaim_for_next_attempt(db, task)
    orchestrator._execute_task_body(task)

    assert db.state == TaskState.FAILED
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: LOOP_DETECTED after 2 identical failures.\n"
        "review transport failed; exit_code=1; status=401"
    )
    assert SECRET_VALUE not in db.last_error


def test_last_error_is_the_deterministic_controller_summary(tmp_path, monkeypatch):
    """Even the fw5a rejection's real, stable-looking code stays out: `last_error` is built from
    classification, exit code and numeric status alone."""
    orchestrator, db, runner, task = build_transport_failing(tmp_path, monkeypatch)

    orchestrator._execute_task_body(task)

    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review transport failed; exit_code=1; status=400"
    )
    assert "invalid_json_schema" not in db.last_error


# --------------------------------------------------------------------------------------------
# Part 4 - failure identity: the fingerprint is built from controller facts, never provider text
# --------------------------------------------------------------------------------------------


def volatile_structured_rejection(request_id: str, temp_path: str) -> str:
    """One stable failure class (`invalid_json_schema`, 400, exit 1) in volatile clothing."""
    return json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_json_schema",
                "message": f"Schema at {temp_path} rejected; request {request_id}; key {SECRET_VALUE}",
                "param": f"{temp_path}#request={request_id}",
            },
            "status": 400,
        }
    )


def test_volatile_paths_and_ids_do_not_alter_the_failure_fingerprint(tmp_path, monkeypatch):
    """Regressions H and J: identical stable class, differing temp paths / request IDs / secrets.

    The two attempts must fingerprint identically - anything else lets volatile provider text
    defeat identical-failure detection - and the bounded loop must therefore still terminate."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path,
        monkeypatch,
        error_message=volatile_structured_rejection(
            "req_0a1b2c", "C:\\Temp\\forge-aa11\\schema.json"
        ),
    )

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    runner.error_message = volatile_structured_rejection(
        "req_9z8y7x", "C:\\Temp\\forge-zz99\\schema.json"
    )
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)

    assert first is not None and first == second
    assert db.state == TaskState.FAILED
    assert "LOOP_DETECTED after 2 identical failures" in db.last_error
    assert db.last_error.startswith(f"{REVIEW_INFRASTRUCTURE_BLOCKED}: ")


def test_unstructured_failure_prose_is_never_a_fingerprint_input(tmp_path, monkeypatch):
    """Two unstructured failures whose raw texts differ still fingerprint the same.

    The raw artifacts prove the texts really differed; the equal fingerprints prove no prose is
    identity, and the loop still terminates on the shared controller failure class."""
    first_text = "transport wedged\npath C:\\Temp\\forge-aa11\\review req_id=req-11111"
    second_text = "transport wedged\npath C:\\Temp\\forge-zz99\\review req_id=req-99999"
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=first_text, stderr_text=first_text
    )

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    first_events = Path(block_record(tmp_path, task, attempt=1)["evidence"]["events"]).read_text(
        encoding="utf-8"
    )
    reclaim_for_next_attempt(db, task)
    runner.error_message = second_text
    runner.stderr_text = second_text
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)
    second_events = Path(block_record(tmp_path, task, attempt=2)["evidence"]["events"]).read_text(
        encoding="utf-8"
    )

    assert "req-11111" in first_events and "req-99999" in second_events
    assert first is not None and first == second
    assert db.state == TaskState.FAILED
    assert "LOOP_DETECTED after 2 identical failures" in db.last_error
    # The volatile prose stays out of the durable record and the terminal error alike.
    assert "req-11111" not in db.last_error and "req-99999" not in db.last_error
    for attempt in (1, 2):
        block_text = block_path(tmp_path, task, attempt).read_text(encoding="utf-8")
        assert "transport wedged" not in block_text
        assert "req-11111" not in block_text and "req-99999" not in block_text


def test_no_operator_certification_mechanism_exists():
    """The rejected design is gone: no certified-code table, no summary type carrying provider
    strings toward durable state, no per-field sanitizer to maintain. Future provider failures
    classify autonomously, so there is deliberately nothing here for an operator to update."""
    import codex_forge.runner as runner_module

    for removed in (
        "CERTIFIED_STABLE_PROVIDER_CODES",
        "TransportFailureSummary",
        "transport_failure_summary",
        "_provider_error_fields",
        "_sanitize_provider_field",
    ):
        assert not hasattr(runner_module, removed), removed
    source = Path(runner_module.__file__).read_text(encoding="utf-8")
    assert "CERTIFIED" not in source
    assert "stable_code" not in source


def test_structural_helper_makes_no_keyword_support_claim():
    """Regression K: the helper's scope is structural closure only.

    The repo holds no authoritative allowlist of provider-supported schema keywords, so the
    helper claims nothing about keyword support: a keyword outside its structural scope - here
    `oneOf` - passes structurally, and only the live transport can refuse it (Part 2 owns that
    refusal's classification). The old partial denylist overclaimed and is gone."""
    import strict_schema

    assert not hasattr(strict_schema, "_UNSUPPORTED_KEYWORDS")
    schema = load_schema("review-result.schema.json")
    schema["properties"]["verdict"] = {"oneOf": [{"type": "string"}, {"type": "null"}]}
    assert structural_strict_schema_violations(schema) == []


CREDENTIAL_SHAPED_CODE = "api-key-SUPERSECRETVALUE123456789"


def code_bearing_rejection(code: str) -> str:
    """A structured rejection whose `error.code` is attacker- or volatility-shaped."""
    return json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": code,
                "message": "Request rejected.",
                "param": "text.format.schema",
            },
            "status": 401,
        }
    )


def test_credential_shaped_provider_code_never_enters_durable_summaries(tmp_path, monkeypatch):
    """A code no redactor could recognize as a secret stays out of every durable surface -
    `last_error`, the LOOP_DETECTED detail, and the block record itself - because provider
    strings are never persisted at all."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=code_bearing_rejection(CREDENTIAL_SHAPED_CODE)
    )

    orchestrator._execute_task_body(task)
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review transport failed; exit_code=1; status=401"
    )
    assert CREDENTIAL_SHAPED_CODE not in db.last_error

    reclaim_for_next_attempt(db, task)
    orchestrator._execute_task_body(task)
    assert db.state == TaskState.FAILED
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: LOOP_DETECTED after 2 identical failures.\n"
        "review transport failed; exit_code=1; status=401"
    )
    assert CREDENTIAL_SHAPED_CODE not in db.last_error
    # The raw event log is the value's ONLY owner: the durable block record - provider
    # metadata, detail and summary included - never carries it in any form.
    for attempt in (1, 2):
        record = block_record(tmp_path, task, attempt)
        assert CREDENTIAL_SHAPED_CODE in Path(record["evidence"]["events"]).read_text(
            encoding="utf-8"
        )
        assert CREDENTIAL_SHAPED_CODE not in block_path(tmp_path, task, attempt).read_text(
            encoding="utf-8"
        )
        assert "provider" not in record


def test_request_id_shaped_codes_do_not_split_the_failure_fingerprint(tmp_path, monkeypatch):
    """Two rejections identical in stable class whose `error.code` is a differing request-ID-shaped
    token must fingerprint identically: an uncertified code is not identity, so volatile codes can
    neither defeat identical-failure detection nor appear in the terminal error."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=code_bearing_rejection("req_0a1b2c3d4e5f")
    )

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    runner.error_message = code_bearing_rejection("req_9z8y7x6w5v4u")
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)

    assert first is not None and first == second
    assert db.state == TaskState.FAILED
    assert "LOOP_DETECTED after 2 identical failures" in db.last_error
    assert "req_0a1b2c3d4e5f" not in db.last_error and "req_9z8y7x6w5v4u" not in db.last_error


def rejection_with_status(status: int) -> str:
    """The same provider-authored content behind a different numeric HTTP status."""
    return json.dumps(
        {
            "type": "error",
            "error": {
                "type": "provider_error",
                "code": "some_provider_code",
                "message": "Request refused.",
                "param": None,
            },
            "status": status,
        }
    )


def test_arbitrary_future_provider_codes_need_no_maintenance_to_bound_retries(
    tmp_path, monkeypatch
):
    """A provider error Forge has never seen classifies autonomously: two rejections with novel,
    differing codes but the same controller-observable class share one fingerprint, the bounded
    loop terminates, and no durable surface waits on anyone to certify anything."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=code_bearing_rejection("entirely_new_code_2027")
    )

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    runner.error_message = code_bearing_rejection("another_unheard_of_failure")
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)

    assert first is not None and first == second
    assert db.state == TaskState.FAILED
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: LOOP_DETECTED after 2 identical failures.\n"
        "review transport failed; exit_code=1; status=401"
    )
    for attempt in (1, 2):
        block_text = block_path(tmp_path, task, attempt).read_text(encoding="utf-8")
        assert "entirely_new_code_2027" not in block_text
        assert "another_unheard_of_failure" not in block_text


def test_differing_secret_values_do_not_split_the_failure_fingerprint(tmp_path, monkeypatch):
    """Two rejections identical in controller class whose fields carry different secrets must
    fingerprint identically - secrets are not identity - and neither secret goes durable."""
    other_secret = "sk-livekey-OTHERCANARY-9876543210zyxwvutsrq"
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=secret_field_rejection(SECRET_VALUE)
    )

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    runner.error_message = secret_field_rejection(other_secret)
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)

    assert first is not None and first == second
    assert db.state == TaskState.FAILED
    for secret in (SECRET_VALUE, other_secret):
        assert secret not in db.last_error
    for attempt in (1, 2):
        block_text = block_path(tmp_path, task, attempt).read_text(encoding="utf-8")
        assert SECRET_VALUE not in block_text and other_secret not in block_text


def test_a_different_exit_code_is_a_different_failure_class(tmp_path, monkeypatch):
    """Controller facts ARE identity: the same rejection behind a different process exit code
    must not read as the identical failure."""
    orchestrator, db, runner, task = build_transport_failing(tmp_path, monkeypatch)

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    runner.exit_code = 2
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)

    assert first is not None and second is not None and first != second


def test_a_different_http_status_is_a_different_failure_class(tmp_path, monkeypatch):
    """A 400 rejection and a 429 rate limit are different failure classes even though everything
    provider-authored about them is ignored."""
    orchestrator, db, runner, task = build_transport_failing(
        tmp_path, monkeypatch, error_message=rejection_with_status(400)
    )

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    runner.error_message = rejection_with_status(429)
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)

    assert first is not None and second is not None and first != second


# --------------------------------------------------------------------------------------------
# Part 5 - the exit-0 leg of the trusted-review boundary: unusable output is infrastructure
# --------------------------------------------------------------------------------------------
# The complementary positive laws live in the sibling suites and keep running against the same
# gate: a valid `changes_required` ReviewResult still enters the repair path
# (test_review_infrastructure.test_a_genuine_changes_required_verdict_still_enters_the_repair_path)
# and a valid conforming `pass` still completes
# (test_specification_conformance.test_a_conforming_pass_with_every_criterion_met_completes,
# test_binding_contract.test_a_conforming_candidate_with_green_tests_still_completes).


class UnusableReviewOutputRunner(RecordingRunner):
    """Reviews whose process exits 0 but whose final output never becomes a ReviewResult.

    For each phase named in `unusable_phases` the event stream shows a really-executed command
    (the reviewer did inspect something, so the commands-never-executed detector stays quiet),
    stderr carries `stderr_text`, and the final file receives `final_bytes` verbatim — or is not
    created at all when that is None. The in-memory `final` mimics production `runner.run`
    exactly: parsed JSON when the bytes parse, otherwise a fabricated blocked-shaped dict
    carrying the raw text — which the controller must never consult for a review phase."""

    def __init__(self, finals, resolution, unusable_phases, *, final_bytes, stderr_text=""):
        super().__init__(finals, resolution)
        self.unusable_phases = unusable_phases
        self.final_bytes = final_bytes
        self.stderr_text = stderr_text

    def run(self, *, prompt, cwd, artifact_dir, phase, model, effort, schema_name, sandbox=None):
        if phase not in self.unusable_phases:
            return super().run(
                prompt=prompt,
                cwd=cwd,
                artifact_dir=artifact_dir,
                phase=phase,
                model=model,
                effort=effort,
                schema_name=schema_name,
                sandbox=sandbox,
            )
        self.phases.append(phase)
        self.sandboxes.append(sandbox)
        directory = Path(artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        events = [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "git diff master...HEAD",
                        "exit_code": 0,
                    },
                }
            ),
        ]
        jsonl = directory / f"{phase}.events.jsonl"
        jsonl.write_text("\n".join(events) + "\n", encoding="utf-8")
        stderr = directory / f"{phase}.stderr.log"
        stderr.write_text(self.stderr_text, encoding="utf-8")
        final_path = directory / f"{phase}.final.json"
        fabricated_tail = {
            "changed_files": [],
            "validation_performed": [],
            "assumptions": [],
            "decisions_needed": [],
            "reusable_patterns": [],
        }
        if self.final_bytes is None:
            fabricated = {
                "status": "blocked",
                "summary": "Codex did not create a final result file.",
                "blocking_reason": self.stderr_text[-12_000:],
                **fabricated_tail,
            }
        else:
            final_path.write_text(self.final_bytes, encoding="utf-8")
            try:
                fabricated = json.loads(self.final_bytes)
            except json.JSONDecodeError:
                fabricated = {
                    "status": "blocked",
                    "summary": "Codex returned a non-JSON final response.",
                    "blocking_reason": self.final_bytes,
                    **fabricated_tail,
                }
        return CodexRunResult(
            exit_code=0,
            final=fabricated,
            jsonl_path=jsonl,
            final_path=final_path,
            stderr_path=stderr,
            thread_id="thread-1",
            usage={"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 0},
        )


def build_unusable_output(
    tmp_path, monkeypatch, *, finals=None, unusable_phases=None, final_bytes=None, stderr_text=""
):
    orchestrator, db, recorder, task = build_orchestrator(
        tmp_path,
        monkeypatch,
        finals=finals or {"implementation": (COMPLETED_IMPLEMENTATION_FINAL, [])},
    )
    runner = UnusableReviewOutputRunner(
        recorder.finals,
        recorder.resolution,
        unusable_phases or {"review"},
        final_bytes=final_bytes,
        stderr_text=stderr_text,
    )
    orchestrator.runner = runner
    return orchestrator, db, runner, task


def test_exit_zero_with_missing_final_output_is_infrastructure_not_semantic(tmp_path, monkeypatch):
    """Regression A: exit 0, no final file. The fabricated in-memory final carries the stderr
    tail — secret included — and the controller must never consult it for a review phase."""
    orchestrator, db, runner, task = build_unusable_output(
        tmp_path, monkeypatch, final_bytes=None, stderr_text=f"reviewer wedged\n{SECRET_HEADER}"
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert db.state == TaskState.READY  # bounded retry, exactly like a transport refusal
    assert never_completed(db)
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review output unusable; "
        "kind=missing_output; exit_code=0"
    )
    assert not [phase for phase in runner.phases if "repair" in phase]
    record = block_record(tmp_path, task, attempt=1)
    assert record["error_code"] == REVIEW_INFRASTRUCTURE_BLOCKED
    assert record["reason"] == BLOCK_REVIEW_OUTPUT_UNUSABLE
    assert record["kind"] == REVIEW_OUTPUT_MISSING
    assert record["exit_codes"] == [0]
    assert "http_status" not in record
    # The stderr artifact keeps the raw text; the durable record and last_error never do.
    assert SECRET_VALUE in Path(record["evidence"]["stderr"]).read_text(encoding="utf-8")
    block_text = block_path(tmp_path, task, attempt=1).read_text(encoding="utf-8")
    assert SECRET_VALUE not in block_text and "reviewer wedged" not in block_text
    assert SECRET_VALUE not in db.last_error


def test_exit_zero_with_empty_final_output_is_infrastructure(tmp_path, monkeypatch):
    """Regression B: exit 0, final file exists but holds nothing."""
    orchestrator, db, runner, task = build_unusable_output(
        tmp_path, monkeypatch, final_bytes="  \n"
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert never_completed(db)
    assert not [phase for phase in runner.phases if "repair" in phase]
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review output unusable; "
        "kind=missing_output; exit_code=0"
    )
    assert block_record(tmp_path, task, attempt=1)["kind"] == REVIEW_OUTPUT_MISSING


def malformed_final(secret: str, request_id: str, temp_path: str) -> str:
    """Volatile non-JSON reviewer bytes: secrets, request IDs, paths, truncated structure."""
    return f'{{"verdict": "pass", "summary": "key {secret} req {request_id} at {temp_path}'


def test_malformed_final_output_with_secrets_never_reaches_durable_state(tmp_path, monkeypatch):
    """Regression C: exit 0 + non-JSON final carrying credential material and request IDs."""
    raw = malformed_final(SECRET_VALUE, "req_0a1b2c3d", "C:\\Temp\\forge-aa11\\review")
    orchestrator, db, runner, task = build_unusable_output(
        tmp_path, monkeypatch, final_bytes=raw, stderr_text=""
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert never_completed(db)
    assert not [phase for phase in runner.phases if "repair" in phase]
    record = block_record(tmp_path, task, attempt=1)
    assert record["reason"] == BLOCK_REVIEW_OUTPUT_UNUSABLE
    assert record["kind"] == REVIEW_OUTPUT_INVALID_JSON
    # Raw evidence law: the final artifact keeps the malformed bytes verbatim.
    assert SECRET_VALUE in Path(record["evidence"]["final"]).read_text(encoding="utf-8")
    # Durable state carries none of it.
    block_text = block_path(tmp_path, task, attempt=1).read_text(encoding="utf-8")
    for volatile in (SECRET_VALUE, "req_0a1b2c3d", "forge-aa11"):
        assert volatile not in block_text
        assert volatile not in db.last_error
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review output unusable; "
        "kind=invalid_json; exit_code=0"
    )


def test_schema_invalid_final_output_is_infrastructure_and_starts_no_repair(tmp_path, monkeypatch):
    """Regression D: exit 0 + well-formed JSON that fails the review-result schema.

    This is exactly the shape the old path read as semantics: a parseable dict with a verdict
    but without the full contract. It must classify as infrastructure and start no repair."""
    raw = json.dumps(
        {
            "verdict": "changes_required",
            "summary": "Half a report.",
            "findings": [],
            "debug_token": SECRET_VALUE,
        }
    )
    orchestrator, db, runner, task = build_unusable_output(tmp_path, monkeypatch, final_bytes=raw)

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert never_completed(db)
    # The parseable `changes_required` verdict must not have started the repair loop.
    assert not [phase for phase in runner.phases if "repair" in phase]
    record = block_record(tmp_path, task, attempt=1)
    assert record["kind"] == REVIEW_OUTPUT_SCHEMA_INVALID
    block_text = block_path(tmp_path, task, attempt=1).read_text(encoding="utf-8")
    assert SECRET_VALUE not in block_text and "Half a report." not in block_text
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review output unusable; "
        "kind=schema_invalid; exit_code=0"
    )


def test_differing_malformed_outputs_share_one_fingerprint_and_loop_terminates(
    tmp_path, monkeypatch
):
    """Regression E: two malformed outputs with different secrets, request IDs, paths and text
    are the same controller failure class, so bounded loop detection still terminates."""
    orchestrator, db, runner, task = build_unusable_output(
        tmp_path,
        monkeypatch,
        final_bytes=malformed_final(SECRET_VALUE, "req_11111", "C:\\Temp\\forge-aa11"),
    )

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    other_secret = "sk-livekey-OTHERCANARY-9876543210zyxwvutsrq"
    runner.final_bytes = malformed_final(other_secret, "req_99999", "D:\\scratch\\forge-zz99")
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)

    assert first is not None and first == second
    assert db.state == TaskState.FAILED
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: LOOP_DETECTED after 2 identical failures.\n"
        "review output unusable; kind=invalid_json; exit_code=0"
    )
    for attempt in (1, 2):
        block_text = block_path(tmp_path, task, attempt).read_text(encoding="utf-8")
        for volatile in (SECRET_VALUE, other_secret, "req_11111", "req_99999"):
            assert volatile not in block_text
            assert volatile not in db.last_error


def test_different_decode_kinds_are_different_failure_classes(tmp_path, monkeypatch):
    """The controller-owned kind IS identity: a review that produced nothing and a review that
    produced garbage are different facts, and the fingerprint says so."""
    orchestrator, db, runner, task = build_unusable_output(tmp_path, monkeypatch, final_bytes=None)

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    runner.final_bytes = "{not json"
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)

    assert first is not None and second is not None and first != second


def test_unusable_output_in_the_re_review_is_classified_the_same_way(tmp_path, monkeypatch):
    """Regression H: a genuine verdict starts the repair loop; the re-review's unusable exit-0
    output is still an environment fault, not a second finding about the candidate."""
    orchestrator, db, runner, task = build_unusable_output(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (GENUINE_CHANGES_REQUIRED_FINAL, INSPECTED_REVIEW_EVENTS),
        },
        unusable_phases={"review-2"},
        final_bytes=malformed_final(SECRET_VALUE, "req_rr", "C:\\Temp\\rr"),
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert never_completed(db)
    assert runner.phases == ["implementation", "review", "review-repair-1", "review-2"]
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review output unusable; "
        "kind=invalid_json; exit_code=0"
    )
    assert SECRET_VALUE not in db.last_error


def final_probe(tmp_path, final_bytes: str | None) -> CodexRunResult:
    """A minimal exit-0 run shape whose final file holds `final_bytes`, or is absent."""
    directory = tmp_path / "final-probe"
    directory.mkdir(parents=True, exist_ok=True)
    final_path = directory / "review.final.json"
    if final_path.exists():
        final_path.unlink()
    if final_bytes is not None:
        final_path.write_text(final_bytes, encoding="utf-8")
    jsonl = directory / "review.events.jsonl"
    jsonl.write_text("", encoding="utf-8")
    stderr = directory / "review.stderr.log"
    stderr.write_text("", encoding="utf-8")
    return CodexRunResult(
        exit_code=0,
        final={},
        jsonl_path=jsonl,
        final_path=final_path,
        stderr_path=stderr,
        thread_id="t",
        usage={},
    )


def test_decode_review_result_is_the_single_trusted_transition(tmp_path):
    """Every decode step failure yields a controller-owned kind and nothing else; only a fully
    valid ReviewResult comes back as semantics, byte-identical to what the reviewer wrote."""
    schema = load_schema("review-result.schema.json")

    decoded, kind = decode_review_result(
        final_probe(tmp_path, json.dumps(CONFORMING_REVIEW_RESULT)), schema
    )
    assert kind is None and decoded == CONFORMING_REVIEW_RESULT

    cases = [
        (None, REVIEW_OUTPUT_MISSING),  # file never created
        ("", REVIEW_OUTPUT_MISSING),  # empty file
        ("   \n\t", REVIEW_OUTPUT_MISSING),  # whitespace only
        ('{"verdict": "pass"', REVIEW_OUTPUT_INVALID_JSON),  # truncated JSON
        ("verdict: pass", REVIEW_OUTPUT_INVALID_JSON),  # not JSON at all
        (json.dumps(["not", "an", "object"]), REVIEW_OUTPUT_SCHEMA_INVALID),
        (json.dumps("just a string"), REVIEW_OUTPUT_SCHEMA_INVALID),
        # Required keys absent.
        (
            json.dumps({"verdict": "pass", "summary": "s", "findings": []}),
            REVIEW_OUTPUT_SCHEMA_INVALID,
        ),
        # Verdict outside the enum.
        (
            json.dumps({**CONFORMING_REVIEW_RESULT, "verdict": "looks_fine"}),
            REVIEW_OUTPUT_SCHEMA_INVALID,
        ),
        # Undeclared extra property.
        (
            json.dumps({**CONFORMING_REVIEW_RESULT, "debug": "x"}),
            REVIEW_OUTPUT_SCHEMA_INVALID,
        ),
        # Item-level violation at depth: findings entry missing a required key.
        (
            json.dumps(
                {
                    **CONFORMING_REVIEW_RESULT,
                    "findings": [{"severity": "low", "file": None, "line": None}],
                }
            ),
            REVIEW_OUTPUT_SCHEMA_INVALID,
        ),
    ]
    for final_bytes, expected_kind in cases:
        decoded, kind = decode_review_result(final_probe(tmp_path, final_bytes), schema)
        assert decoded is None, final_bytes
        assert kind == expected_kind, final_bytes


# --------------------------------------------------------------------------------------------
# Part 6 - W3R2: a decoded verdict is admitted only if its own stream closed every command
# --------------------------------------------------------------------------------------------
# Acceptance run W3R2 launched the full validation gate inside the review phase, never recorded an
# outcome for it, and returned a schema-valid `pass`. Both existing checks were silent by
# construction: `detect_review_infrastructure_block` reads only `item.completed` events, so an
# abandoned launch is invisible to it, and `decode_review_result` inspects only the final bytes,
# which were well formed. The verdict was therefore trusted on an evidentiary basis part of which
# never existed. Admission now consults the run's own event stream as well.
#
# What the stream can prove is exactly what is asserted here: that no outcome was recorded for a
# command the reviewer launched. It cannot prove whether a child process was orphaned, killed, or
# finished after the stream closed, and closure is never a claim about operating-system process
# exit - every one of those cases impeaches the evidence identically, so the correction stays
# purely epistemic and adds no process-ownership machinery.

W3R2_GATE_COMMAND = r'"C:\Program Files\PowerShell\7\pwsh.exe" -Command "python -m pytest"'


def command_item(command: str, **fields) -> dict:
    """A `command_execution` item in the shape the Codex CLI emits, minus any field omitted."""
    return {"type": "command_execution", "command": command, "aggregated_output": "", **fields}


def started(command: str, **fields) -> str:
    """An `item.started` line. `id` is passed explicitly so id-less starts stay expressible."""
    return json.dumps(
        {
            "type": "item.started",
            "item": command_item(command, exit_code=None, status="in_progress", **fields),
        }
    )


def completed(command: str, exit_code: int = 0, **fields) -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": command_item(
                command,
                exit_code=exit_code,
                status="failed" if exit_code else "completed",
                **fields,
            ),
        }
    )


THREAD_STARTED = json.dumps({"type": "thread.started", "thread_id": "thread-1"})
# The W3R2 shape: the gate was launched and its outcome was never recorded.
UNCLOSED_GATE_EVENTS = [THREAD_STARTED, started(W3R2_GATE_COMMAND, id="item_7")]
CLOSED_GATE_EVENTS = [
    THREAD_STARTED,
    started(W3R2_GATE_COMMAND, id="item_7"),
    completed(W3R2_GATE_COMMAND, 0, id="item_7"),
]
# A review that ran no command at all: legitimate, and never forced to run one.
STATIC_REVIEW_EVENTS = [THREAD_STARTED]


def closure_probe(tmp_path, events, *, name="stream") -> CodexRunResult:
    """A minimal exit-0 run shape whose event stream holds `events`."""
    directory = tmp_path / "closure" / name
    directory.mkdir(parents=True, exist_ok=True)
    jsonl = directory / "review.events.jsonl"
    jsonl.write_text("".join(f"{line}\n" for line in events), encoding="utf-8")
    final_path = directory / "review.final.json"
    final_path.write_text(json.dumps(CONFORMING_REVIEW_RESULT), encoding="utf-8")
    stderr = directory / "review.stderr.log"
    stderr.write_text("", encoding="utf-8")
    return CodexRunResult(
        exit_code=0,
        final={},
        jsonl_path=jsonl,
        final_path=final_path,
        stderr_path=stderr,
        thread_id="thread-1",
        usage={},
    )


def test_a_started_command_with_a_recorded_outcome_is_certified_closed(tmp_path):
    """Any recorded outcome closes a launch. The exit code is not the question here: a command
    that ran and failed was still observed, and launch-failure semantics stay the sibling
    detector's alone."""
    for name, exit_code in (("zero", 0), ("nonzero", 1), ("launch-failure", -1073741502)):
        closure = review_command_closure(
            closure_probe(
                tmp_path,
                [
                    THREAD_STARTED,
                    started("pytest", id="item_1"),
                    completed("pytest", exit_code, id="item_1"),
                ],
                name=name,
            )
        )
        assert closure.certified, exit_code
        assert closure.open_commands == ()


def test_the_w3r2_shape_a_launched_command_with_no_recorded_outcome_is_refused(tmp_path):
    """The defect itself: the gate was started, no outcome was ever recorded, and the open
    command comes back so the block artifact can name what the reviewer abandoned."""
    closure = review_command_closure(closure_probe(tmp_path, UNCLOSED_GATE_EVENTS))

    assert not closure.certified
    assert closure.open_commands == (W3R2_GATE_COMMAND,)


def test_a_review_that_ran_no_command_is_trivially_closed(tmp_path):
    """A static review is legitimate; this check never obliges a reviewer to execute anything."""
    closure = review_command_closure(closure_probe(tmp_path, STATIC_REVIEW_EVENTS))

    assert closure.certified and closure.open_commands == ()


def test_a_recorded_outcome_without_a_recorded_launch_is_certified(tmp_path):
    """The rule is one-directional: an unobserved *outcome* impeaches, an unobserved launch does
    not. This is also the shape every pre-existing fixture in the suite emits."""
    assert review_command_closure(
        closure_probe(tmp_path, INSPECTED_REVIEW_EVENTS, name="completion-only")
    ).certified
    # Including a completion carrying no id at all, which is what the Part 5 runner writes.
    assert review_command_closure(
        closure_probe(
            tmp_path, [THREAD_STARTED, completed("git diff master...HEAD")], name="idless-completion"
        )
    ).certified


def test_duplicate_and_out_of_order_ids_cannot_manufacture_a_refusal(tmp_path):
    """Set semantics over ids: multiplicity and line order are stream noise, not evidence."""
    noisy = [
        THREAD_STARTED,
        completed("pytest", 0, id="item_1"),  # completion observed before its own start line
        started("pytest", id="item_1"),
        started("pytest", id="item_1"),  # duplicate launch line for the same id
        completed("ruff check .", 0, id="item_2"),
        completed("ruff check .", 0, id="item_2"),  # duplicate completion
        started("ruff check .", id="item_2"),
    ]
    assert review_command_closure(closure_probe(tmp_path, noisy, name="noisy")).certified

    # The same noise around one genuinely unaccounted launch still refuses, and names only it.
    closure = review_command_closure(
        closure_probe(tmp_path, noisy + [started(W3R2_GATE_COMMAND, id="item_9")], name="noisy-open")
    )
    assert not closure.certified
    assert closure.open_commands == (W3R2_GATE_COMMAND,)


def test_a_launch_with_no_usable_identity_can_never_be_paired(tmp_path):
    """Identity is explicit: a start pairs only through a non-empty string id. Nothing is inferred
    from counts or chronology, so an unidentifiable launch stays open even beside a completion."""
    for name, fields in (
        ("missing-id", {}),
        ("null-id", {"id": None}),
        ("numeric-id", {"id": 7}),
        ("empty-id", {"id": ""}),
    ):
        closure = review_command_closure(
            closure_probe(
                tmp_path,
                [
                    THREAD_STARTED,
                    started(W3R2_GATE_COMMAND, **fields),
                    completed(W3R2_GATE_COMMAND, 0, id="item_1"),
                ],
                name=name,
            )
        )
        assert not closure.certified, name
        assert closure.open_commands == (W3R2_GATE_COMMAND,), name


def test_unrelated_item_types_are_not_command_outcome_claims(tmp_path):
    """Only `command_execution` items assert that a command produced an outcome. Widening the
    check to other item types would refuse on evidence that says nothing about commands."""
    events = [
        THREAD_STARTED,
        json.dumps({"type": "item.started", "item": {"id": "r1", "type": "reasoning"}}),
        json.dumps({"type": "item.started", "item": {"id": "f1", "type": "file_change"}}),
        json.dumps({"type": "item.started", "item": {"id": "a1", "type": "agent_message"}}),
        json.dumps({"type": "turn.started"}),
    ]
    assert review_command_closure(closure_probe(tmp_path, events, name="unrelated")).certified


def test_malformed_lines_are_skipped_and_a_real_open_command_is_still_caught(tmp_path):
    """Tolerant per-line parsing is the existing convention. A lost completion line therefore
    reads as unclosed, which is the fail-closed direction; garbage alone never refuses."""
    garbage = [
        "",
        "not json at all",
        '{"type": "item.completed", "item": {"type": "command_execution"',  # truncated
        json.dumps(5),  # valid JSON, not an object
        json.dumps({"type": "item.completed", "item": "not-an-object"}),
        json.dumps({"type": "item.started", "item": None}),
    ]
    assert review_command_closure(closure_probe(tmp_path, garbage, name="garbage-only")).certified

    closure = review_command_closure(
        closure_probe(tmp_path, garbage + UNCLOSED_GATE_EVENTS, name="garbage-and-open")
    )
    assert not closure.certified
    assert closure.open_commands == (W3R2_GATE_COMMAND,)


def test_an_unreadable_event_stream_is_refused_with_nothing_to_name(tmp_path):
    """Evidence ambiguity fails closed on admission: a verdict whose evidence stream cannot be
    read is not admitted. `certified` and `open_commands` are separate fields precisely so this
    case cannot be confused with a healthy review at the call site."""
    absent = closure_probe(tmp_path, [], name="absent")
    absent.jsonl_path.unlink()
    readable = closure_probe(tmp_path, [], name="directory")
    unreadable = CodexRunResult(
        exit_code=0,
        final={},
        jsonl_path=readable.jsonl_path.parent,  # a directory: readable path, unreadable stream
        final_path=readable.final_path,
        stderr_path=readable.stderr_path,
        thread_id="thread-1",
        usage={},
    )

    for result in (absent, unreadable):
        closure = review_command_closure(result)
        assert not closure.certified
        assert closure.open_commands == ()


def test_open_command_evidence_is_bounded_in_count_and_in_length(tmp_path):
    """Reviewer-influenced bytes stay bounded exactly like the sibling detector's, while
    `certified` still reflects every open command beyond the recorded bound."""
    overlong = "python -m pytest " + "x" * (_MAX_RECORDED_COMMAND_CHARS * 3)
    events = [THREAD_STARTED] + [
        started(overlong, id=f"item_{index}") for index in range(_MAX_RECORDED_COMMANDS + 5)
    ]

    closure = review_command_closure(closure_probe(tmp_path, events, name="bounded"))

    assert not closure.certified
    assert len(closure.open_commands) == _MAX_RECORDED_COMMANDS
    for command in closure.open_commands:
        assert len(command) == _MAX_RECORDED_COMMAND_CHARS + len("...")
        assert command.endswith("...")


# --- controller routing ----------------------------------------------------------------------


def build_review_stream(tmp_path, monkeypatch, *, final=None, events=None, **kwargs):
    """The controller with one scripted review phase, whose event stream the test chooses."""
    return build_orchestrator(
        tmp_path,
        monkeypatch,
        finals={
            "implementation": (COMPLETED_IMPLEMENTATION_FINAL, []),
            "review": (final if final is not None else PASSING_REVIEW_FINAL, events or []),
        },
        **kwargs,
    )


UNCLOSED_LAST_ERROR = (
    f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review output unusable; "
    f"kind={REVIEW_OUTPUT_COMMANDS_UNCLOSED}; exit_code=0"
)


def test_a_schema_valid_pass_on_an_unobserved_gate_is_infrastructure_not_semantic(
    tmp_path, monkeypatch
):
    """The W3R2 replay. The report decodes, conforms and says `pass`; its own stream says the gate
    it launched was never observed. The task must not complete on it, and no finding, no repair
    and no candidate-quality conclusion may be produced from an environment fault."""
    orchestrator, db, runner, task = build_review_stream(
        tmp_path, monkeypatch, events=UNCLOSED_GATE_EVENTS
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert never_completed(db)
    assert db.state == TaskState.READY  # bounded retry, exactly like any other unusable output
    assert runner.phases == ["implementation", "review"]
    assert not [phase for phase in runner.phases if "repair" in phase]
    assert db.last_error == UNCLOSED_LAST_ERROR
    record = block_record(tmp_path, task, attempt=1)
    assert record["error_code"] == REVIEW_INFRASTRUCTURE_BLOCKED
    assert record["reason"] == BLOCK_REVIEW_OUTPUT_UNUSABLE
    assert record["kind"] == REVIEW_OUTPUT_COMMANDS_UNCLOSED
    assert record["exit_codes"] == [0]
    assert "http_status" not in record
    # The abandoned command is evidence: it reaches `failed_commands` and nothing else.
    assert record["failed_commands"] == [W3R2_GATE_COMMAND]
    assert W3R2_GATE_COMMAND not in db.last_error
    assert Path(record["evidence"]["events"]).is_file()
    assert Path(record["evidence"]["final"]).is_file()
    # The control plane still reads this as an environment fault, not a defect in the code.
    code, message = _safe_task_error(db.last_error)
    assert code == REVIEW_INFRASTRUCTURE_BLOCKED
    assert "not a finding about the task's code" in message


def test_an_unclosed_changes_required_verdict_launches_no_repair(tmp_path, monkeypatch):
    """Verdict-agnostic by design: an abandoned gate impeaches `changes_required` exactly as it
    impeaches `pass`, so no paid repair phase runs against findings from an unobserved run."""
    orchestrator, db, runner, task = build_review_stream(
        tmp_path,
        monkeypatch,
        final=GENUINE_CHANGES_REQUIRED_FINAL,
        events=UNCLOSED_GATE_EVENTS,
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert never_completed(db)
    assert runner.phases == ["implementation", "review"]
    assert not [phase for phase in runner.phases if "repair" in phase]
    assert db.last_error == UNCLOSED_LAST_ERROR
    assert block_record(tmp_path, task, attempt=1)["kind"] == REVIEW_OUTPUT_COMMANDS_UNCLOSED


def test_a_review_whose_launched_command_closed_still_completes(tmp_path, monkeypatch):
    """The positive law: an observed gate is a healthy review and nothing about it changes."""
    orchestrator, db, runner, task = build_review_stream(
        tmp_path, monkeypatch, events=CLOSED_GATE_EVENTS
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == "completed"
    assert db.state == TaskState.COMPLETED
    assert runner.phases == ["implementation", "review"]


def test_a_review_that_launched_no_command_still_completes(tmp_path, monkeypatch):
    """No-command compatibility: a static review is trivially closed and completes as before."""
    orchestrator, db, runner, task = build_review_stream(
        tmp_path, monkeypatch, events=STATIC_REVIEW_EVENTS
    )

    assert orchestrator._execute_task_body(task) == "completed"
    assert db.state == TaskState.COMPLETED


def test_a_completion_only_stream_still_completes(tmp_path, monkeypatch):
    """The shape every pre-existing fixture emits keeps completing: no test in the suite has to
    fabricate `item.started` lines to stay healthy."""
    orchestrator, db, runner, task = build_review_stream(
        tmp_path, monkeypatch, events=INSPECTED_REVIEW_EVENTS
    )

    assert orchestrator._execute_task_body(task) == "completed"
    assert db.state == TaskState.COMPLETED


def test_repeated_unclosed_reviews_share_one_fingerprint_and_the_loop_terminates(
    tmp_path, monkeypatch
):
    """Failure identity stays controller-owned: two abandoned reviews are the same class even
    when the abandoned command text differs, so bounded loop detection still terminates and the
    owning class survives into the terminal error."""
    orchestrator, db, runner, task = build_review_stream(
        tmp_path, monkeypatch, events=UNCLOSED_GATE_EVENTS
    )

    orchestrator._execute_task_body(task)
    first = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    other_command = f"python -m pytest -k {SECRET_VALUE}"
    runner.finals["review"] = (
        PASSING_REVIEW_FINAL,
        [THREAD_STARTED, started(other_command, id="item_42")],
    )
    orchestrator._execute_task_body(task)
    second = latest_fingerprint(db)

    assert first is not None and first == second
    assert db.state == TaskState.FAILED
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: LOOP_DETECTED after 2 identical failures.\n"
        f"review output unusable; kind={REVIEW_OUTPUT_COMMANDS_UNCLOSED}; exit_code=0"
    )
    assert SECRET_VALUE not in db.last_error
    assert block_record(tmp_path, task, attempt=2)["failed_commands"] == [other_command]
    assert SECRET_VALUE not in block_path(tmp_path, task, attempt=1).read_text(encoding="utf-8")


def test_an_unclosed_review_is_a_different_failure_class_from_unusable_bytes(tmp_path, monkeypatch):
    """A review that produced garbage and a review that abandoned its gate are different facts,
    and the controller-owned kind keeps their failure identities disjoint."""
    orchestrator, db, runner, task = build_review_stream(
        tmp_path, monkeypatch, events=UNCLOSED_GATE_EVENTS
    )

    orchestrator._execute_task_body(task)
    unclosed = latest_fingerprint(db)
    reclaim_for_next_attempt(db, task)
    orchestrator.runner = UnusableReviewOutputRunner(
        runner.finals, runner.resolution, {"review"}, final_bytes="{not json"
    )
    orchestrator._execute_task_body(task)
    invalid_json = latest_fingerprint(db)

    assert unclosed is not None and invalid_json is not None
    assert unclosed != invalid_json
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review output unusable; "
        f"kind={REVIEW_OUTPUT_INVALID_JSON}; exit_code=0"
    )


def test_a_launch_dead_review_stays_terminal_even_with_an_unclosed_command(tmp_path, monkeypatch):
    """Precedence pin 1: the launch-dead detector runs first and keeps its terminal, no-retry
    ownership. A stream that is both launch-dead and unclosed is still `commands_never_executed`."""
    orchestrator, db, runner, task = build_review_stream(
        tmp_path,
        monkeypatch,
        final=CANARY_BLOCKED_FINAL,
        events=BLOCKED_REVIEW_EVENTS + [started(W3R2_GATE_COMMAND, id="item_9")],
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert db.state == TaskState.FAILED  # terminal, not the unusable path's bounded retry
    assert never_completed(db)
    record = block_record(tmp_path, task, attempt=1)
    assert record["reason"] == BLOCK_COMMANDS_NEVER_EXECUTED
    assert "kind" not in record
    assert db.last_error != UNCLOSED_LAST_ERROR


def test_a_decode_failure_outranks_an_unclosed_command(tmp_path, monkeypatch):
    """Precedence pin 2: decoding is judged before closure, so every failure class the controller
    already names keeps its existing kind - and therefore its existing fingerprint - unchanged."""
    orchestrator, db, runner, task = build_review_stream(
        tmp_path,
        monkeypatch,
        final={"verdict": "pass", "summary": "Half a report.", "findings": []},
        events=UNCLOSED_GATE_EVENTS,
    )

    outcome = orchestrator._execute_task_body(task)

    assert outcome == OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED
    assert db.last_error == (
        f"{REVIEW_INFRASTRUCTURE_BLOCKED}: review output unusable; "
        f"kind={REVIEW_OUTPUT_SCHEMA_INVALID}; exit_code=0"
    )
    record = block_record(tmp_path, task, attempt=1)
    assert record["kind"] == REVIEW_OUTPUT_SCHEMA_INVALID
    assert record["failed_commands"] == []
