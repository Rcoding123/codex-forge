"""Gate 7 foundation: the approval-gated Codex telemetry canary, and the report it produces.

Gate 5 proved a supervised **Claude** canary could be accounted for. Gate 7 asks the same question of
**Codex**, and the two are not the same question. Claude is configured by environment variable; Codex
is configured by a per-invocation `-c` override that `--strict-config` will reject outright if a
single key is wrong, and its content-bearing events live on the *logs* signal rather than on spans.
So the Codex canary has to prove four things at once: that the override is accepted, that logs stay
off, that traces and metrics arrive, and that `forge.invocation.id` ties what arrived to the one
invocation that produced it.

**Gate 7 has not passed. No Codex process has ever run under this harness.** Everything here is the
apparatus for one supervised run that has not happened, and the harness is written so that the run is
the only thing that can produce a `PASS`:

* nothing starts without `--approved-by` naming a human;
* `--dry-run` prints the exact argument vector and starts nothing at all;
* an existing output directory stops the run before a Collector is started;
* the Collector and the Codex child are reaped on every path, including a timeout;
* a malformed JSONL line, a reply that is not exactly `CANARY_OK`, an unresolved span or metric
  remainder, an absent correlation attribute, any log record, or any forbidden value in the rendered
  report each fail the gate.

What this module reuses rather than re-implements: the Collector lifecycle, the settle-and-read span
accounting, the process reaping, and the privacy check all come from :mod:`otel_canary`; the
Collector configuration, the bounded span-name diagnostic, and the internal-telemetry scrape come
from :mod:`otel_certification`; the output-directory refusal, the deterministic JSON rendering, and
the forbidden-output scan come from :mod:`otel_inventory`.

What it will never carry: the prompt, the reply, a path, a credential, an account, or the invocation
identifier itself. The correlation result is two booleans — the attribute was observed, and its value
equalled the one this process is holding — because printing the value would put a 32-character
identifier into a file whose whole purpose is to be readable by someone who is not allowed to see
one.

    .\\.venv\\Scripts\\python.exe tests\\otel_codex_canary.py --output-dir <dir> --dry-run
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import otel_canary as canary
import otel_certification as certification
import otel_inventory as inventory

# The package lives under `src/`; the harness is run directly as a script as often as under pytest.
if str(certification.REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(certification.REPO_ROOT / "src"))

from codex_forge import agent_telemetry  # noqa: E402 - after the sys.path insert above
from codex_forge.config import ForgeConfig  # noqa: E402 - same
from codex_forge.runner import _build_process_command  # noqa: E402 - same

SCHEMA_VERSION = "forge.codex.telemetry-canary/1"

REPORT_JSON_NAME = "codex-canary-report.json"
REPORT_MARKDOWN_NAME = "codex-canary-report.md"
OWNED_REPORT_NAMES = (REPORT_JSON_NAME, REPORT_MARKDOWN_NAME)

# The Codex this vector was written against. A version change is a drift event under contract
# section 10: `--strict-config` acceptance, the `[otel]` key names, and the JSONL event shapes are
# all version-specific, and none of them may be assumed to carry forward.
CODEX_VERSION = "0.146.0"

# The same one-shot prompt the Claude canary uses, so the two gates differ in the agent and nothing
# else. Imported rather than retyped: two copies of a canary prompt is two prompts.
CANARY_PROMPT = inventory.CANARY_PROMPT
CANARY_RESPONSE = "CANARY_OK"


# ---------------------------------------------------------------------------
# The Codex vector, and what it is allowed to be
# ---------------------------------------------------------------------------
#
# The canonical form is an *argument vector*, not a command string. The vector below is what
# `subprocess` receives; the command an operator types does not contain it.

CODEX_EXECUTABLE = "codex"
CODEX_MODEL = "gpt-5.6-terra"
CODEX_REASONING_EFFORT = "low"

# The two settings that bound what the canary may do to this machine. `read-only` is the narrowest
# sandbox Codex offers, and `never` means no approval prompt can be answered — together they make a
# canary that cannot write, and cannot be talked into writing, whatever the model replies.
CODEX_SANDBOX = "read-only"
CODEX_APPROVAL_POLICY = "never"

# Flags that would make the run something other than the canary that was approved. The first two are
# the same flag in 0.146.0 (`--yolo` is the alias) and disable the sandbox *and* the approval policy
# in one move; `--full-auto` is the removed legacy spelling that the binary still recognizes well
# enough to warn about, so it is refused by name rather than left to a deprecation path.
FORBIDDEN_CODEX_FLAGS = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "--full-auto",
)

# Sandbox modes that are not this canary's. Refused wherever they appear in a supplied command, not
# only as the value of `--sandbox`, because they also reach Codex through `-c sandbox_mode=...`.
FORBIDDEN_SANDBOX_MODES = ("workspace-write", "danger-full-access")

# Bounded by design. A canary that hangs holds a loopback port and an authenticated child process;
# five minutes is far past a one-token reply and far short of anything an operator would wait out.
CODEX_CANARY_TIMEOUT = 300.0

# Stand-ins for the two values that do not exist until the run starts. `--dry-run` prints the vector
# with these in place, so the printed vector has the exact shape of the real one without inventing a
# correlation identifier that was never minted or a port that was never bound.
PLACEHOLDER_INVOCATION_ID = "0" * agent_telemetry.INVOCATION_ID_CHARS
PLACEHOLDER_ENDPOINT = "http://127.0.0.1:4318"


def worker_disabled_features() -> tuple[str, ...]:
    """The exact feature list `CodexRunner` disables, read from the production default.

    Read rather than retyped. If Forge's own list changes, the canary's changes with it, and there is
    no second list to drift.
    """
    for item in dataclasses.fields(ForgeConfig):
        if item.name == "worker_disabled_features":
            return tuple(item.default_factory())
    raise RuntimeError("ForgeConfig no longer declares worker_disabled_features.")


def disabled_feature_arguments() -> tuple[str, ...]:
    """`--disable <feature>` pairs, built exactly as `CodexRunner.run` builds them."""
    return tuple(
        item for feature in dict.fromkeys(worker_disabled_features()) for item in ("--disable", feature)
    )


def build_codex_argv(
    *, overrides: Sequence[str], executable: str = CODEX_EXECUTABLE
) -> tuple[str, ...]:
    """The exact canary vector, as separate argv elements. Pure; starts nothing.

    Global options precede the subcommand and subcommand options follow it, which is the placement
    `CodexRunner` already uses and the only one Codex 0.146.0 accepts. The four `exec` options each
    remove a way for the run to be something other than what was approved:

    * `--json` — machine-readable events, so the reply can be checked without reading a transcript.
    * `--strict-config` — an unrecognized configuration key fails the invocation instead of being
      ignored. This is what makes the `-c` override a *test* rather than a hope.
    * `--ephemeral` — no session file is written to disk.
    * `--ignore-user-config` — `$CODEX_HOME/config.toml` is not loaded, so an operator's own
      settings cannot change what the canary runs. Authentication still uses `CODEX_HOME`, which is
      exactly why this flag is the right tool and relocating `CODEX_HOME` is not.
    * `--skip-git-repo-check` — the canary runs in an empty temporary directory that is deliberately
      not a repository.
    """
    return (
        executable,
        "--ask-for-approval",
        CODEX_APPROVAL_POLICY,
        "--sandbox",
        CODEX_SANDBOX,
        "--model",
        CODEX_MODEL,
        "-c",
        f"model_reasoning_effort={CODEX_REASONING_EFFORT}",
        *overrides,
        *disabled_feature_arguments(),
        "exec",
        "--json",
        "--strict-config",
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",
        CANARY_PROMPT,
    )


def placeholder_argv(executable: str = CODEX_EXECUTABLE) -> tuple[str, ...]:
    """The vector as `--dry-run` prints it: real in every element except the port and the id."""
    return build_codex_argv(
        overrides=agent_telemetry.build_codex_otel_overrides(
            agent_telemetry.InvocationContext(invocation_id=PLACEHOLDER_INVOCATION_ID),
            endpoint=PLACEHOLDER_ENDPOINT,
        ),
        executable=executable,
    )


def resolve_codex_argv(
    command: str = "", *, executable: str = CODEX_EXECUTABLE
) -> tuple[tuple[str, ...], list[str]]:
    """Return ``(argv, problems)`` for an operator-supplied command, or the canonical vector.

    An empty ``command`` yields the canonical vector, which is the ordinary path: the canary
    invocation is not something an operator should have to retype.

    A supplied command must parse to the same vector, differing at most in the path used to reach the
    `codex` executable. The `-c otel.*` overrides may not be supplied at all — they carry the
    per-invocation correlation identifier and the run's own ephemeral port, neither of which exists
    when the command is typed. Everything else is compared element by element; "close enough" is how
    a canary ends up running with a writable sandbox.
    """
    canonical = placeholder_argv(executable)
    if not command.strip():
        return canonical, []
    try:
        argv = tuple(shlex.split(command))
    except ValueError as error:
        return (), [f"the Codex command could not be parsed: {error}"]
    if not argv:
        return (), ["the Codex command is empty"]

    problems: list[str] = []
    lowered = [argument.lower() for argument in argv]
    for flag in FORBIDDEN_CODEX_FLAGS:
        if flag in lowered:
            problems.append(f"the Codex command must not pass {flag}")
    for mode in FORBIDDEN_SANDBOX_MODES:
        if mode in lowered:
            problems.append(f"the Codex command must not name the {mode} sandbox")
    if "--sandbox" in argv and argv[argv.index("--sandbox") + 1 :][:1] != (CODEX_SANDBOX,):
        problems.append(f"--sandbox must be {CODEX_SANDBOX}")
    if "--ask-for-approval" in argv and argv[argv.index("--ask-for-approval") + 1 :][:1] != (
        CODEX_APPROVAL_POLICY,
    ):
        problems.append(f"--ask-for-approval must be {CODEX_APPROVAL_POLICY}")
    if Path(argv[0]).stem.lower() != "codex":
        problems.append("the Codex command must invoke `codex`")
    if any(argument.startswith("otel.") for argument in argv):
        problems.append(
            "the Codex command must not supply an otel.* override; they are built per invocation"
        )
    if argv[-1:] != (CANARY_PROMPT,):
        problems.append("the Codex command must end with the exact canary prompt")

    supplied = _without_otel_overrides(argv)[1:]
    expected = _without_otel_overrides(canonical)[1:]
    if supplied != expected:
        missing = [item for item in expected if item not in supplied]
        extra = [item for item in supplied if item not in expected]
        problems.append(
            "the Codex command conflicts with the approved canary vector"
            + (f"; missing {missing}" if missing else "")
            + (f"; unexpected {extra}" if extra else "")
            + ("; same arguments in a different order" if not missing and not extra else "")
        )
    # The supplied vector is never returned: a command that survives every check is equal to the
    # canonical one except for argv[0], and the canonical one is what the run is approved for.
    return (argv[:1] + canonical[1:] if not problems else argv), problems


def _without_otel_overrides(argv: Sequence[str]) -> tuple[str, ...]:
    """Drop each `-c otel.*` pair, which is the only part of the vector an operator cannot type."""
    kept: list[str] = []
    index = 0
    while index < len(argv):
        if (
            argv[index] == "-c"
            and index + 1 < len(argv)
            and argv[index + 1].startswith("otel.")
        ):
            index += 2
            continue
        kept.append(argv[index])
        index += 1
    return tuple(kept)


# ---------------------------------------------------------------------------
# Codex JSONL
# ---------------------------------------------------------------------------
#
# `codex exec --json` emits one JSON object per line. The parser below reads three things out of it
# and nothing else: how many lines failed to parse, how many events of each *bounded* type were seen,
# and whether exactly one final assistant message was returned whose text is exactly `CANARY_OK`.
#
# The reply text itself is never stored, never counted by length, and never rendered. It is compared
# and discarded. An event type outside the vocabulary is counted under `other` for the same reason a
# span name outside the diagnostic vocabulary is: an agent-controlled string is unbounded, and the
# count is the part that is safe to keep.

EVENT_TYPES = (
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "item.started",
    "item.updated",
    "item.completed",
    "error",
)
EVENT_OTHER = "other"
EVENT_BUCKETS = EVENT_TYPES + (EVENT_OTHER,)

# The item types that carry a visible assistant reply. Both spellings are accepted because the event
# schema is not pinned by anything this repository controls; a run that matches neither reports zero
# assistant messages and fails, which is the fail-closed outcome.
ASSISTANT_ITEM_TYPES = frozenset({"agent_message", "assistant_message"})
COMPLETED_EVENT_TYPES = frozenset({"item.completed", "item.done"})


@dataclass(frozen=True)
class CodexTranscript:
    """What one Codex run said, reduced to counts and booleans before anything is kept."""

    lines: int = 0
    malformed: int = 0
    event_counts: Mapping[str, int] = field(default_factory=dict)
    assistant_messages: int = 0
    matched_messages: int = 0

    @property
    def parsed_cleanly(self) -> bool:
        return self.lines > 0 and self.malformed == 0

    @property
    def canary_ok(self) -> bool:
        """Exactly one visible assistant reply, and it was exactly `CANARY_OK`.

        This is a statement about the reply and nothing else. Whether every line of the stream parsed
        is a separate fact, reported separately and gated separately: a stream that also carried a
        line this parser could not read is a *malformed stream*, not a wrong answer, and conflating
        the two produced a run that reported one matched `CANARY_OK` response and `canary_ok=false`
        in the same breath. The exactness of the match is untouched — `matched_messages` still counts
        only a reply equal to `CANARY_OK`, and one unmatched reply still makes this false.
        """
        return self.assistant_messages == 1 and self.matched_messages == 1


def parse_codex_transcript(text: str) -> CodexTranscript:
    """Reduce Codex **stdout** JSONL to counts. Never returns, stores, or logs any message text.

    Only stdout is ever passed here. `codex exec --json` defines stdout as the JSONL stream; stderr
    is free-form diagnostic text that is not, and never was, a record in this protocol.
    """
    lines = 0
    malformed = 0
    counts: dict[str, int] = {}
    assistant = 0
    matched = 0

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        lines += 1
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        kind = event.get("type")
        bucket = kind if isinstance(kind, str) and kind in EVENT_TYPES else EVENT_OTHER
        counts[bucket] = counts.get(bucket, 0) + 1

        reply = _assistant_text(event)
        if reply is None:
            continue
        assistant += 1
        if reply.strip() == CANARY_RESPONSE:
            matched += 1

    return CodexTranscript(
        lines=lines,
        malformed=malformed,
        event_counts=dict(sorted(counts.items())),
        assistant_messages=assistant,
        matched_messages=matched,
    )


def _assistant_text(event: Mapping) -> str | None:
    """The visible assistant reply carried by one event, or None. Counted on completion only.

    `item.started` and `item.updated` describe the *same* message as the completed event that
    follows, so counting them would turn one reply into three and fail a run that behaved.
    """
    kind = event.get("type")
    item = event.get("item")
    if isinstance(item, Mapping) and kind in COMPLETED_EVENT_TYPES:
        item_type = item.get("item_type") or item.get("type")
        text = item.get("text")
        if item_type in ASSISTANT_ITEM_TYPES and isinstance(text, str):
            return text
    if kind in ASSISTANT_ITEM_TYPES and isinstance(event.get("text"), str):
        return str(event["text"])
    return None


@dataclass(frozen=True)
class CodexDiagnostics:
    """What Codex wrote to stderr, reduced to counts before anything is kept.

    Stderr is diagnostic text of unknown provenance: it can quote a path, a profile name, or an
    upstream error body. None of it is parsed as JSONL, none of it can become an assistant response,
    and none of it reaches the report — only how much of it there was.
    """

    lines: int = 0
    characters: int = 0

    @property
    def empty(self) -> bool:
        return self.lines == 0


def summarize_stderr(text: str) -> CodexDiagnostics:
    """Reduce Codex stderr to two counts. Never returns, stores, or logs any diagnostic text.

    A non-empty stderr is not by itself a Gate 7 failure. Codex writes ordinary notices there on a
    run that behaved, and the failures that *are* verdict-relevant are observed directly rather than
    inferred from prose: a process failure is the return code, and an export failure is the
    Collector's own accepted/refused counters and the span and metric accounting built from them.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    return CodexDiagnostics(lines=len(lines), characters=sum(len(line) for line in lines))


