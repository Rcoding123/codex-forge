"""Gate 4 primitives for the shared agent telemetry contract — implemented, tested, and unwired.

Gate 4 of [the shared agent telemetry contract](../../docs/AGENT-TELEMETRY-CONTRACT.md) asks for the
correlation identifier, the trace-context extraction, and the child-process environment that a later
slice would need in order to observe a Claude or Codex invocation. This module is that code.

**Nothing here is wired into normal Forge execution.** No production module imports it, and
`CodexRunner.run()` still builds the same arguments and environment as before. The supervised
Claude canary exercised these helpers only through the approval-gated canary harness. Native
Claude and Codex telemetry remain disabled in normal execution. Gate 5 has passed; Gate 6 field
inspection remains incomplete. The Codex helpers below are the Gate 7 *foundation*: **Gate 7 has
not passed**, no Codex canary has run, and nothing here has been accepted by a real
`codex --strict-config` invocation.

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
* `build_codex_child_environment()` — the same neutralization with **no approved block at all**.
  Codex is configured per invocation through `-c`, so the child gets no `OTEL_*` variable to read;
  the only job the environment builder has is taking inherited ones away. `CODEX_HOME` is preserved
  exactly as the caller held it and is never created, replaced, redirected, or deleted.
* `build_codex_otel_overrides()` — the exact immutable `-c` argv fragment for one Codex invocation:
  logs off, prompt logging off, traces and metrics to a loopback Collector over OTLP/HTTP, and the
  two correlation attributes. **Proposed, not active** — see the caveats on that function.

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


# --------------------------------------------------------------------------------------------
# Codex child environment
# --------------------------------------------------------------------------------------------

# Empty, and that is the design. Contract section 4 requires Codex to be configured through a
# per-invocation `-c` override, so there is no approved environment block to apply and no variable
# for the child to read. The builder only *removes*.
#
# Removing is not optional. Codex 0.146.0 embeds an OTLP exporter that reads the standard
# `OTEL_EXPORTER_OTLP_*` variables and falls back to `http://localhost:4318`; an inherited endpoint,
# header, or protocol variable in the parent would therefore reach the exporter underneath the `-c`
# configuration, which is exactly the destination substitution contract section 7 exists to prevent.
CODEX_CHILD_ENVIRONMENT_KEYS: frozenset[str] = frozenset()


def build_codex_child_environment(
    base_environment: Mapping[str, str], *, endpoint: str
) -> dict[str, str]:
    """Return a new environment dictionary for a Codex child process.

    Pure. `os.environ` is not read and not written, `base_environment` is not mutated, no global
    Codex configuration is touched, `~/.codex/config.toml` is neither read nor written, and no
    authentication file is opened, moved, or inspected.

    **This function launches nothing, and no production path calls it.** It returns a dictionary.

    The result is the supplied environment with inherited agent-telemetry configuration removed —
    every `OTEL_*` and `CLAUDE_CODE_OTEL_*` name compared case-insensitively, plus `TRACEPARENT`,
    `TRACESTATE`, and the Claude switches — and **nothing added**. Everything else survives
    byte-for-byte, including `PATH` and including `CODEX_HOME`:

    * A caller that had `CODEX_HOME` set gets exactly that value back, unchanged.
    * A caller that had no `CODEX_HOME` gets no `CODEX_HOME`.

    Contract section 4 forbids setting it either way. `CODEX_HOME` selects credential, history, and
    session storage; relocating it is not a telemetry mechanism, and Codex's own
    `--ignore-user-config` still authenticates out of `CODEX_HOME`, so a redirect would break login
    rather than isolate telemetry.

    `endpoint` is the loopback Collector the caller intends to point the child at. It is validated
    and then deliberately **not written anywhere**: the endpoint travels in the `-c` override built
    by :func:`build_codex_otel_overrides`, and validating it here means a caller cannot assemble a
    child environment for a Collector that is not on this machine and discover the problem later.
    """
    validate_loopback_endpoint(endpoint)

    child: dict[str, str] = {}
    for key, value in base_environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("environment keys and values must be strings.")
        if is_neutralized_key(key):
            continue
        child[key] = value
    return child


# --------------------------------------------------------------------------------------------
# Codex per-invocation `-c` overrides
# --------------------------------------------------------------------------------------------
#
# Codex 0.146.0 carries its OpenTelemetry configuration in a `[otel]` table with these keys, read
# from the binary's own configuration surface: `exporter` (logs), `trace_exporter`,
# `metrics_exporter`, `log_user_prompt`, `environment`, `span_attributes`, and `tracestate`. An
# exporter is one of the unit variant `none` or a single-key table naming `otlp-http` or `otlp-grpc`.
#
# What this builder writes, and why each line is the value it is:
#
# * `exporter = "none"` — the **logs** pipeline, and the one that must never be enabled. Codex's
#   content-bearing events are logs: the binary's own event definitions put `prompt`,
#   `user.email`, and `user.account_id` on that signal. Contract section 5 forbids it outright, and
#   the Collector has no logs pipeline to receive it even if this line were wrong.
# * `log_user_prompt = false` — belt to that brace. Even a logs exporter turned on by mistake would
#   not be handed the prompt text.
# * `trace_exporter` / `metrics_exporter` — OTLP/HTTP with the binary protobuf encoding, to the
#   validated loopback Collector, with an explicitly empty header table. Empty rather than absent
#   for the reason contract section 7 gives: a header is how telemetry acquires a second
#   destination, so the setting is claimed and emptied rather than left for a default to fill.
# * `environment = "forge-canary"` — a constant. It is not derived from a project, a task, a branch,
#   a path, or a host name, so it can carry nothing.
# * `span_attributes` — exactly the two attributes contract sections 4 and 10 reserve, and no third.
#
# `tracestate` is never written. It is unbounded vendor text, and contract section 4 admits inbound
# trace context only.
#
# **Unproven until the supervised run.** Three things here are read off the 0.146.0 binary's
# configuration surface rather than off a successful invocation, and `--strict-config` rejects any
# key this version does not recognize:
#
#   1. the exact nesting of an exporter variant — `{otlp-http={...}}` as an externally tagged table;
#   2. whether `headers` is accepted on that variant, and whether an empty table is accepted;
#   3. whether an exporter table's `endpoint` is posted to verbatim. It is treated here as a full
#      signal URL, so this builder appends `/v1/traces` and `/v1/metrics` itself rather than handing
#      over a base URL and relying on the exporter to append them. The path-appending behaviour
#      belongs to the *environment* form of the setting; an explicitly configured endpoint is the
#      form OTLP/HTTP posts to as written.
#
# None of the three may be described as accepted until one supervised `codex` run has accepted it.

CODEX_OTEL_ENVIRONMENT = "forge-canary"

# OTLP/HTTP rather than gRPC: the Collector answers both on loopback, and HTTP is the transport whose
# wire format is inspectable from the Collector side without a second client library.
CODEX_OTEL_HTTP_PROTOCOL = "binary"
CODEX_LOGS_EXPORTER = "none"

# The OTLP/HTTP signal paths. The builder is given a bare Collector base URL and constructs these two
# URLs itself, so neither the caller nor the exporter's own defaulting decides where a signal lands.
CODEX_TRACES_PATH = "/v1/traces"
CODEX_METRICS_PATH = "/v1/metrics"
CODEX_SIGNAL_PATHS = (CODEX_TRACES_PATH, CODEX_METRICS_PATH)

# Every `[otel]` key this builder writes, in the order it writes them. A reviewer can diff this
# tuple against the emitted vector without reading the function.
CODEX_OTEL_OVERRIDE_KEYS = (
    "otel.exporter",
    "otel.log_user_prompt",
    "otel.trace_exporter",
    "otel.metrics_exporter",
    "otel.environment",
    "otel.span_attributes",
)

# Keys this builder must never write. `otel.tracestate` is unbounded vendor text; the other two are
# the content switches whose defaults must not be relaxed by a Forge-supplied override.
CODEX_FORBIDDEN_OVERRIDE_KEYS = frozenset({"otel.tracestate"})

# The two correlation attributes, and there is no third. `span_attributes` is a free-form map, which
# is precisely why the set that may go into it is pinned here.
CODEX_SPAN_ATTRIBUTE_KEYS = (ATTR_INVOCATION_ID, ATTR_CONTRACT_VERSION)

# What a TOML basic string may contain here. Not an escaping routine: every value this builder emits
# comes from a validated, bounded source, so a value that would *need* escaping is a value that came
# from somewhere this builder does not accept input from. Refusing it is the fail-closed answer, and
# it is also what keeps the emitted argument safe through Windows shim quoting — there is no
# backslash, no embedded quote, and no whitespace for a shim to re-split on.
_TOML_SAFE = re.compile(r"[0-9A-Za-z._:/=~+-]{1,200}")


def _toml_string(value: str) -> str:
    """Render one TOML basic string, refusing anything that would need escaping."""
    if not isinstance(value, str) or _TOML_SAFE.fullmatch(value) is None:
        raise ValueError(
            "a Codex configuration override value must be 1-200 characters drawn from "
            "[0-9A-Za-z._:/=~+-]; refusing to escape anything else."
        )
    return f'"{value}"'


def _toml_inline_table(pairs: tuple[tuple[str, str], ...]) -> str:
    """Render `{key="value",...}` with every key quoted, so a dotted key stays one key."""
    return "{" + ",".join(f"{_toml_string(key)}={_toml_string(value)}" for key, value in pairs) + "}"


def codex_signal_endpoint(endpoint: str, path: str) -> str:
    """Return the OTLP/HTTP URL for one signal, built from a validated loopback base endpoint.

    The base is validated first, so a caller cannot smuggle a path in and have a signal path appended
    behind it; the path itself is one of the two this module owns and is not caller-supplied text.
    """
    base = validate_loopback_endpoint(endpoint)
    if path not in CODEX_SIGNAL_PATHS:
        raise ValueError(f"signal path must be one of {', '.join(CODEX_SIGNAL_PATHS)}; got {path!r}.")
    return f"{base}{path}"


def _codex_otlp_http_exporter(endpoint: str) -> str:
    """Render one externally tagged `otlp-http` exporter table for one signal URL."""
    return (
        "{otlp-http={"
        f"endpoint={_toml_string(endpoint)},"
        f"protocol={_toml_string(CODEX_OTEL_HTTP_PROTOCOL)},"
        "headers={}"
        "}}"
    )


def build_codex_otel_overrides(
    context: InvocationContext, *, endpoint: str = DEFAULT_COLLECTOR_ENDPOINT
) -> tuple[str, ...]:
    """Return the exact `-c` argv fragment for one Codex invocation. Pure; launches nothing.

    The result is a flat tuple of **separate argv elements** — `("-c", "otel.exporter=\\"none\\"",
    ...)` — never a command string. Codex splits an override at its first `=`, so a value may contain
    as many more as it likes; what it may not survive is a shell joining or re-splitting the pair,
    which is why nothing here is ever concatenated with a space.

    Invalid input fails closed with `ValueError`: a malformed invocation id, a contract version other
    than the one this module implements, a non-loopback endpoint, or any value outside the bounded
    alphabet raises rather than producing an override a reviewer would have to inspect to trust.

    This is **proposed, not active** (contract section 4). No production path calls it, and no Codex
    process has accepted its output.
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
    # One base endpoint in, two signal URLs out. The caller passes the Collector's bare loopback
    # address; which path each signal is posted to is this builder's decision, not the caller's.
    traces = codex_signal_endpoint(endpoint, CODEX_TRACES_PATH)
    metrics = codex_signal_endpoint(endpoint, CODEX_METRICS_PATH)

    values = {
        "otel.exporter": _toml_string(CODEX_LOGS_EXPORTER),
        "otel.log_user_prompt": "false",
        "otel.trace_exporter": _codex_otlp_http_exporter(traces),
        "otel.metrics_exporter": _codex_otlp_http_exporter(metrics),
        "otel.environment": _toml_string(CODEX_OTEL_ENVIRONMENT),
        "otel.span_attributes": _toml_inline_table(
            (
                (ATTR_INVOCATION_ID, context.invocation_id),
                (ATTR_CONTRACT_VERSION, context.contract_version),
            )
        ),
    }
    # Guards an editing mistake in the block above rather than any input, exactly as the Claude
    # builder does: a forbidden key added there is the one class of change no input-driven test sees.
    forbidden = CODEX_FORBIDDEN_OVERRIDE_KEYS & set(values)
    if forbidden:
        raise RuntimeError(f"Codex overrides must never set: {', '.join(sorted(forbidden))}.")
    if tuple(values) != CODEX_OTEL_OVERRIDE_KEYS:
        raise RuntimeError("the emitted override keys drifted from CODEX_OTEL_OVERRIDE_KEYS.")

    fragment: list[str] = []
    for key in CODEX_OTEL_OVERRIDE_KEYS:
        fragment += ["-c", f"{key}={values[key]}"]
    return tuple(fragment)
