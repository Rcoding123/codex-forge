"""Local, privacy-safe OpenTelemetry tracing for the Forge orchestration path.

The trace shape is three nested spans with fixed names:

    forge.task
    └── forge.attempt
        └── forge.phase

Span *names* are constant. Everything that varies — the task id, the phase, the repair cycle —
is an attribute, so the number of distinct span names a backend sees never grows with the number
of tasks Forge runs.

Design rules this module exists to enforce:

* **Off by default.** With `[telemetry] enabled = false` (the default, and the behavior of a
  forge.toml that has no `[telemetry]` section at all) no provider is built, no exporter is
  constructed, no socket is opened, and every span helper is a cheap no-op.
* **Telemetry never fails a task.** Every OpenTelemetry call is wrapped. An import error, an
  unreachable collector, or an exporter fault degrades to "no spans" and is reported as a bounded,
  sanitized warning. Orchestration control flow does not observe telemetry state.
* **Nothing sensitive leaves.** Only bounded vocabularies, opaque identifiers, and integers become
  attributes. Exception *messages* are never recorded — Forge exception text routinely embeds
  command output, stderr tails, and absolute paths — so spans carry `error.type` (the class name)
  and a bounded `error.code` instead. Span status is set without a description for the same reason.
* **Configuration comes from forge.toml only.** Endpoint, transport security, timeout, resource,
  sampler, and batch sizing are all passed explicitly, so the SDK never consults the environment
  for them. OTLP request headers are the one setting that cannot be passed explicitly — the
  exporter resolves them as `headers or environ.get(...)`, and an empty tuple is falsy — so they
  are suppressed for the exporter construction alone; see `_without_exporter_headers`.

Known limitation — *this slice does not trace individual model requests.* A `forge.phase` span
covers one `codex exec` invocation, and one such invocation may internally issue many model
requests. The usage attributes on a phase span are therefore the total the worker reported for the
whole invocation, not per-request telemetry, and `gen_ai.request.model` names the model Forge
asked for rather than the model that served each internal request. Per-model-call spans are a
later slice.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from .config import TelemetryConfig
from .usage import (
    ACCOUNTING_BASIS,
    BUDGET_BASIS,
    cache_hit_ratio,
    coerce_token_count,
    normalize_run_usage,
)

# --------------------------------------------------------------------------------------------
# Bounded vocabularies. Every value a span attribute may take is enumerated here so a reviewer can
# see the whole cardinality surface in one place, and so the tests can assert against it.
# --------------------------------------------------------------------------------------------

SPAN_TASK = "forge.task"
SPAN_ATTEMPT = "forge.attempt"
SPAN_PHASE = "forge.phase"
SPAN_NAMES = frozenset({SPAN_TASK, SPAN_ATTEMPT, SPAN_PHASE})

PHASE_IMPLEMENTATION = "implementation"
PHASE_VALIDATION_INITIAL = "validation_initial"
PHASE_VALIDATION_REPAIR = "validation_repair"
PHASE_REVIEW = "review"
PHASE_REVIEW_REPAIR = "review_repair"
PHASE_VALIDATION_FINAL = "validation_final"
PHASE_EVIDENCE = "evidence"
PHASE_COMPLETION = "completion"
# Any label this module does not recognize collapses here. Without it a future orchestrator phase
# would either leak an unbounded name into telemetry or raise inside instrumentation.
PHASE_OTHER = "other"
PHASE_NAMES = frozenset(
    {
        PHASE_IMPLEMENTATION,
        PHASE_VALIDATION_INITIAL,
        PHASE_VALIDATION_REPAIR,
        PHASE_REVIEW,
        PHASE_REVIEW_REPAIR,
        PHASE_VALIDATION_FINAL,
        PHASE_EVIDENCE,
        PHASE_COMPLETION,
        PHASE_OTHER,
    }
)

ROLE_IMPLEMENTER = "implementer"
ROLE_REVIEWER = "reviewer"
ROLE_VALIDATOR = "validator"
ROLE_CONTROLLER = "controller"
AGENT_ROLES = frozenset({ROLE_IMPLEMENTER, ROLE_REVIEWER, ROLE_VALIDATOR, ROLE_CONTROLLER})

ATTEMPT_KIND_FULL = "full"
ATTEMPT_KINDS = frozenset({ATTEMPT_KIND_FULL})

RETRY_BUCKETS = ("0", "1", "2", "3+")

OUTCOME_COMPLETED = "completed"
OUTCOME_NEEDS_DECISION = "needs_decision"
OUTCOME_NONCOMPLETION = "noncompletion"
OUTCOME_VALIDATION_FAILED = "validation_failed"
OUTCOME_REVIEW_CHANGES_REQUIRED = "review_changes_required"
OUTCOME_BUDGET_EXCEEDED = "budget_exceeded"
OUTCOME_ERROR = "error"
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"

TASK_OUTCOMES = frozenset(
    {
        OUTCOME_COMPLETED,
        OUTCOME_NEEDS_DECISION,
        OUTCOME_NONCOMPLETION,
        OUTCOME_VALIDATION_FAILED,
        OUTCOME_REVIEW_CHANGES_REQUIRED,
        OUTCOME_BUDGET_EXCEEDED,
        OUTCOME_ERROR,
    }
)
PHASE_OUTCOMES = frozenset(
    {OUTCOME_OK, OUTCOME_FAILED, OUTCOME_SKIPPED, OUTCOME_BUDGET_EXCEEDED, OUTCOME_ERROR}
)

TERMINATED_BY_COMPLETION = "completion"
TERMINATED_BY_CODEX_NONCOMPLETION = "codex_noncompletion"
TERMINATED_BY_VALIDATION_FAILURE = "validation_failure"
TERMINATED_BY_REVIEW_VERDICT = "review_verdict"
TERMINATED_BY_BUDGET = "budget"
TERMINATED_BY_EXCEPTION = "exception"
TERMINATION_REASONS = frozenset(
    {
        TERMINATED_BY_COMPLETION,
        TERMINATED_BY_CODEX_NONCOMPLETION,
        TERMINATED_BY_VALIDATION_FAILURE,
        TERMINATED_BY_REVIEW_VERDICT,
        TERMINATED_BY_BUDGET,
        TERMINATED_BY_EXCEPTION,
    }
)

ERROR_TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
ERROR_CODEX_EXIT_NONZERO = "codex_exit_nonzero"
ERROR_VALIDATION_FAILED = "validation_failed"
ERROR_REVIEW_CHANGES_REQUIRED = "review_changes_required"
ERROR_CODEX_NONCOMPLETION = "codex_noncompletion"
ERROR_UNEXPECTED = "unexpected_error"
ERROR_CODES = frozenset(
    {
        ERROR_TOKEN_BUDGET_EXCEEDED,
        ERROR_CODEX_EXIT_NONZERO,
        ERROR_VALIDATION_FAILED,
        ERROR_REVIEW_CHANGES_REQUIRED,
        ERROR_CODEX_NONCOMPLETION,
        ERROR_UNEXPECTED,
    }
)

# A task outcome that is not `completed` carries a matching bounded error code, so a backend can
# alert on failures without parsing any free text.
ERROR_CODE_BY_OUTCOME = {
    OUTCOME_NONCOMPLETION: ERROR_CODEX_NONCOMPLETION,
    OUTCOME_VALIDATION_FAILED: ERROR_VALIDATION_FAILED,
    OUTCOME_REVIEW_CHANGES_REQUIRED: ERROR_REVIEW_CHANGES_REQUIRED,
    OUTCOME_BUDGET_EXCEEDED: ERROR_TOKEN_BUDGET_EXCEEDED,
    OUTCOME_ERROR: ERROR_UNEXPECTED,
}

TERMINATED_BY_OUTCOME = {
    OUTCOME_COMPLETED: TERMINATED_BY_COMPLETION,
    OUTCOME_NEEDS_DECISION: TERMINATED_BY_CODEX_NONCOMPLETION,
    OUTCOME_NONCOMPLETION: TERMINATED_BY_CODEX_NONCOMPLETION,
    OUTCOME_VALIDATION_FAILED: TERMINATED_BY_VALIDATION_FAILURE,
    OUTCOME_REVIEW_CHANGES_REQUIRED: TERMINATED_BY_REVIEW_VERDICT,
    OUTCOME_BUDGET_EXCEEDED: TERMINATED_BY_BUDGET,
    OUTCOME_ERROR: TERMINATED_BY_EXCEPTION,
}

# Attribute keys. Grouped by span so the documentation and the tests share one source.
ATTR_TASK_ID = "forge.task.id"
ATTR_PROJECT_ID = "forge.project.id"
ATTR_TASK_RISK = "forge.task.risk"
ATTR_TASK_MAX_ATTEMPTS = "forge.task.max_attempts"
ATTR_TASK_OUTCOME = "forge.task.outcome"
ATTR_ATTEMPT_NUMBER = "forge.attempt.number"
ATTR_ATTEMPT_KIND = "forge.attempt.kind"
ATTR_RETRY_BUCKET = "forge.retry.bucket"
ATTR_ATTEMPT_OUTCOME = "forge.attempt.outcome"
ATTR_ATTEMPT_TERMINATED_BY = "forge.attempt.terminated_by"
ATTR_PHASE_NAME = "forge.phase.name"
ATTR_PHASE_CYCLE = "forge.phase.cycle"
ATTR_AGENT_ROLE = "forge.agent.role"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_REQUEST_EFFORT = "gen_ai.request.reasoning_effort"
ATTR_PHASE_OUTCOME = "forge.phase.outcome"
ATTR_PHASE_DURATION_MS = "forge.phase.duration_ms"
ATTR_ERROR_TYPE = "error.type"
ATTR_ERROR_CODE = "error.code"

ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_CACHED_INPUT_TOKENS = "gen_ai.usage.cached_input_tokens"
ATTR_FRESH_INPUT_TOKENS = "forge.usage.fresh_input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_REASONING_TOKENS = "gen_ai.usage.reasoning_tokens"
ATTR_RAW_TOTAL_TOKENS = "forge.usage.raw_total_tokens"
ATTR_CACHE_HIT_RATIO = "forge.usage.cache_hit_ratio"
ATTR_ACCOUNTING_BASIS = "forge.usage.accounting_basis"
ATTR_BUDGET_BASIS = "forge.usage.budget_basis"


# Attributes whose value must stay inside a small enumerated set, because a backend will group or
# alert on them the way it would group a metric label. The cardinality test asserts exactly this.
LOW_CARDINALITY_ATTRIBUTES = frozenset(
    {
        ATTR_TASK_RISK,
        ATTR_TASK_OUTCOME,
        ATTR_ATTEMPT_KIND,
        ATTR_RETRY_BUCKET,
        ATTR_ATTEMPT_OUTCOME,
        ATTR_ATTEMPT_TERMINATED_BY,
        ATTR_PHASE_NAME,
        ATTR_AGENT_ROLE,
        ATTR_PHASE_OUTCOME,
        ATTR_ERROR_CODE,
        ATTR_ACCOUNTING_BASIS,
        ATTR_BUDGET_BASIS,
    }
)

# Defensive caps. Nothing routed through this module should approach them; they exist so that a
# future caller passing something unexpected truncates instead of publishing it.
MAX_ATTRIBUTE_CHARS = 120
MAX_WARNINGS = 5
MAX_PHASE_CYCLE = 999
EXPORT_TIMEOUT_SECONDS = 5
SHUTDOWN_TIMEOUT_SECONDS = 5.0

# The cycle group is length-bounded on purpose. An unbounded `\d+` would hand a long digit run to
# `int()`, which raises above CPython's 4300-digit conversion limit — a raise inside instrumentation
# that would surface as a task failure.
_CYCLE_SUFFIX = re.compile(r"(?P<stem>.*?)-(?P<cycle>\d{1,6})")

RISK_LEVELS = ("low", "medium", "high", "critical")
RISK_OTHER = "other"
RISK_VALUES = frozenset({*RISK_LEVELS, RISK_OTHER})

# Identifiers are opaque when Forge generates them (`TASK-<hash>`, `project-<hash>`), but a task id
# may also be supplied by an operator (`--id`, or `id:` in an inbox YAML file) and is otherwise
# unconstrained.
#
# An identifier is therefore accepted only if it already has the shape of one. Filtering unsafe
# *characters* is not enough: replacing the separators in
# "Repo https://github.com/acme/private" yields "Repo-https-github.com-acme-private", which still
# publishes the hostname and the prose. Anything that does not match is replaced wholesale by a
# digest, which keeps spans correlatable across a task without carrying any of its content.
MAX_IDENTIFIER_CHARS = 64
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,%d}" % (MAX_IDENTIFIER_CHARS - 1))


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


def _split_cycle(label: str) -> tuple[str, int]:
    """Split an orchestrator phase label such as `validation-repair-2` into (`stem`, cycle)."""
    match = _CYCLE_SUFFIX.fullmatch(label)
    if not match:
        return label, 0
    return match.group("stem"), min(int(match.group("cycle")), MAX_PHASE_CYCLE)


def normalize_phase(raw: str, *, kind: str = "codex") -> tuple[str, int]:
    """Map an orchestrator phase label onto a bounded phase name and a numeric repair cycle.

    The cycle is deliberately separated from the name: `validation-repair-1` and
    `validation-repair-2` are the same phase observed on different cycles, and folding the number
    into the name would make span names grow with the repair limit.

    `kind` disambiguates the two label namespaces the orchestrator uses. A Codex phase label like
    `review-repair-1` names a repair *run*, while the validation-suffix label of the same name
    describes the validation that followed it.
    """
    stem, cycle = _split_cycle(str(raw or "").strip().lower())
    if kind == "validation":
        if stem in {"", "initial"}:
            return PHASE_VALIDATION_INITIAL, cycle
        if stem == "final":
            return PHASE_VALIDATION_FINAL, cycle
        if stem in {"repair", "review-repair"}:
            return PHASE_VALIDATION_REPAIR, cycle
        return PHASE_OTHER, cycle
    if stem == "implementation":
        return PHASE_IMPLEMENTATION, cycle
    if stem == "validation-repair":
        return PHASE_VALIDATION_REPAIR, cycle
    if stem == "review-repair":
        return PHASE_REVIEW_REPAIR, cycle
    if stem == "review":
        # The first review is labelled `review`; follow-up reviews are `review-2`, `review-3`, ...
        # so that a follow-up reports the repair cycle it belongs to rather than an off-by-one.
        return PHASE_REVIEW, max(0, cycle - 1)
    if stem == PHASE_EVIDENCE:
        return PHASE_EVIDENCE, cycle
    if stem == PHASE_COMPLETION:
        return PHASE_COMPLETION, cycle
    return PHASE_OTHER, cycle


def normalize_risk(raw: Any) -> str:
    """Map a task's risk onto a bounded value.

    `forge.task.risk` is grouped like a metric label, so it must not be free text. The submission
    contract validates risk against `RISK_LEVELS`, but the inbox YAML importer accepts
    `str(data.get("risk", "medium"))` unvalidated, so an imported task can carry arbitrary text —
    including a path or a sentence — into this attribute unless it is bounded here.
    """
    if not isinstance(raw, str):
        return RISK_OTHER
    value = raw.strip().lower()
    return value if value in RISK_LEVELS else RISK_OTHER


def safe_identifier(raw: Any) -> str:
    """Return an identifier that is safe to publish, or an opaque digest standing in for it.

    A value that already looks like an identifier passes through unchanged, so Forge's own
    `TASK-<hash>` and `project-<hash>` ids stay readable. Anything else — a path, a URL, a
    sentence — is replaced by `opaque-<digest>` rather than scrubbed, because scrubbing separators
    leaves the words behind. The digest is stable, so spans for the same task still correlate.
    """
    if not isinstance(raw, str):
        raw = str(raw)
    candidate = raw.strip()
    if _SAFE_IDENTIFIER.fullmatch(candidate):
        return candidate
    return "opaque-" + hashlib.sha256(candidate.encode("utf-8", "replace")).hexdigest()[:12]


def retry_bucket(attempt_number: Any) -> str:
    """Bucket an attempt number into `0`, `1`, `2`, or `3+`.

    Attempts are 1-based, so the first attempt has made zero retries. The open-ended top bucket
    keeps the attribute's cardinality fixed no matter how large `max_attempts` is configured.
    """
    try:
        retries = max(0, int(attempt_number) - 1)
    except (TypeError, ValueError):
        return RETRY_BUCKETS[0]
    return RETRY_BUCKETS[retries] if retries < len(RETRY_BUCKETS) - 1 else RETRY_BUCKETS[-1]


def usage_attributes(counts: Mapping[str, Any]) -> dict[str, Any]:
    """Project categorized token counts onto span attributes.

    Accepts either one run's `normalize_run_usage` output or a task's `aggregate_token_usage`
    totals — both use the same field names. No token arithmetic happens here: the categories and
    the cache ratio come from `codex_forge.usage`, so telemetry can never disagree with the number
    the control-plane contract and the token budget report for the same task.

    Every field goes through the shared `coerce_token_count`, which makes this function total. A
    malformed count degrades to zero rather than raising an exception that instrumentation would
    then propagate into a task's control flow.
    """
    cached = coerce_token_count(counts.get("cached_input_tokens"))
    reported_input = coerce_token_count(counts.get("input_tokens"))
    ratio = counts.get("cache_hit_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        ratio = cache_hit_ratio(cached, reported_input)
    return {
        ATTR_INPUT_TOKENS: reported_input,
        ATTR_CACHED_INPUT_TOKENS: cached,
        ATTR_FRESH_INPUT_TOKENS: coerce_token_count(counts.get("fresh_input_tokens")),
        ATTR_OUTPUT_TOKENS: coerce_token_count(counts.get("output_tokens")),
        ATTR_REASONING_TOKENS: coerce_token_count(counts.get("reasoning_output_tokens")),
        ATTR_RAW_TOTAL_TOKENS: coerce_token_count(counts.get("raw_total_tokens")),
        ATTR_CACHE_HIT_RATIO: float(ratio),
        ATTR_ACCOUNTING_BASIS: ACCOUNTING_BASIS,
        ATTR_BUDGET_BASIS: BUDGET_BASIS,
    }


def phase_usage_attributes(raw_usage: Any) -> dict[str, Any]:
    """Project one Codex run's reported usage onto span attributes."""
    return usage_attributes(normalize_run_usage(raw_usage))


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Return the bounded (outcome, error code) pair for an exception.

    Matched on class name rather than by importing the orchestrator, which imports this module.
    """
    if type(exc).__name__ == "TaskUsageBudgetExceeded":
        return OUTCOME_BUDGET_EXCEEDED, ERROR_TOKEN_BUDGET_EXCEEDED
    return OUTCOME_ERROR, ERROR_UNEXPECTED


def _bounded(value: Any) -> Any:
    """Cap an attribute value. Numbers pass through; anything else becomes a truncated string."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    return text[:MAX_ATTRIBUTE_CHARS]