# ---------------------------------------------------------------------------
# What the Collector saw
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportProbe:
    """The totals the span accounting does not read, taken while the Collector still answers."""

    metrics: canary.MetricAccounting
    log_records_accepted: int
    log_records_refused: int


COUNTER_ACCEPTED_LOGS = "otelcol_receiver_accepted_log_records"
COUNTER_REFUSED_LOGS = "otelcol_receiver_refused_log_records"


def read_transport(ports: Mapping[str, int]) -> TransportProbe:
    """Read metric-point and log-record totals from the Collector's loopback internal telemetry."""
    port = int(ports["internal"])
    series = certification.scrape_internal_metrics(port)
    return TransportProbe(
        metrics=canary.read_metric_accounting(port),
        log_records_accepted=certification.counter_total(series, COUNTER_ACCEPTED_LOGS),
        log_records_refused=certification.counter_total(series, COUNTER_REFUSED_LOGS),
    )


# Markers the debug exporter prints when a log record reaches an exporter. Their presence in the
# Collector output means a logs pipeline exists and carried something, which fails the gate outright.
LOG_SIGNAL_MARKERS = ("LogRecord #", "ResourceLog #", "LogRecordCount")


def logs_observed(output: str) -> bool:
    return any(marker in output for marker in LOG_SIGNAL_MARKERS)


