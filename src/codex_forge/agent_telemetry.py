"""Gate 4 primitives for the shared agent telemetry contract — implemented, tested, and unwired.

Gate 4 of [the shared agent telemetry contract](../../docs/AGENT-TELEMETRY-CONTRACT.md) asks for the
correlation identifier, the trace-context extraction, and the child-process environment that a later
slice would need in order to observe a Claude or Codex invocation. This module is that code.

**Nothing here is wired.** No other module in this package imports it. `CodexRunner.run()` builds the
same arguments and the same environment it built before this module existed, native Claude and Codex
telemetry stay disabled, no canary has run, and no Codex `-c` override is implemented. Enabling an
agent exporter is Gate 5 (Claude) and Gate 7 (Codex), and each needs its own human approval.

What the module provides:

* `new_invocation_id()` — 128 bits from `secrets`, rendered as 32 lowercase hexadecimal characters.
  The value is uniformly random and is never derived, hashed, or encoded from a task id, a project
  id, a path, a branch, a commit, a prompt, a response, a model, or an account. Contract section 4
  makes that absolute: a deterministic derivative of a sensitive value is a sensitive value.
* `current_traceparent()` — the W3C `traceparent` for the currently active Forge span, produced by
  the official `TraceContextTextMapPropagator` rather than composed by hand, and `None` on every
  degraded path.
* `build_claude_child_environment()` — a pure function returning a new dictionary: the supplied
  environment with inherited agent-telemetry configuration removed, then exactly the approved Claude
  settings applied. It never touches `os.environ`, the mapping it was handed, or any global agent
  configuration.

Fail-closed choices worth knowing before editing:

* The collector endpoint is validated as loopback http with an explicit port and nothing else — no
  userinfo, no path, no query, no fragment. A non-loopback endpoint raises rather than being
  accepted and then redacted downstream.
* `CLAUDE_CODE_PROPAGATE_TRACEPARENT` is never set. Inbound trace context is accepted by the child;
  outbound propagation would put Forge's trace and span identifiers on requests to Anthropic.
* `TRACESTATE` is never emitted, and the propagator's `tracestate` output is discarded.
* `CODEX_HOME` is never set, and `~/.codex/config.toml` is never read or written.
* The tracing SDK is never required. Every OpenTelemetry import is lazy and guarded, so Forge with
  telemetry disabled — or without the `telemetry` extra installed at all — behaves identically.
* Importing this module has no side effects: it reads no environment, opens no socket, touches no
  file, and imports no OpenTelemetry package.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

# --------------------------------------------------------------------------------------------
# Contract version
# --------------------------------------------------------------------------------------------

# The one place this literal exists. Contract section 10 requires an implementation to record the
# contract version it was built against, so a reader can tell from telemetry alone which rules a
# trace was produced under; a second copy elsewhere would let the two drift apart silently.
AGENT_TELEMETRY_CONTRACT_VERSION = "1.0"

# The two attribute names contract section 4 and section 10 reserve. They are *future* names: no
# Forge span carries either one today, and this module only ever writes them into the resource
# attributes of a child environment that nothing constructs yet.
ATTR_INVOCATION_ID = "forge.invocation.id"
ATTR_CONTRACT_VERSION = "forge.telemetry.contract.version"


# --------------------------------------------------------------------------------------------
# Invocation identifier
# --------------------------------------------------------------------------------------------

# 128 bits is the contract floor, not a target to tune. Hex keeps the value inside the bounded
# `[0-9a-f]` alphabet and well under the 120-character attribute cap the Forge span slice enforces.
INVOCATION_ID_BYTES = 16
INVOCATION_ID_CHARS = INVOCATION_ID_BYTES * 2

_INVOCATION_ID = re.compile(r"[0-9a-f]{%d}" % INVOCATION_ID_CHARS)


def new_invocation_id() -> str:
    """Mint one `forge.invocation.id`: 128 fresh CSPRNG bits as 32 lowercase hexadecimal characters.

    Every call returns a new value. Nothing is passed in, so nothing about the task, the project,
    the phase, the attempt, the branch, the commit, the path, the prompt, the response, the model,
    or the account can reach the identifier — not directly, not hashed, not encoded. That is the
    whole reason exporting it is acceptable under contract section 4: it is meaningless outside a
    Forge process's own memory.

    The value is not persisted, not cached, and not reused. A retry, a cancellation, a second phase,
    or a second provider each get their own.
    """
    return secrets.token_bytes(INVOCATION_ID_BYTES).hex()


def is_valid_invocation_id(value: Any) -> bool:
    """Return True only for the exact approved format, failing closed on everything else.

    Uppercase hexadecimal, a wrong length, surrounding whitespace, a dashed UUID, and any non-string
    are all rejected rather than normalized. A caller that accepted `TASK-1234` here would export the
    task identifier the contract exists to keep out of telemetry.
    """
    return isinstance(value, str) and _INVOCATION_ID.fullmatch(value) is not None


# --------------------------------------------------------------------------------------------
# Trace context
# --------------------------------------------------------------------------------------------

TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"

# W3C TraceContext level 1: `version-trace_id-parent_id-trace_flags`, all lowercase hex.
_TRACEPARENT = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<parent_id>[0-9a-f]{16})-"
    r"(?P<flags>[0-9a-f]{2})"
)


def is_valid_traceparent(value: Any) -> bool:
    """Return True for a well-formed W3C `traceparent` that names a real span.

    Version `ff` is forbidden by the specification, and an all-zero trace id or parent id is the
    invalid-context encoding — none of the three identifies anything, so all three are refused
    rather than handed to a child process.
    """
    if not isinstance(value, str):
        return False
    match = _TRACEPARENT.fullmatch(value)
    if match is None:
        return False
    if match.group("version") == "ff":
        return False
    return int(match.group("trace_id"), 16) != 0 and int(match.group("parent_id"), 16) != 0


def _active_span() -> Any:
    """Return the currently active span. Imported lazily so the API package stays optional."""
    from opentelemetry import trace

    return trace.get_current_span()


def _traceparent_propagator() -> Any:
    """Return the official W3C TraceContext propagator.

    Deliberately *not* `opentelemetry.propagate.inject`, which uses the globally configured
    propagator list. That list is read from `OTEL_PROPAGATORS` and includes baggage by default, so
    it would both honor an operator's environment and risk writing baggage entries — arbitrary
    operator-supplied key/value pairs — into a child process's trace context.
    """
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    return TraceContextTextMapPropagator()


def current_traceparent() -> str | None:
    """Return the W3C `traceparent` for the active Forge span, or None.

    The header is produced by the official propagator, never composed from formatted trace and span
    identifiers by hand: hand composition is how a wrong flag byte, a truncated id, or a stale
    version prefix reaches a child process that will happily believe it.

    None is returned — never an exception, and never a partial value — when telemetry is disabled,
    when the SDK is not installed, when no recording span is active, when the current context is
    invalid, and when propagation fails for any other reason. This function is safe to call from
    inside task execution because it cannot raise into it.

    `tracestate` is discarded even when the active context carries one. Contract section 4 admits
    inbound trace context only; `tracestate` is an unbounded vendor-supplied string, and emitting it
    is a reviewed decision for a later slice rather than a default.
    """
    try:
        span = _active_span()
        # A non-recording span still has a context — a sampled-out trace, or a context restored from
        # a remote header. Correlating a child against a span Forge will never export produces a
        # dangling parent, so a traceparent is offered only while a real span is recording.
        if span is None or not span.is_recording():
            return None
        carrier: dict[str, str] = {}
        _traceparent_propagator().inject(carrier)
        candidate = carrier.get(TRACEPARENT_HEADER)
    except Exception:
        return None
    return candidate if is_valid_traceparent(candidate) else None


# --------------------------------------------------------------------------------------------
# Provider-neutral invocation context
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvocationContext:
    """The complete set of values a provider-specific child configuration may draw on.

    Three fields, and there will not be a fourth without a contract change. A Codex implementation
    written later reuses this record as-is; anything it additionally needs — a task id, a phase, a
    worktree path — is exactly what contract section 6 forbids exporting, so the absence of those
    fields is the point of the type rather than an omission.

    This is a plain record: it validates nothing. `build_claude_child_environment` is the single
    enforcement point, so there is one place to read when auditing what may reach a child.
    """

    invocation_id: str
    contract_version: str = AGENT_TELEMETRY_CONTRACT_VERSION
    traceparent: str | None = None


def new_invocation_context() -> InvocationContext:
    """Mint a fresh context: a new identifier, the pinned contract version, the active traceparent.

    The traceparent is None whenever `current_traceparent()` declines to produce one, which is the
    normal case with telemetry disabled.
    """
    return InvocationContext(
        invocation_id=new_invocation_id(),
        contract_version=AGENT_TELEMETRY_CONTRACT_VERSION,
        traceparent=current_traceparent(),
    )


# --------------------------------------------------------------------------------------------
# Loopback endpoint validation
# --------------------------------------------------------------------------------------------

DEFAULT_COLLECTOR_ENDPOINT = "http://127.0.0.1:4317"
MAX_ENDPOINT_CHARS = 200

# Narrower than `config.TELEMETRY_LOOPBACK_HOSTS`, which also admits `::1`. The Collector this slice
# targets binds `127.0.0.1` for both OTLP receivers, and an IPv6 literal in an OTLP endpoint needs
# bracket quoting that the agents' own parsers have not been exercised against. A host that has not
# been tested end to end is refused rather than assumed equivalent.
CLAUDE_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def validate_loopback_endpoint(value: Any) -> str:
    """Return the endpoint unchanged, or raise `ValueError`.

    An agent child is configured by environment variable, so the endpoint is the one setting that
    decides whether telemetry stays on this machine. Everything except a bare loopback http address
    with an explicit port is refused: a remote host is egress, `https` implies TLS material and
    therefore a credential, userinfo *is* a credential, and a path, query, or fragment is a signal
    that something other than an OTLP collector address was pasted in.

    Refusal is the point. There is no normalization step here, because an endpoint that has to be
    repaired before it is safe is an endpoint a reviewer cannot check by reading `forge.toml`.
    """
    if not isinstance(value, str):
        raise ValueError("collector endpoint must be a string.")
    if not value:
        raise ValueError("collector endpoint must not be empty.")
    if len(value) > MAX_ENDPOINT_CHARS:
        raise ValueError(f"collector endpoint must be at most {MAX_ENDPOINT_CHARS} characters.")
    if any(character.isspace() for character in value):
        raise ValueError("collector endpoint must not contain whitespace.")
    parsed = urlsplit(value)
    if parsed.scheme != "http":
        raise ValueError(
            f"collector endpoint must use http:// for a local collector; got scheme {parsed.scheme!r}."
        )
    try:
        username, password = parsed.username, parsed.password
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValueError("collector endpoint is not a valid host and port.") from exc
    if username or password or "@" in parsed.netloc:
        raise ValueError("collector endpoint must not embed credentials.")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("collector endpoint must be a bare host and port, with no path or query.")
    if (host or "").lower() not in CLAUDE_LOOPBACK_HOSTS:
        raise ValueError(
            "collector endpoint must address the loopback interface "
            f"({', '.join(sorted(CLAUDE_LOOPBACK_HOSTS))}); agent telemetry never leaves the host."
        )
    if port is None:
        raise ValueError("collector endpoint must include an explicit port.")
    return value


# --------------------------------------------------------------------------------------------
# Inherited-configuration neutralization
# --------------------------------------------------------------------------------------------
#
# The exact policy, which the tests assert against directly:
#
# 1. A key is neutralized when its name — compared case-insensitively — either appears in
#    `NEUTRALIZED_KEYS` or begins with one of `NEUTRALIZED_PREFIXES`.
# 2. Neutralization means the key is *absent* from the returned dictionary. It is not blanked, not
#    defaulted, and not passed through: an agent that reads a variable Forge did not anticipate
#    finds nothing there rather than something Forge guessed at.
# 3. The approved block is applied afterwards, so an approved key always ends up with its approved
#    value regardless of what the parent environment held.
# 4. Nothing else is removed. `PATH`, `CODEX_HOME`, credentials the agent needs to run, and an
#    operator's unrelated variables all survive byte-for-byte — the child must still be able to
#    work, and contract section 7 requires that unrelated processes are unaffected, not that the
#    child is stripped bare.
#
# The comparison is case-insensitive because Windows environment variable names are, so a hostile
# `otel_exporter_otlp_headers` in the parent would otherwise survive and be read by the child under
# the name the approved block thought it had already claimed.
#
# The `OTEL_*` prefix is swept wholesale rather than enumerated. The list of variables the SDKs read
# grows with every release, and contract section 10 makes an unrecognized setting deny-by-default;
# enumerating them would mean a new exporter, endpoint, header, resource-attribute, or content-logging
# variable arrives inherited and enabled.

NEUTRALIZED_PREFIXES = ("OTEL_", "CLAUDE_CODE_OTEL_")

NEUTRALIZED_KEYS = frozenset(
    {
        # Inbound and outbound W3C trace context.
        "TRACEPARENT",
        "TRACESTATE",
        # Claude's own telemetry switches.
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
        "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
        "CLAUDE_CODE_WORKSPACE_HOST_PATHS",
        # Claude's detailed beta tracing, which carries content and has its own endpoint.
        "ENABLE_BETA_TRACING_DETAILED",
        "BETA_TRACING_ENDPOINT",
    }
)

# Keys this module must never set. `CLAUDE_CODE_PROPAGATE_TRACEPARENT` would forward Forge's trace
# and span identifiers to Anthropic; `CLAUDE_CODE_WORKSPACE_HOST_PATHS` publishes absolute paths;
# the two beta-tracing variables enable a content-carrying signal with a separate destination;
# `CODEX_HOME` relocates credential, history, and session storage and is not a telemetry mechanism;
# `TRACESTATE` is unbounded vendor text.
#
# All but `CODEX_HOME` are also neutralized above, so an inherited copy cannot reach the child
# either. `CODEX_HOME` is deliberately *not* neutralized: an operator's own value is unrelated
# configuration that contract section 7 says must survive. Forge neither sets it nor removes it.
FORBIDDEN_CHILD_KEYS = frozenset(
    {
        "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
        "CLAUDE_CODE_WORKSPACE_HOST_PATHS",
        "ENABLE_BETA_TRACING_DETAILED",
        "BETA_TRACING_ENDPOINT",
        "CODEX_HOME",
        "TRACESTATE",
    }
)


def is_neutralized_key(key: Any) -> bool:
    """Return True when an inherited environment variable must not reach an agent child."""
    if not isinstance(key, str):
        return False
    name = key.upper()
    return name in NEUTRALIZED_KEYS or name.startswith(NEUTRALIZED_PREFIXES)


# --------------------------------------------------------------------------------------------
# Claude child environment
# --------------------------------------------------------------------------------------------

# Every key the builder may write, and nothing else. A reviewer can diff this set against the
# approved block in the contract without reading the function.
CLAUDE_CHILD_ENVIRONMENT_KEYS = frozenset(
    {
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_LOGS_EXPORTER",
        "OTEL_LOG_USER_PROMPTS",
        "OTEL_LOG_ASSISTANT_RESPONSES",
        "OTEL_LOG_RAW_API_BODIES",
        "OTEL_LOG_TOOL_DETAILS",
        "OTEL_LOG_TOOL_CONTENT",
        "OTEL_METRICS_INCLUDE_SESSION_ID",
        "OTEL_METRICS_INCLUDE_ACCOUNT_UUID",
        "OTEL_METRICS_INCLUDE_VERSION",
        "OTEL_METRICS_INCLUDE_ENTRYPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_INSECURE",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
        "OTEL_METRIC_EXPORT_INTERVAL",
        "OTEL_TRACES_EXPORT_INTERVAL",
        "CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS",
        "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS",
        "OTEL_RESOURCE_ATTRIBUTES",
        "TRACEPARENT",
    }
)


def encode_resource_attributes(context: InvocationContext) -> str:
    """Encode exactly the two approved resource attributes as an `OTEL_RESOURCE_ATTRIBUTES` value.

    The format is the W3C Baggage-style `key=value` list the OpenTelemetry specification defines for
    this variable, with values percent-encoded. Both values are already inside the unreserved set,
    so the encoding is a no-op today and exists so a future value cannot smuggle a `,` or an `=` and
    forge a third attribute.

    Two attributes, no more. `service.name`, a host name, a task id, or a phase would each either
    duplicate what the Collector already knows or export something contract section 6 forbids.
    """
    pairs = (
        (ATTR_INVOCATION_ID, context.invocation_id),
        (ATTR_CONTRACT_VERSION, context.contract_version),
    )
    return ",".join(f"{key}={quote(value, safe='')}" for key, value in pairs)


def _approved_claude_settings(
    *, endpoint: str, resource_attributes: str, traceparent: str | None
) -> dict[str, str]:
    """The exact approved Claude telemetry block. Every value is a constant or a validated input."""
    settings = {
        # Claude's telemetry is opt-in per process; these two switches are what turn it on for the
        # child alone, and they are neutralized out of the inherited environment first so an
        # operator cannot have turned them on for a different destination.
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        # Contract section 5: traces and metrics may be received; logs are forbidden agent-side,
        # and forbidden again by the Collector having no logs pipeline. Two independent barriers.
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "none",
        # Contract section 6 and section 14: no prompt, response, body, or tool content, in any
        # form, truncated or otherwise.
        "OTEL_LOG_USER_PROMPTS": "false",
        "OTEL_LOG_ASSISTANT_RESPONSES": "false",
        "OTEL_LOG_RAW_API_BODIES": "false",
        "OTEL_LOG_TOOL_DETAILS": "false",
        "OTEL_LOG_TOOL_CONTENT": "false",
        # The gated identity dimensions. The ungated ones are the Collector's problem, which is why
        # Gate 3 had to pass before this block could be written down.
        "OTEL_METRICS_INCLUDE_SESSION_ID": "false",
        "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
        "OTEL_METRICS_INCLUDE_VERSION": "false",
        "OTEL_METRICS_INCLUDE_ENTRYPOINT": "false",
        # Transport. `insecure` is honest rather than lax: the endpoint is validated loopback http,
        # so there is no TLS material and nothing for a credential to authenticate to.
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_INSECURE": "true",
        # Explicitly empty, not merely absent. Contract section 7: a header setting reaching an agent
        # is evidence of an unapproved destination, so each of the three the SDK consults is claimed
        # and emptied rather than left for a later inherited value to fill.
        "OTEL_EXPORTER_OTLP_HEADERS": "",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS": "",
        # Delta temporality keeps a metric point meaningful on its own, which matters because an
        # agent process exits long before its telemetry is consumed.
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
        "OTEL_TRACES_EXPORT_INTERVAL": "1000",
        # Bounded shutdown, for the same reason Forge bounds its own: an unreachable collector must
        # not hold a child process open after its work is done.
        "CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS": "2000",
        "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS": "2000",
        # The join. Metric points carry no trace context, so this is the only thing that connects a
        # child's metrics back to the phase that produced them.
        "OTEL_RESOURCE_ATTRIBUTES": resource_attributes,
    }
    # Omitted rather than blanked when there is no active span. An empty `TRACEPARENT` is not a
    # valid header, and a child that parses one strictly would be entitled to fail on it.
    if traceparent is not None:
        settings["TRACEPARENT"] = traceparent
    return settings


def build_claude_child_environment(
    base_environment: Mapping[str, str],
    context: InvocationContext,
    *,
    endpoint: str = DEFAULT_COLLECTOR_ENDPOINT,
) -> dict[str, str]:
    """Return a new environment dictionary for a future Claude child process.

    Pure. `os.environ` is not read and not written, `base_environment` is not mutated, no global
    Claude or Codex configuration is touched, and no parent process state changes. Contract section
    7 requires exactly this: the controller is concurrent, so even a temporary mutation of the
    parent environment would be visible to every other thread and to every unrelated child started
    during the window.

    **This function launches nothing.** It returns a dictionary. No production path calls it.

    The result is the supplied environment with inherited agent-telemetry configuration removed
    (see the neutralization policy above), plus exactly the approved Claude block. Invalid input
    fails closed with `ValueError` — a malformed invocation id, a contract version that is not the
    one this module implements, a malformed traceparent, or a non-loopback endpoint each raise
    rather than producing a child environment a reviewer would have to inspect to trust.
    """
    if not isinstance(context, InvocationContext):
        raise TypeError("context must be an InvocationContext.")
    if not is_valid_invocation_id(context.invocation_id):
        raise ValueError(
            f"invocation id must be {INVOCATION_ID_CHARS} lowercase hexadecimal characters."
        )
    if context.contract_version != AGENT_TELEMETRY_CONTRACT_VERSION:
        raise ValueError(
            f"contract version must be {AGENT_TELEMETRY_CONTRACT_VERSION!r}; "
            f"got {context.contract_version!r}."
        )
    if context.traceparent is not None and not is_valid_traceparent(context.traceparent):
        raise ValueError("traceparent must be a valid W3C traceparent header, or None.")
    validated_endpoint = validate_loopback_endpoint(endpoint)

    child: dict[str, str] = {}
    for key, value in base_environment.items():
        # A non-string key or value cannot be passed to a subprocess at all, so it is refused here
        # rather than silently dropped where the loss would be invisible.
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("environment keys and values must be strings.")
        if is_neutralized_key(key):
            continue
        child[key] = value

    approved = _approved_claude_settings(
        endpoint=validated_endpoint,
        resource_attributes=encode_resource_attributes(context),
        traceparent=context.traceparent,
    )
    # Guards an editing mistake in the block above rather than any input. A forbidden key added
    # there would be the one class of change no test of the caller's data would catch.
    forbidden = FORBIDDEN_CHILD_KEYS & set(approved)
    if forbidden:
        raise RuntimeError(f"approved block must never set: {', '.join(sorted(forbidden))}.")
    child.update(approved)
    return child