# The only environment variables that can put operator data onto a Forge span. Every other SDK
# setting Forge cares about — endpoint, insecure, timeout, resource, sampler, batch sizing — is
# passed explicitly at construction, so the SDK never consults the environment for it. These two
# alone cannot be passed explicitly: the exporter resolves headers as
# `headers or environ.get(...)`, and an empty tuple is falsy, so it falls through to the
# environment no matter what is passed.
_HEADER_ENVIRONMENT_KEYS = ("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_HEADERS")

# `os.environ` is process-global, so the suppression window is serialized across every Telemetry
# instance rather than per-instance.
_ENVIRONMENT_LOCK = threading.Lock()


@contextmanager
def _without_exporter_headers() -> Iterator[None]:
    """Construct the exporter with the OTLP header variables suppressed, then restore them exactly.

    Scoped deliberately narrowly. An earlier revision removed every `OTEL_*` variable, which touched
    settings that carry no operator data, overrode the operator's `OTEL_SDK_DISABLED` kill switch,
    and widened the window in which a child process could inherit a modified environment. Suppressing
    only the header variables gives the identical privacy guarantee — a stray
    `OTEL_EXPORTER_OTLP_HEADERS` cannot attach a credential to a Forge span — with the smallest
    possible blast radius.

    Restoration is exact on every path: a variable that was absent beforehand is absent afterwards,
    including one created inside the window.
    """
    with _ENVIRONMENT_LOCK:
        saved = {key: os.environ.get(key) for key in _HEADER_ENVIRONMENT_KEYS}
        for key in _HEADER_ENVIRONMENT_KEYS:
            os.environ.pop(key, None)
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _package_version() -> str | None:
    """Return the installed distribution version, or None when it cannot be read safely."""
    try:
        from importlib.metadata import version

        return version("codex-forge")
    except Exception:
        return None