def logs_pipeline_declared(config: Mapping) -> bool:
    """True when the Collector configuration declares a pipeline for a forbidden signal."""
    pipelines = (config.get("service") or {}).get("pipelines") or {}
    return any(
        str(name).split("/", 1)[0] in certification.FORBIDDEN_PIPELINES for name in pipelines
    )


def correlation_result(output: str, invocation_id: str) -> dict:
    """Whether the two correlation attributes survived, and whether the id was *this* run's.

    Three booleans and no value. The identifier is compared against the one this process minted and
    then dropped; the report says it matched, never what it was.
    """
    return {
        "invocation_id_attribute_observed": f"-> {agent_telemetry.ATTR_INVOCATION_ID}:" in output,
        "invocation_id_matched_local_value": bool(invocation_id) and invocation_id in output,
        "contract_version_attribute_observed": (
            f"-> {agent_telemetry.ATTR_CONTRACT_VERSION}:" in output
        ),
    }


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

# Stated in the report itself rather than in a document nobody opens next to it. Every one of these
# is a thing a reader of a PASS could otherwise reasonably assume, and none of them is true.
LIMITATIONS = (
    "Gate 7 observes one invocation. It is not a field inventory: no Codex resource, span, event, or "
    "metric attribute has been individually inspected or approved. That is Gate 8.",
    "Native Codex telemetry remains disabled outside this harness. Normal Forge execution is "
    "unwired: no production module imports the builders this canary uses, and CodexRunner emits no "
    "-c otel override and no OTEL_* variable.",
    "Traces and metrics are observational. Forge persisted usage remains the authoritative accounting "
    "basis for budgets, retries, and task state, and nothing here feeds any of them.",
    "Native telemetry proves nothing about subscription cost or remaining quota. Token counts on a "
    "span are the model's report of one request, not a bill and not a balance.",
    "The exporter override is accepted or rejected by this run alone. Until it is accepted, the "
    "nesting of the otlp-http exporter table, the presence of a headers field on it, and whether "
    "the per-signal endpoint URLs the builder emits (/v1/traces and /v1/metrics) are the form the "
    "exporter posts to are unproven.",
    "CODEX_HOME is preserved exactly as the operator holds it. It is never set, redirected, or "
    "removed, and --ignore-user-config still authenticates out of it.",
)