# --------------------------------------------------------------------------------------------
# Span handles
# --------------------------------------------------------------------------------------------


class SpanHandle:
    """A guarded handle to one span, or to nothing at all when telemetry is off.

    Every method is total: with no underlying span each call returns immediately, and with one, a
    failure inside the SDK is captured as a sanitized warning rather than raised into the caller.
    """

    __slots__ = ("_span", "_owner", "_outcome_key", "_error_code", "_outcome")

    def __init__(self, span: Any, owner: "Telemetry", outcome_key: str | None = None) -> None:
        self._span = span
        self._owner = owner
        self._outcome_key = outcome_key
        self._error_code: str | None = None
        self._outcome: str | None = None

    @property
    def recording(self) -> bool:
        """True when attribute work is worth doing at all."""
        return self._span is not None

    def set(self, key: str, value: Any) -> None:
        if self._span is None or value is None:
            return
        try:
            self._span.set_attribute(key, _bounded(value))
        except Exception as exc:
            self._owner._warn("attribute", exc)

    def set_many(self, attributes: Mapping[str, Any]) -> None:
        for key, value in attributes.items():
            self.set(key, value)

    def set_outcome(self, outcome: str) -> None:
        """Record the bounded outcome, and the matching error code when it is not a success."""
        self._outcome = outcome
        if self._outcome_key:
            self.set(self._outcome_key, outcome)
        code = ERROR_CODE_BY_OUTCOME.get(outcome)
        if code:
            self.set_error_code(code)

    def set_error_code(self, code: str) -> None:
        """Record a bounded error code. The first code recorded wins, being the most specific."""
        if self._error_code is not None:
            return
        self._error_code = code
        self.set(ATTR_ERROR_CODE, code)

    def record_error(self, exc: BaseException) -> None:
        """Mark the span failed from an exception, without recording the exception's message.

        Forge exception text carries stderr tails, command output, and absolute worktree paths, so
        neither `record_exception` (which stores the message as a span event) nor a status
        description is used. The class name and a bounded code are enough to alert on.
        """
        outcome, code = classify_exception(exc)
        self.set_error_code(code)
        if self._outcome is None:
            self.set_outcome(outcome)
        self.set(ATTR_ERROR_TYPE, type(exc).__name__)
        if self._span is None or self._owner._error_status is None:
            return
        try:
            self._span.set_status(self._owner._error_status)
        except Exception as status_exc:
            self._owner._warn("status", status_exc)