def build_document(
    report: canary.CanaryReport,
    *,
    transcript: CodexTranscript | None,
    invocation_id: str,
    timed_out: bool = False,
    codex_returncode: int | None = None,
    diagnostics: CodexDiagnostics | None = None,
    config: Mapping | None = None,
    errors: Sequence[str] = (),
) -> dict:
    """Turn one canary run into the canonical Gate 7 document. Deterministic and sorted."""
    accounting = report.accounting
    probe = report.probe if isinstance(report.probe, TransportProbe) else None
    metrics = probe.metrics if probe is not None else None
    if metrics is not None:
        # The probe read the counters while the Collector still answered; the connector-generated
        # points can only be counted off the complete log, which exists only after shutdown
        # flushed the final batch. Joining them here is what makes the metric equation balance
        # for a topology whose exporter is shared by the OTLP and diagnostic metric pipelines.
        metrics = metrics.with_internally_generated(
            certification.count_diagnostic_metric_points(report.output)
        )
    effective = config if config is not None else canary_configuration()
    empty = CodexTranscript()
    seen = transcript if transcript is not None else empty
    stderr = diagnostics if diagnostics is not None else CodexDiagnostics()

    body = {
        "schema": SCHEMA_VERSION,
        "subject": {
            "agent": "codex-cli",
            "agent_version": CODEX_VERSION,
            "collector_version": certification.CERTIFIED_COLLECTOR_VERSION,
            "contract_version": agent_telemetry.AGENT_TELEMETRY_CONTRACT_VERSION,
            "otel_environment": agent_telemetry.CODEX_OTEL_ENVIRONMENT,
        },
        "canary_process": {
            "codex_returncode": codex_returncode,
            "timed_out": bool(timed_out),
            "jsonl_lines": seen.lines,
            "jsonl_malformed": seen.malformed,
            "event_counts": dict(seen.event_counts),
            "assistant_messages": seen.assistant_messages,
            "matched_canary_responses": seen.matched_messages,
            "canary_ok": seen.canary_ok,
            "transcript_read": transcript is not None,
            # Counts only, and no condition below reads them: stderr is diagnostic text, a non-empty
            # stderr is not a failure, and its content never reaches this report.
            "stderr_lines": stderr.lines,
            "stderr_characters": stderr.characters,
        },
        "telemetry_transport": {
            "logs_exporter": agent_telemetry.CODEX_LOGS_EXPORTER,
            "logs_pipeline_declared": logs_pipeline_declared(effective),
            "log_records_accepted": getattr(probe, "log_records_accepted", None),
            "log_records_refused": getattr(probe, "log_records_refused", None),
            "log_signal_in_output": logs_observed(report.output),
            "collector_returncode": report.collector_returncode,
            "counts_settled": getattr(accounting, "settled", None),
        },
        "correlation": correlation_result(report.output, invocation_id),
        "span_accounting": {
            "accepted": getattr(accounting, "accepted", None),
            "exported": getattr(accounting, "exported", None),
            "filtered": getattr(accounting, "filtered", None),
            "refused": getattr(accounting, "refused", None),
            "send_failed": getattr(accounting, "send_failed", None),
            "unresolved": getattr(accounting, "unresolved", None),
            "balanced": bool(accounting is not None and accounting.balanced),
            "accepted_by_name": dict(getattr(accounting, "accepted_by_name", {}) or {}),
            "exported_by_name": dict(getattr(accounting, "exported_by_name", {}) or {}),
            "not_exported_by_name": (
                accounting.missing_by_name() if accounting is not None else {}
            ),
            "diagnostic_disagreements": list(
                getattr(accounting, "diagnostic_disagreements", ()) or ()
            ),
        },
        # Three populations, named separately, because two of them are counted by different
        # Collector counters and the third is their sum. `accepted` is agent-originated points at
        # the OTLP receiver; `internally_generated` is points the diagnostic `count` connectors
        # made out of spans and handed straight to the shared exporter; `exported` is everything
        # the exporter sent, which is both. A reader who sees only `accepted` and `exported` would
        # reach the same wrong conclusion this accounting used to.
        "metric_accounting": {
            "accepted": getattr(metrics, "accepted", None),
            "internally_generated": getattr(metrics, "internally_generated", None),
            "available": getattr(metrics, "available", None),
            "exported": getattr(metrics, "exported", None),
            "refused": getattr(metrics, "refused", None),
            "send_failed": getattr(metrics, "send_failed", None),
            "unresolved": getattr(metrics, "unresolved", None),
            "balanced": bool(metrics is not None and metrics.balanced),
        },
        "privacy": {
            "checks_run": list(report.privacy.checks),
            "findings": list(report.privacy.findings),
            "passed": report.privacy.passed,
        },
        "run": {
            "canary_passed": report.passed,
            "errors": [*report.errors, *errors],
        },
        "limitations": list(LIMITATIONS),
    }

    # Scanned as serialized, so anything a future field could carry into the report is caught before
    # the report exists rather than after somebody reads it. The invocation identifier is the value
    # this most exists to catch: it is 32 hexadecimal characters, and the scan knows that shape.
    leaked = inventory.scan_forbidden_output(json.dumps(body, sort_keys=True, ensure_ascii=False))
    body["privacy_scan"] = {
        "patterns_checked": [name for name, _ in inventory.FORBIDDEN_OUTPUT_PATTERNS],
        "patterns_found": list(leaked),
        "passed": not leaked,
    }
    body["gate7"] = _verdict(body)
    return body


def _verdict(body: Mapping) -> dict:
    """Every condition Gate 7 requires, each named so a FAIL says which one failed."""
    process = body["canary_process"]
    transport = body["telemetry_transport"]
    correlation = body["correlation"]
    spans = body["span_accounting"]
    metrics = body["metric_accounting"]
    conditions = {
        "the Codex process exited cleanly": process["codex_returncode"] == 0,
        "the Codex process did not time out": process["timed_out"] is False,
        "the Codex JSONL was read": process["transcript_read"] is True,
        # Stdout only. Stderr is a separate stream, counted separately, and cannot reach this.
        "every Codex JSONL line parsed": process["jsonl_lines"] > 0
        and process["jsonl_malformed"] == 0,
        "exactly one visible assistant response was returned": process["assistant_messages"] == 1,
        "the response was exactly CANARY_OK": process["canary_ok"] is True,
        "the Collector declares no logs pipeline": transport["logs_pipeline_declared"] is False,
        "the logs exporter is none": transport["logs_exporter"] == "none",
        "no log record was accepted": transport["log_records_accepted"] == 0,
        "no log signal reached the exporter": transport["log_signal_in_output"] is False,
        "the counts settled": transport["counts_settled"] is True,
        "the Collector exited cleanly": transport["collector_returncode"] == 0,
        "at least one span was accepted": (spans["accepted"] or 0) > 0,
        "the span accounting balanced": spans["balanced"] is True,
        "the diagnostic agrees with the Collector counters": not spans["diagnostic_disagreements"],
        "at least one metric point was accepted": (metrics["accepted"] or 0) > 0,
        "the metric accounting balanced": metrics["balanced"] is True,
        "forge.invocation.id was observed after redaction": correlation[
            "invocation_id_attribute_observed"
        ]
        is True,
        "the observed forge.invocation.id matched the locally held value": correlation[
            "invocation_id_matched_local_value"
        ]
        is True,
        "forge.telemetry.contract.version was observed": correlation[
            "contract_version_attribute_observed"
        ]
        is True,
        "the privacy checks passed": body["privacy"]["passed"] is True,
        "no forbidden value appears in this report": body["privacy_scan"]["passed"] is True,
        "the canary run passed": body["run"]["canary_passed"] is True,
        "the run recorded no error": not body["run"]["errors"],
    }
    return {
        "verdict": "PASS" if all(conditions.values()) else "FAIL",
        "conditions": [{"condition": name, "met": bool(met)} for name, met in sorted(conditions.items())],
    }