# --------------------------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------------------------


class Telemetry:
    """Owns a private tracer provider and hands out guarded span context managers.

    The provider is private rather than installed as the global OpenTelemetry provider: Forge is
    an application, but this keeps repeated initialization harmless, keeps tests independent of
    process-global state, and leaves any host process's own provider untouched.
    """

    def __init__(
        self,
        config: TelemetryConfig,
        *,
        processor_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._config = config
        # Tests inject an in-memory span processor here. Left as None, the OTLP/gRPC exporter is
        # built from the configured loopback endpoint.
        self._processor_factory = processor_factory
        self._lock = threading.Lock()
        self._started = False
        self._provider: Any = None
        self._tracer: Any = None
        self._error_status: Any = None
        self._warnings: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    @property
    def active(self) -> bool:
        """True once a tracer exists. False when disabled or when initialization failed."""
        return self._tracer is not None

    @property
    def warnings(self) -> tuple[str, ...]:
        """Bounded, sanitized initialization and instrumentation warnings."""
        with self._lock:
            return tuple(self._warnings)

    def _warn(self, stage: str, exc: BaseException) -> None:
        """Record that a telemetry stage failed, naming only the stage and the exception class.

        The message is never included. Exporter and socket errors embed the endpoint, and OS errors
        embed filesystem paths; neither belongs in a warning that an operator may paste anywhere.
        """
        with self._lock:
            if len(self._warnings) >= MAX_WARNINGS:
                return
            self._warnings.append(f"telemetry {stage} unavailable ({type(exc).__name__})")

    def start(self) -> None:
        """Initialize the provider once. Safe to call repeatedly and from any thread."""
        if not self.enabled:
            return
        with self._lock:
            if self._started:
                return
            # Marked started before building, so a failing initialization is attempted once rather
            # than retried on every task.
            self._started = True
            try:
                self._build()
            except Exception as exc:
                self._provider = None
                self._tracer = None
                self._warnings.append(f"telemetry init unavailable ({type(exc).__name__})")

    def _build(self) -> None:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        from opentelemetry.trace import Status, StatusCode

        # Built without a description: a status description would carry the exception message.
        self._error_status = Status(StatusCode.ERROR)

        attributes: dict[str, Any] = {"service.name": self._config.service_name}
        version = _package_version()
        if version:
            attributes["service.version"] = version
        if self._config.environment:
            attributes["deployment.environment"] = self._config.environment

        # `Resource(...)` rather than `Resource.create(...)`: `create` merges environment variables
        # and registered detectors, which would put host-derived data on every span. No environment
        # suppression is needed here, because this constructor never reads the environment.
        resource = Resource(attributes)
        provider = TracerProvider(
            resource=resource,
            # ParentBased keeps a sampled task span's attempt and phase children sampled, so a
            # ratio below 1.0 drops whole traces instead of shredding individual ones.
            sampler=ParentBased(root=TraceIdRatioBased(self._config.sample_ratio)),
            # The SDK's own atexit hook is unbounded: it would join the export worker at
            # interpreter exit with no timeout, so an unreachable collector could hold the process
            # open after the CLI has finished. Forge shuts telemetry down explicitly and with a
            # bound instead; see `shutdown`.
            shutdown_on_exit=False,
        )
        provider.add_span_processor(self._build_processor())

        self._provider = provider
        self._tracer = provider.get_tracer("codex_forge.telemetry")

    def _build_processor(self) -> Any:
        if self._processor_factory is not None:
            return self._processor_factory()
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Endpoint, insecure, and timeout are all passed explicitly, so the exporter consults the
        # environment for none of them. Headers are the sole exception; see
        # `_without_exporter_headers`.
        with _without_exporter_headers():
            exporter = OTLPSpanExporter(
                endpoint=self._config.endpoint,
                # The endpoint is validated as loopback http, so there is no TLS material and no
                # credential to carry.
                insecure=True,
                timeout=EXPORT_TIMEOUT_SECONDS,
            )
        return BatchSpanProcessor(
            exporter,
            max_queue_size=2048,
            schedule_delay_millis=5000,
            max_export_batch_size=512,
            export_timeout_millis=EXPORT_TIMEOUT_SECONDS * 1000,
        )

    def flush(self, timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Export whatever is queued, bounded by `timeout_seconds`."""
        provider = self._provider
        if provider is None:
            return
        try:
            provider.force_flush(timeout_millis=int(max(0.0, timeout_seconds) * 1000))
        except Exception as exc:
            self._warn("flush", exc)

    def shutdown(self, timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Flush and tear down, never blocking CLI or daemon exit for longer than the timeout.

        The provider shutdown runs on a daemon thread that is joined with a bound, because an
        unreachable collector can otherwise hold the export worker until its own timeouts elapse.
        A thread that overruns cannot keep the interpreter alive.

        `timeout_seconds` bounds the whole call, not each step: the flush and the join split one
        budget, so the caller's wait cannot reach twice the value it asked for. The environment is
        never touched here.
        """
        with self._lock:
            provider, self._provider, self._tracer = self._provider, None, None
            self._started = True
        if provider is None:
            return
        budget = max(0.0, timeout_seconds)
        deadline = time.monotonic() + budget
        self.flush(budget / 2)
        worker = threading.Thread(
            target=self._shutdown_provider, args=(provider,), name="forge-telemetry-shutdown", daemon=True
        )
        worker.start()
        worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def _shutdown_provider(self, provider: Any) -> None:
        try:
            provider.shutdown()
        except Exception as exc:
            self._warn("shutdown", exc)

    # ----------------------------------------------------------------------------------------
    # Span helpers
    # ----------------------------------------------------------------------------------------

    @contextmanager
    def _span(
        self,
        name: str,
        attributes: Mapping[str, Any],
        *,
        outcome_key: str,
        duration_key: str | None = None,
    ) -> Iterator[SpanHandle]:
        """Open one span, guaranteeing it is closed on every exit path including generator close.

        `record_exception` and `set_status_on_exception` are both disabled and the span is ended by
        this function rather than by `use_span`, so the exception message never reaches the span
        and attributes recorded during unwinding are set before the span closes.
        """
        if self._tracer is None:
            yield SpanHandle(None, self)
            return

        started = time.monotonic()
        scope: Any = None
        span: Any = None
        try:
            scope = self._tracer.start_as_current_span(
                name, record_exception=False, set_status_on_exception=False, end_on_exit=False
            )
            span = scope.__enter__()
        except Exception as exc:
            self._warn("span_start", exc)
            scope = None
            span = None

        handle = SpanHandle(span, self, outcome_key)
        handle.set_many(attributes)
        try:
            yield handle
        except BaseException as exc:
            try:
                handle.record_error(exc)
            except Exception:
                # Recording must never replace the exception the orchestrator is handling; the
                # original failure classification is what decides the task's state.
                pass
            raise
        finally:
            if duration_key is not None:
                handle.set(duration_key, int((time.monotonic() - started) * 1000))
            if scope is not None:
                try:
                    # Detach the context only. Exception state is deliberately not forwarded: this
                    # module records the class name and a bounded code, never the message.
                    scope.__exit__(None, None, None)
                except Exception as exc:
                    self._warn("span_scope", exc)
            if span is not None:
                try:
                    span.end()
                except Exception as exc:
                    self._warn("span_end", exc)

    @contextmanager
    def task_span(
        self, *, task_id: str, project_id: str, risk: str, max_attempts: int
    ) -> Iterator[SpanHandle]:
        """Span covering one orchestrated execution of a task in this process.

        Forge re-queues a retried task rather than looping in place, so a task that retries produces
        one `forge.task` span per scheduling, correlated by `forge.task.id`.
        """
        with self._span(
            SPAN_TASK,
            {
                ATTR_TASK_ID: safe_identifier(task_id),
                ATTR_PROJECT_ID: safe_identifier(project_id),
                ATTR_TASK_RISK: normalize_risk(risk),
                ATTR_TASK_MAX_ATTEMPTS: max_attempts,
            },
            outcome_key=ATTR_TASK_OUTCOME,
        ) as handle:
            yield handle

    @contextmanager
    def attempt_span(self, *, attempt_number: int) -> Iterator[SpanHandle]:
        """Span covering the single attempt that the enclosing task execution performs."""
        with self._span(
            SPAN_ATTEMPT,
            {
                ATTR_ATTEMPT_NUMBER: attempt_number,
                ATTR_ATTEMPT_KIND: ATTEMPT_KIND_FULL,
                ATTR_RETRY_BUCKET: retry_bucket(attempt_number),
            },
            outcome_key=ATTR_ATTEMPT_OUTCOME,
        ) as handle:
            yield handle

    @contextmanager
    def phase_span(
        self,
        *,
        phase_name: str,
        cycle: int = 0,
        role: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> Iterator[SpanHandle]:
        """Span covering one orchestration phase.

        For a Codex phase this covers a whole `codex exec` invocation, which may issue several
        model requests internally; see the module docstring.
        """
        with self._span(
            SPAN_PHASE,
            {
                ATTR_PHASE_NAME: phase_name,
                ATTR_PHASE_CYCLE: cycle,
                ATTR_AGENT_ROLE: role,
                ATTR_REQUEST_MODEL: model,
                ATTR_REQUEST_EFFORT: effort,
            },
            outcome_key=ATTR_PHASE_OUTCOME,
            duration_key=ATTR_PHASE_DURATION_MS,
        ) as handle:
            yield handle


def build_telemetry(
    config: TelemetryConfig, *, processor_factory: Callable[[], Any] | None = None
) -> Telemetry:
    """Build and initialize telemetry. Disabled configuration yields an inert instance."""
    telemetry = Telemetry(config, processor_factory=processor_factory)
    telemetry.start()
    return telemetry