_MARKDOWN_SECTIONS = (
    ("canary_process", "Canary process result"),
    ("telemetry_transport", "Telemetry transport result"),
    ("correlation", "Correlation result"),
    ("span_accounting", "Span accounting"),
    ("metric_accounting", "Metric accounting"),
    ("privacy", "Privacy check"),
)


def render_markdown(document: Mapping) -> str:
    """The human-readable report. Rendered only from ``document``, so the two always agree."""
    gate = document["gate7"]
    subject = document["subject"]
    lines = [
        "# Codex telemetry canary (Gate 7)",
        "",
        f"- schema: `{document['schema']}`",
        f"- agent: `{subject['agent']}` `{subject['agent_version']}`",
        f"- collector: `{subject['collector_version']}`",
        f"- contract: `{subject['contract_version']}`",
        f"- otel environment: `{subject['otel_environment']}`",
        "",
        f"## Gate 7 verdict: {gate['verdict']}",
        "",
        "| Condition | Met |",
        "| --- | --- |",
    ]
    for condition in gate["conditions"]:
        lines.append(f"| {condition['condition']} | {'yes' if condition['met'] else '**NO**'} |")

    for key, title in _MARKDOWN_SECTIONS:
        lines += ["", f"## {title}", "", "| Field | Value |", "| --- | --- |"]
        for name, value in sorted(document[key].items()):
            lines.append(f"| `{name}` | {_cell(value)} |")
        if key == "metric_accounting":
            lines += ["", *_metric_equation(document[key])]

    lines += ["", "## Privacy scan", "", "| Field | Value |", "| --- | --- |"]
    for name, value in sorted(document["privacy_scan"].items()):
        lines.append(f"| `{name}` | {_cell(value)} |")

    lines += ["", "## Run", "", "| Field | Value |", "| --- | --- |"]
    for name, value in sorted(document["run"].items()):
        lines.append(f"| `{name}` | {_cell(value)} |")

    lines += ["", "## Limitations", ""]
    for limitation in document["limitations"]:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


def _metric_equation(metrics: Mapping) -> list[str]:
    """The metric balance written out, so a reader can check the arithmetic without the source.

    The exporter is shared by the OTLP metrics pipeline and the diagnostic metrics pipeline, so
    `exported` is larger than `accepted` by however many points the `count` connectors made. That
    is a property of the topology, not a discrepancy, and a report that printed only the two
    counters would read as though points had appeared from nowhere.
    """
    if any(metrics.get(name) is None for name in ("accepted", "internally_generated", "exported")):
        return ["The metric balance is unavailable: the run did not reach a countable state."]
    return [
        "Metric points enter through two doors and leave through one. The receiver counter sees "
        "only the first door:",
        "",
        "```",
        f"  {metrics['accepted']:>5}  accepted from Codex at the OTLP receiver",
        f"+ {metrics['internally_generated']:>5}  generated inside the Collector by the "
        "diagnostic count connectors",
        f"= {metrics['available']:>5}  entered a metric pipeline",
        f"- {metrics['exported']:>5}  sent by the debug exporter (both pipelines share it)",
        f"- {metrics['refused']:>5}  refused at the receiver",
        f"- {metrics['send_failed']:>5}  failed to send at the exporter",
        f"= {metrics['unresolved']:>5}  unresolved remainder",
        "```",
    ]


def _cell(value: object) -> str:
    """One Markdown cell. Bounded: a mapping or list is rendered as sorted `key=count` pairs."""
    if isinstance(value, bool) or value is None:
        return {True: "yes", False: "no", None: "—"}[value]
    if isinstance(value, Mapping):
        return ", ".join(f"`{key}`={value[key]}" for key in sorted(value)) or "none"
    if isinstance(value, (list, tuple)):
        return ", ".join(f"`{item}`" for item in value) or "none"
    return f"`{value}`"


def write_reports(document: Mapping, output_dir: Path) -> tuple[Path, Path]:
    """Write the JSON and Markdown reports — or stubs instead of leaking ones.

    The final bytes are scanned again here. A report that would publish something forbidden is not
    written: a stub naming the patterns that matched takes its place, so the failure is visible
    without the finding being the leak.
    """
    json_text = inventory.render_json(document)
    markdown_text = render_markdown(document)
    leaked = tuple(
        sorted(
            set(inventory.scan_forbidden_output(json_text))
            | set(inventory.scan_forbidden_output(markdown_text))
        )
    )
    json_path = output_dir / REPORT_JSON_NAME
    markdown_path = output_dir / REPORT_MARKDOWN_NAME
    if leaked:
        stub = {
            "schema": SCHEMA_VERSION,
            "gate7": {"verdict": "FAIL", "conditions": []},
            "withheld": {
                "reason": "the rendered report matched a forbidden-output pattern and was withheld",
                "patterns_found": list(leaked),
            },
        }
        _write(json_path, inventory.render_json(stub))
        _write(
            markdown_path,
            "# Codex telemetry canary (Gate 7)\n\n## Gate 7 verdict: FAIL\n\n"
            "The rendered report matched a forbidden-output pattern and was withheld.\n\n"
            + "".join(f"- `{name}`\n" for name in leaked),
        )
        return json_path, markdown_path
    _write(json_path, json_text)
    _write(markdown_path, markdown_text)
    return json_path, markdown_path


def _write(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings on every platform, so a report hashes the same everywhere."""
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def canary_configuration() -> dict:
    """The Collector configuration a Codex canary runs: the shipped file plus the diagnostic.

    Nothing else. No receiver, exporter, connector, or pipeline is added, so there is no route by
    which telemetry could reach a report without first passing `memory_limiter`,
    `filter/unredactable`, and `transform/privacy` — and no `logs` pipeline for a Codex log record to
    land in.
    """
    return certification.diagnostic_configuration(certification.load_configuration())


@dataclass
class CodexOutcome:
    """What the Codex child did, filled in by the agent and read by the report builder."""

    returncode: int | None = None
    timed_out: bool = False
    transcript: CodexTranscript | None = None
    diagnostics: CodexDiagnostics | None = None
    errors: tuple[str, ...] = ()


def codex_agent(
    outcome: CodexOutcome,
    *,
    invocation_id: str,
    executable: str = CODEX_EXECUTABLE,
    timeout: float = CODEX_CANARY_TIMEOUT,
) -> Callable[[dict, Path], int | None]:
    """The real agent: one Codex child, in a fresh empty temporary directory outside the repository.

    Every per-invocation value is built here, inside the run, from the ports the Collector actually
    bound: the endpoint is that run's own loopback port, and the `-c` overrides carry that run's own
    correlation identifier. Nothing is assembled ahead of time and nothing is reused.

    The working directory is created under the system temporary directory — never inside the
    repository, never a git worktree — and is removed on every path including a timeout. It holds the
    child's stdout, which is Codex JSONL, which contains the model's reply. The reply is parsed into
    counts inside that directory and the directory is then deleted; no text leaves this function.

    Stdout and stderr are read from separate files. Only stdout is Codex JSONL; stderr is reduced to
    a line count and discarded with the directory.
    """

    def agent(ports: dict, workdir: Path) -> int | None:
        endpoint = f"http://127.0.0.1:{ports['http']}"
        context = agent_telemetry.InvocationContext(
            invocation_id=invocation_id,
            contract_version=agent_telemetry.AGENT_TELEMETRY_CONTRACT_VERSION,
            traceparent=None,
        )
        overrides = agent_telemetry.build_codex_otel_overrides(context, endpoint=endpoint)
        argv = build_codex_argv(overrides=overrides, executable=executable)
        environment = agent_telemetry.build_codex_child_environment(os.environ, endpoint=endpoint)
        # Through the same shim resolution `CodexRunner` uses, because "the override survives the
        # Windows shim" is one of the things this canary exists to find out.
        command = _build_process_command(argv[0], list(argv[1:]))

        isolated = Path(tempfile.mkdtemp(prefix="forge-codex-canary-"))
        try:
            returncode = canary.subprocess_agent(
                command,
                environment,
                timeout=timeout,
                on_timeout=lambda: setattr(outcome, "timed_out", True),
            )(ports, isolated)
            outcome.returncode = returncode
            # stdout only. The stderr file beside it is diagnostic text and is never parsed here.
            log = isolated / canary.AGENT_STDOUT_NAME
            if log.is_file():
                outcome.transcript = parse_codex_transcript(
                    log.read_text(encoding="utf-8", errors="replace")
                )
            else:
                outcome.errors += ("Codex produced no output file to parse",)
            diagnostic = isolated / canary.AGENT_STDERR_NAME
            outcome.diagnostics = summarize_stderr(
                diagnostic.read_text(encoding="utf-8", errors="replace")
                if diagnostic.is_file()
                else ""
            )
            return returncode
        finally:
            shutil.rmtree(isolated, ignore_errors=True)

    return agent


@dataclass(frozen=True)
class CodexCanaryRun:
    canary: canary.CanaryReport
    document: dict
    json_path: Path | None
    markdown_path: Path | None
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors and self.document.get("gate7", {}).get("verdict") == "PASS"

    def render(self) -> str:
        gate = self.document.get("gate7", {})
        lines = ["CODEX CANARY (GATE 7)"]
        for key, title in _MARKDOWN_SECTIONS:
            lines.append(f"  {title}")
            for name, value in sorted(self.document.get(key, {}).items()):
                lines.append(f"    {name:<34} {_plain(value)}")
        for condition in gate.get("conditions", []):
            if not condition["met"]:
                lines.append(f"  UNMET {condition['condition']}")
        for error in self.errors:
            lines.append(f"  ERROR {error}")
        lines += ["", self.canary.render(), "", f"GATE 7 {gate.get('verdict', 'FAIL')}"]
        return "\n".join(lines)


def _plain(value: object) -> str:
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={value[key]}" for key in sorted(value)) or "none"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "none"
    return str(value)


def run_codex_canary(
    *,
    binary: Path,
    workdir: Path,
    output_dir: Path,
    agent: Callable[[dict, Path], int | None],
    outcome: CodexOutcome,
    invocation_id: str,
    startup_timeout: float = 60.0,
    settle_timeout: float = 180.0,
) -> CodexCanaryRun:
    """Run one Codex canary and report what the Collector received.

    The output directory is prepared **before** the Collector starts, so a refusal starts nothing.
    The Collector and the child are reaped inside :func:`otel_canary.run_canary`, which returns before
    any report is built — so a failure in report generation cannot leave a process behind, and is
    recorded rather than raised.
    """
    inventory.prepare_output_directory(output_dir)
    config = canary_configuration()
    report = canary.run_canary(
        binary=binary,
        workdir=workdir,
        agent=agent,
        config=config,
        startup_timeout=startup_timeout,
        settle_timeout=settle_timeout,
        probe=read_transport,
    )
    errors: list[str] = []
    try:
        document = build_document(
            report,
            transcript=outcome.transcript,
            invocation_id=invocation_id,
            timed_out=outcome.timed_out,
            codex_returncode=outcome.returncode,
            diagnostics=outcome.diagnostics,
            config=config,
            errors=outcome.errors,
        )
    except Exception as error:  # noqa: BLE001 - the report is the deliverable, not a traceback
        reason = f"the report could not be built: {type(error).__name__}: {error}"
        errors.append(reason)
        document = build_document(
            canary.CanaryReport(
                accounting=report.accounting,
                privacy=report.privacy,
                collector_returncode=report.collector_returncode,
                agent_returncode=report.agent_returncode,
                errors=report.errors + (reason,),
                output="",
            ),
            transcript=None,
            invocation_id="",
            config=config,
            errors=outcome.errors,
        )
    paths: tuple[Path | None, Path | None] = (None, None)
    try:
        paths = write_reports(document, output_dir)
    except Exception as error:  # noqa: BLE001 - a report that cannot be written is a result
        errors.append(f"the report could not be written: {type(error).__name__}: {error}")
    return CodexCanaryRun(
        canary=report,
        document=document,
        json_path=paths[0],
        markdown_path=paths[1],
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

APPROVAL_REQUIRED = (
    "Refusing to launch Codex. A Gate 7 canary is a supervised event: re-run with --approved-by "
    "naming the human who approved it. Without it this harness starts nothing — no Collector, no "
    "Codex, and no output directory."
)

COLLECTOR_PATH_HINT = inventory.COLLECTOR_PATH_HINT


def render_powershell_command(
    output_dir: str, approved_by: str, *, collector_path: str = COLLECTOR_PATH_HINT
) -> str:
    """The exact PowerShell invocation for the later supervised run, built from the parser itself.

    The Codex vector deliberately does **not** appear: it is a constant in this module, so there is
    no command string for a shell to mangle and no way for an argument to be lost in the quoting.
    """
    return "\n".join(
        [
            f"$env:OTELCOL_PATH = {_powershell_literal(collector_path)}",
            ".\\.venv\\Scripts\\python.exe tests\\otel_codex_canary.py `",
            f"    --output-dir {_powershell_literal(output_dir)} `",
            f"    --approved-by {_powershell_literal(approved_by)}",
        ]
    )


def _powershell_literal(value: str) -> str:
    """A PowerShell single-quoted literal. Nothing inside is expanded; `'` is doubled."""
    return "'" + value.replace("'", "''") + "'"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="otel_codex_canary",
        description=(
            "Run one supervised Codex canary against the local Collector and report whether its "
            "native telemetry arrived, correlated, and stayed within the contract. Launches nothing "
            "without --approved-by."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="where the reports are written")
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="where the Collector writes its log; defaults to <output-dir>-run",
    )
    parser.add_argument(
        "--approved-by",
        default="",
        help="the human who approved this supervised run; required before anything is launched",
    )
    parser.add_argument(
        "--codex-command",
        default="",
        help=(
            "override the Codex command line; must parse to the approved canary vector, which is "
            "used by default and needs no override"
        ),
    )
    parser.add_argument(
        "--codex-executable",
        default=CODEX_EXECUTABLE,
        help="the Codex executable to run; resolved through the same shim CodexRunner uses",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved Codex vector and the PowerShell invocation, and start nothing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    vector, problems = resolve_codex_argv(
        arguments.codex_command, executable=arguments.codex_executable
    )
    if problems:
        for problem in problems:
            print(f"Refusing this Codex command: {problem}", file=sys.stderr)
        return 2

    if arguments.dry_run:
        # Prints and exits. No approval is required because nothing is started, no output directory
        # is touched, and no Collector binary is located. Only the vector is printed: no environment
        # variable, no value read from one, and no credential appears below.
        print("codex argv:")
        for index, argument in enumerate(vector):
            print(f"  [{index}] {argument!r}")
        print("")
        print(
            f"the port shown is a placeholder ({PLACEHOLDER_ENDPOINT}); the run binds an ephemeral "
            "loopback port"
        )
        print(
            "the forge.invocation.id shown is a placeholder of the approved shape; a fresh random "
            "one is minted inside the run"
        )
        print("")
        print("powershell:")
        print(
            render_powershell_command(
                str(arguments.output_dir), arguments.approved_by or "<the human approving this run>"
            )
        )
        print("")
        print("no process was started: no Collector, no Codex, and no output directory was created.")
        return 0

    # Approval is checked before the binary is located and before the output directory is touched.
    # Nothing at all happens without it.
    if not arguments.approved_by.strip():
        print(APPROVAL_REQUIRED, file=sys.stderr)
        return 2

    try:
        binary = certification.collector_binary()
        installed = certification.collector_version(binary)
    except (certification.CollectorUnavailable, certification.CollectorFailed) as error:
        print(error, file=sys.stderr)
        return 2
    if installed != certification.CERTIFIED_COLLECTOR_VERSION:
        print(
            f"Collector {installed} is not the certified {certification.CERTIFIED_COLLECTOR_VERSION}; "
            "re-run Gate 3 before a Codex canary.",
            file=sys.stderr,
        )
        return 2

    output_dir = arguments.output_dir
    workdir = arguments.workdir or output_dir.with_name(output_dir.name + "-run")
    invocation_id = agent_telemetry.new_invocation_id()
    outcome = CodexOutcome()
    try:
        result = run_codex_canary(
            binary=binary,
            workdir=workdir,
            output_dir=output_dir,
            # `vector[0]` rather than the option: a supplied `--codex-command` may name the
            # executable by path, and the run must use the one the refusal checks were applied to.
            agent=codex_agent(outcome, invocation_id=invocation_id, executable=vector[0]),
            outcome=outcome,
            invocation_id=invocation_id,
        )
    except inventory.OutputDirectoryRefused as error:
        print(error, file=sys.stderr)
        return 2
    print(result.render())
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
