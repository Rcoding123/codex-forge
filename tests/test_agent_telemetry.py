"""Gate 4 tests: the agent correlation and child-environment primitives, and their non-wiring.

Two properties this file exists to hold, beyond the behavior of each helper:

* **Nothing is wired.** `test_no_production_module_imports_agent_telemetry` and the runner tests at
  the bottom prove that no production path calls any of it, that `CodexRunner` builds the same
  arguments and the same environment it built before, and that native agent telemetry stays off.
* **Nothing here needs the tracing SDK.** The module scope imports no OpenTelemetry package, and the
  two tests that genuinely need the SDK skip without it. `codex_forge.agent_telemetry` must be
  usable on an installation that never installed the `telemetry` extra.

No test starts a Collector, launches Claude or Codex, runs a Forge task, opens `forge.db`, or
touches anything off the loopback interface. Every value below is synthetic.
"""

from __future__ import annotations

import binascii
import hashlib
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path
from urllib.parse import unquote

import pytest

from codex_forge import agent_telemetry
from codex_forge.agent_telemetry import (
    AGENT_TELEMETRY_CONTRACT_VERSION,
    ATTR_CONTRACT_VERSION,
    ATTR_INVOCATION_ID,
    CLAUDE_CHILD_ENVIRONMENT_KEYS,
    FORBIDDEN_CHILD_KEYS,
    INVOCATION_ID_BYTES,
    INVOCATION_ID_CHARS,
    InvocationContext,
    build_claude_child_environment,
    current_traceparent,
    encode_resource_attributes,
    is_neutralized_key,
    is_valid_invocation_id,
    is_valid_traceparent,
    new_invocation_context,
    new_invocation_id,
    validate_loopback_endpoint,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "codex_forge"

# A valid W3C traceparent that names a real span, used wherever a test needs one without an SDK.
SAMPLE_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

# Material the invocation identifier must never encode, hash, or otherwise derive from. All of it is
# fabricated for this test file.
HOSTILE_MATERIAL = {
    "task_id": "TASK-7f3ab21c9d",
    "project_id": "project-4c8e12ab",
    "phase": "validation_repair",
    "attempt": "attempt-3-of-3",
    "branch": "feat/agent-correlation-foundation",
    "commit": "6aaaab0f1e2d3c4b5a69788796a5b4c3d2e1f0091",
    "path": r"C:\CodexData\worktrees\TASK-7f3ab21c9d",
    "prompt": "Implement Gate 4 of the shared agent telemetry contract",
    "response": "The correlation foundation is implemented and unwired.",
    "model": "gpt-5.6-terra",
    "email": "operator@example.invalid",
    "account": "acct_9f8e7d6c5b4a3210",
    "repository": "https://github.example.invalid/acme/private-repo.git",
}

# Blocks every `opentelemetry` import for a child interpreter without uninstalling anything.
BLOCK_OTEL = textwrap.dedent(
    """
    import sys
    class _Block:
        def find_module(self, name, path=None):
            return self.find_spec(name, path)
        def find_spec(self, name, path=None, target=None):
            if name == "opentelemetry" or name.startswith("opentelemetry."):
                raise ModuleNotFoundError(f"No module named {name!r}")
            return None
    sys.meta_path.insert(0, _Block())
    """
)


def run_child(body: str, *, block_opentelemetry: bool = False) -> subprocess.CompletedProcess:
    """Run a snippet in a fresh interpreter, optionally with the tracing packages hidden."""
    prologue = BLOCK_OTEL if block_opentelemetry else ""
    return subprocess.run(
        [sys.executable, "-c", prologue + textwrap.dedent(body)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def context(traceparent: str | None = None) -> InvocationContext:
    return InvocationContext(invocation_id=new_invocation_id(), traceparent=traceparent)


class FakeSpan:
    def __init__(self, recording: bool) -> None:
        self._recording = recording

    def is_recording(self) -> bool:
        return self._recording


# ------------------------------------------------------------------------------------------------
# 1-4. The invocation identifier
# ------------------------------------------------------------------------------------------------


def test_invocation_id_draws_at_least_128_bits_from_the_csprng(monkeypatch):
    """The bits come from `secrets`, and there are at least 128 of them."""
    requested: list[int] = []
    real = agent_telemetry.secrets.token_bytes

    def recording_token_bytes(count: int) -> bytes:
        requested.append(count)
        return real(count)

    monkeypatch.setattr(agent_telemetry.secrets, "token_bytes", recording_token_bytes)
    value = new_invocation_id()

    assert requested == [INVOCATION_ID_BYTES]
    assert INVOCATION_ID_BYTES * 8 >= 128
    assert len(value) == INVOCATION_ID_CHARS


def test_every_invocation_id_bit_position_varies():
    """A statistical floor under the entropy claim: all 128 bits move across a large sample."""
    samples = [int(new_invocation_id(), 16) for _ in range(512)]
    for bit in range(INVOCATION_ID_BYTES * 8):
        seen = {(sample >> bit) & 1 for sample in samples}
        assert seen == {0, 1}, f"bit {bit} never varied"


def test_invocation_id_is_not_seeded_by_the_random_module():
    """`random` is seedable and therefore reproducible; `secrets` is not. Prove it is not `random`."""
    random.seed(20260806)
    first = new_invocation_id()
    random.seed(20260806)
    second = new_invocation_id()
    assert first != second


def test_invocation_id_matches_the_approved_format_exactly():
    for _ in range(256):
        value = new_invocation_id()
        assert len(value) == 32
        assert set(value) <= set("0123456789abcdef")
        assert value == value.lower()
        assert value.isascii()
        assert is_valid_invocation_id(value)


def test_invocation_ids_are_unique_across_many_calls():
    values = [new_invocation_id() for _ in range(20_000)]
    assert len(set(values)) == len(values)


def test_invocation_ids_encode_no_hostile_material():
    """No identifier is a hash, an encoding, or a fragment of anything sensitive."""
    values = [new_invocation_id() for _ in range(256)]
    joined = "".join(values)

    for label, material in HOSTILE_MATERIAL.items():
        encoded = binascii.hexlify(material.encode("utf-8")).decode("ascii")
        assert encoded not in joined, label
        # Every 8-hex-character window of the encoded value. A coincidence has probability 2**-32
        # per position, so a real derivation lights this up and a random collision does not.
        for start in range(0, len(encoded) - 8):
            assert encoded[start : start + 8] not in joined, f"{label} fragment"

        for algorithm in ("md5", "sha1", "sha256", "sha512", "blake2b"):
            for candidate in (material, material.lower(), material.strip()):
                digest = hashlib.new(algorithm, candidate.encode("utf-8")).hexdigest()
                assert digest[:INVOCATION_ID_CHARS] not in values, f"{label}/{algorithm}"
                assert digest[-INVOCATION_ID_CHARS:] not in values, f"{label}/{algorithm}"


# ------------------------------------------------------------------------------------------------
# 5. The validator fails closed
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        True,
        b"0123456789abcdef0123456789abcdef",
        bytearray(b"0123456789abcdef0123456789abcdef"),
        ["0123456789abcdef0123456789abcdef"],
        {"id": "0123456789abcdef0123456789abcdef"},
        "",
        "0123456789ABCDEF0123456789ABCDEF",  # uppercase
        "0123456789AbCdEf0123456789abcdef",  # mixed case
        "0123456789abcdef0123456789abcde",  # one short
        "0123456789abcdef0123456789abcdef0",  # one long
        " 0123456789abcdef0123456789abcdef",  # leading space
        "0123456789abcdef0123456789abcdef ",  # trailing space
        "0123456789abcdef0123456789abcdef\n",  # trailing newline
        "0123456789abcdef-0123456789abcde",  # punctuation
        "0123456789abcdefg123456789abcdef",  # non-hex character
        "TASK-7f3ab21c9d",
        "4bf92f35-77b3-4da6-a3ce-929d0e0e4736",  # dashed UUID
        "0x0123456789abcdef0123456789abcdef",
    ],
)
def test_validator_rejects_malformed_invocation_ids(value):
    assert is_valid_invocation_id(value) is False


def test_validator_accepts_only_the_exact_shape():
    assert is_valid_invocation_id("0" * 32) is True
    assert is_valid_invocation_id("f" * 32) is True
    assert is_valid_invocation_id(new_invocation_id()) is True


# ------------------------------------------------------------------------------------------------
# 6-8. Trace context
# ------------------------------------------------------------------------------------------------


def test_current_traceparent_is_none_without_an_active_recording_span():
    assert current_traceparent() is None


def test_current_traceparent_is_none_for_a_non_recording_span(monkeypatch):
    """A sampled-out or restored context is not a parent Forge will ever export."""

    def unreachable() -> object:  # pragma: no cover - asserted by not being reached
        raise AssertionError("the propagator must not be consulted for a non-recording span")

    monkeypatch.setattr(agent_telemetry, "_active_span", lambda: FakeSpan(recording=False))
    monkeypatch.setattr(agent_telemetry, "_traceparent_propagator", unreachable)
    assert current_traceparent() is None


def test_traceparent_matches_the_active_forge_phase_span():
    """The trace id and the parent span id are the active `forge.phase` span's own identifiers."""
    pytest.importorskip("opentelemetry.sdk.trace", reason="telemetry extra is not installed")
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from codex_forge.config import TelemetryConfig
    from codex_forge.telemetry import PHASE_IMPLEMENTATION, ROLE_IMPLEMENTER, Telemetry

    exporter = InMemorySpanExporter()
    telemetry = Telemetry(
        TelemetryConfig(enabled=True), processor_factory=lambda: SimpleSpanProcessor(exporter)
    )
    telemetry.start()

    observed: dict[str, str | None] = {}
    try:
        with telemetry.task_span(task_id="TASK-1", project_id="p", risk="low", max_attempts=1):
            observed["task"] = current_traceparent()
            with telemetry.attempt_span(attempt_number=1):
                observed["attempt"] = current_traceparent()
                with telemetry.phase_span(
                    phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER
                ):
                    observed["phase"] = current_traceparent()
                    live = trace_api.get_current_span().get_span_context()
    finally:
        telemetry.shutdown()

    phase_header = observed["phase"]
    assert is_valid_traceparent(phase_header)
    version, trace_id, parent_id, flags = phase_header.split("-")
    assert version == "00"
    # The sampled bit, without pinning the other flag bits: the SDK also sets the random-trace-id
    # bit on recent releases, and a flags byte is a vendor surface Forge does not control.
    assert int(flags, 16) & 0x01 == 0x01
    # Matches the span that was live when the header was produced ...
    assert trace_id == f"{live.trace_id:032x}"
    assert parent_id == f"{live.span_id:016x}"

    # ... and matches the `forge.phase` span the exporter actually received.
    phase_spans = [span for span in exporter.get_finished_spans() if span.name == "forge.phase"]
    assert len(phase_spans) == 1
    assert trace_id == f"{phase_spans[0].context.trace_id:032x}"
    assert parent_id == f"{phase_spans[0].context.span_id:016x}"

    # Each level names its own span, so the phase header is not merely "some active span".
    assert len({observed["task"], observed["attempt"], observed["phase"]}) == 3
    assert observed["task"].split("-")[1] == trace_id
    assert current_traceparent() is None


def test_traceparent_never_carries_tracestate(monkeypatch):
    """The propagator may emit `tracestate`; `current_traceparent` returns only the one header."""
    injected: list[dict[str, str]] = []

    class TracestateEmittingPropagator:
        def inject(self, carrier: dict[str, str]) -> None:
            carrier["traceparent"] = SAMPLE_TRACEPARENT
            carrier["tracestate"] = "vendor=secret-operator-value"
            injected.append(carrier)

    monkeypatch.setattr(agent_telemetry, "_active_span", lambda: FakeSpan(recording=True))
    monkeypatch.setattr(agent_telemetry, "_traceparent_propagator", TracestateEmittingPropagator)
    value = current_traceparent()

    assert value == SAMPLE_TRACEPARENT
    assert "tracestate" in injected[0]
    assert "secret-operator-value" not in value
    built = build_claude_child_environment({}, context(value))
    assert "TRACESTATE" not in built
    assert "secret-operator-value" not in " ".join(built.values())


@pytest.mark.parametrize(
    "failure",
    [
        "import",
        "construction",
        "inject",
        "malformed",
        "invalid-context",
        "missing",
    ],
)
def test_propagation_failures_return_none_and_never_escape(monkeypatch, failure):
    class Exploding:
        def inject(self, carrier: dict[str, str]) -> None:
            raise RuntimeError("collector unreachable at http://127.0.0.1:4317")

    class Malformed:
        def inject(self, carrier: dict[str, str]) -> None:
            carrier["traceparent"] = "not-a-traceparent"

    class InvalidContext:
        def inject(self, carrier: dict[str, str]) -> None:
            carrier["traceparent"] = "00-" + "0" * 32 + "-" + "0" * 16 + "-01"

    class Missing:
        def inject(self, carrier: dict[str, str]) -> None:
            return None

    def raise_import() -> object:
        raise ModuleNotFoundError("No module named 'opentelemetry'")

    monkeypatch.setattr(agent_telemetry, "_active_span", lambda: FakeSpan(recording=True))
    if failure == "import":
        monkeypatch.setattr(agent_telemetry, "_active_span", raise_import)
    elif failure == "construction":
        monkeypatch.setattr(agent_telemetry, "_traceparent_propagator", raise_import)
    else:
        factory = {
            "inject": Exploding,
            "malformed": Malformed,
            "invalid-context": InvalidContext,
            "missing": Missing,
        }[failure]
        monkeypatch.setattr(agent_telemetry, "_traceparent_propagator", factory)

    assert current_traceparent() is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "",
        SAMPLE_TRACEPARENT.upper(),
        SAMPLE_TRACEPARENT + "-extra",
        SAMPLE_TRACEPARENT[:-1],
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # forbidden version
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",  # zero trace id
        "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",  # zero parent id
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",  # no flags
    ],
)
def test_traceparent_validator_fails_closed(value):
    assert is_valid_traceparent(value) is False


def test_invocation_context_carries_only_three_fields():
    fresh = new_invocation_context()
    assert set(InvocationContext.__dataclass_fields__) == {
        "invocation_id",
        "contract_version",
        "traceparent",
    }
    assert is_valid_invocation_id(fresh.invocation_id)
    assert fresh.contract_version == AGENT_TELEMETRY_CONTRACT_VERSION == "1.0"
    assert fresh.traceparent is None
    assert new_invocation_context().invocation_id != fresh.invocation_id


# ------------------------------------------------------------------------------------------------
# 9-16. The Claude child environment
# ------------------------------------------------------------------------------------------------

# An independent statement of the approved block. Deliberately written out rather than imported from
# the module, so an edit to the module cannot silently redefine what "approved" means.
APPROVED = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "none",
    "OTEL_LOG_USER_PROMPTS": "false",
    "OTEL_LOG_ASSISTANT_RESPONSES": "false",
    "OTEL_LOG_RAW_API_BODIES": "false",
    "OTEL_LOG_TOOL_DETAILS": "false",
    "OTEL_LOG_TOOL_CONTENT": "false",
    "OTEL_METRICS_INCLUDE_SESSION_ID": "false",
    "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
    "OTEL_METRICS_INCLUDE_VERSION": "false",
    "OTEL_METRICS_INCLUDE_ENTRYPOINT": "false",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4317",
    "OTEL_EXPORTER_OTLP_INSECURE": "true",
    "OTEL_EXPORTER_OTLP_HEADERS": "",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS": "",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
    "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    "OTEL_TRACES_EXPORT_INTERVAL": "1000",
    "CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS": "2000",
    "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS": "2000",
}

HOSTILE_ENVIRONMENT = {
    # Another exporter, another destination.
    "OTEL_TRACES_EXPORTER": "console",
    "OTEL_METRICS_EXPORTER": "prometheus",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.evil.invalid:4317",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://collector.evil.invalid/v1/traces",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://collector.evil.invalid/v1/logs",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_INSECURE": "false",
    # Credentials and headers.
    "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer sk-live-EVIL",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-api-key=sk-live-EVIL",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS": "x-api-key=sk-live-EVIL",
    "OTEL_EXPORTER_OTLP_CERTIFICATE": r"C:\secrets\client.pem",
    # Resource attributes carrying identity and repository detail.
    "OTEL_RESOURCE_ATTRIBUTES": "user.email=operator@example.invalid,vcs.branch=feat/secret",
    "OTEL_SERVICE_NAME": "operator-workstation",
    # Content logging.
    "OTEL_LOG_USER_PROMPTS": "true",
    "OTEL_LOG_ASSISTANT_RESPONSES": "true",
    "OTEL_LOG_RAW_API_BODIES": "true",
    "OTEL_LOG_TOOL_DETAILS": "true",
    "OTEL_LOG_TOOL_CONTENT": "true",
    "OTEL_METRICS_INCLUDE_SESSION_ID": "true",
    "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "true",
    "OTEL_METRICS_INCLUDE_VERSION": "true",
    "OTEL_METRICS_INCLUDE_ENTRYPOINT": "true",
    # Propagation and SDK-wide switches.
    "OTEL_PROPAGATORS": "b3,baggage,jaeger",
    "OTEL_SDK_DISABLED": "true",
    # Inherited trace context.
    "TRACEPARENT": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    "TRACESTATE": "vendor=secret-operator-value",
    # Claude's own switches, including the two that must never be enabled.
    "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "0",
    "CLAUDE_CODE_PROPAGATE_TRACEPARENT": "1",
    "CLAUDE_CODE_WORKSPACE_HOST_PATHS": r"C:\CodexData\worktrees\TASK-7f3ab21c9d",
    "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS": "600000",
    "ENABLE_BETA_TRACING_DETAILED": "1",
    "BETA_TRACING_ENDPOINT": "https://beta.evil.invalid/v1/traces",
    # Lower and mixed case: Windows environment names are case-insensitive, so these are the same
    # variables as far as a child process is concerned.
    "otel_exporter_otlp_logs_headers": "authorization=Bearer sk-live-EVIL",
    "claude_code_otel_shutdown_timeout_ms": "600000",
    "TraceParent": "00-" + "c" * 32 + "-" + "d" * 16 + "-01",
    "Claude_Code_Propagate_Traceparent": "1",
}

# Every unrelated variable in the hostile base must come out the other side untouched.
UNRELATED_ENVIRONMENT = {
    "PATH": r"C:\Windows\System32;C:\Tools",
    "USERPROFILE": r"C:\Users\operator",
    "ANTHROPIC_API_KEY": "sk-ant-operator-key",
    "CODEX_HOME": r"C:\Users\operator\.codex",
    "OTELCOL_PATH": r"C:\Tools\otelcol-contrib\otelcol-contrib.exe",
    "HTTP_PROXY": "http://proxy.example.invalid:8080",
    "PYTHONUTF8": "1",
    "NOT_OTEL_RELATED": "keep me",
    "MY_OTEL_SETTING": "keep me too",
}

# Substrings that must appear nowhere in a built environment's telemetry settings.
LEAK_SENTINELS = (
    "evil.invalid",
    "sk-live-EVIL",
    "operator@example.invalid",
    r"C:\secrets",
    "b3,baggage,jaeger",
    "secret-operator-value",
    "vcs.branch",
    "operator-workstation",
    "TASK-7f3ab21c9d",
    "a" * 32,
    "c" * 32,
)


def expected_environment(invocation_id: str, traceparent: str | None) -> dict[str, str]:
    expected = dict(APPROVED)
    expected["OTEL_RESOURCE_ATTRIBUTES"] = (
        f"forge.invocation.id={invocation_id},forge.telemetry.contract.version=1.0"
    )
    if traceparent is not None:
        expected["TRACEPARENT"] = traceparent
    return expected


def test_builder_returns_a_new_dictionary():
    base = {"PATH": r"C:\Windows\System32"}
    built = build_claude_child_environment(base, context(SAMPLE_TRACEPARENT))
    assert isinstance(built, dict)
    assert built is not base
    built["PATH"] = "mutated"
    built["NEW"] = "value"
    assert base == {"PATH": r"C:\Windows\System32"}


def test_builder_mutates_neither_os_environ_nor_the_supplied_mapping():
    environ_before = dict(os.environ)
    base = {**HOSTILE_ENVIRONMENT, **UNRELATED_ENVIRONMENT}
    base_before = dict(base)

    build_claude_child_environment(base, context(SAMPLE_TRACEPARENT))
    build_claude_child_environment(os.environ, context())

    assert base == base_before
    assert dict(os.environ) == environ_before


def test_every_approved_key_has_its_exact_required_value():
    fresh = context(SAMPLE_TRACEPARENT)
    built = build_claude_child_environment({}, fresh)
    assert built == expected_environment(fresh.invocation_id, SAMPLE_TRACEPARENT)
    assert set(built) == CLAUDE_CHILD_ENVIRONMENT_KEYS


def test_traceparent_is_omitted_rather_than_blanked_when_there_is_no_active_span():
    fresh = context(None)
    built = build_claude_child_environment({}, fresh)
    assert "TRACEPARENT" not in built
    assert built == expected_environment(fresh.invocation_id, None)


def test_forbidden_variables_are_absent():
    built = build_claude_child_environment(HOSTILE_ENVIRONMENT, context(SAMPLE_TRACEPARENT))
    upper = {key.upper() for key in built}
    for key in FORBIDDEN_CHILD_KEYS:
        assert key not in upper, key
    assert "CLAUDE_CODE_PROPAGATE_TRACEPARENT" not in upper
    assert "CLAUDE_CODE_WORKSPACE_HOST_PATHS" not in upper
    assert "ENABLE_BETA_TRACING_DETAILED" not in upper
    assert "BETA_TRACING_ENDPOINT" not in upper
    assert "TRACESTATE" not in upper
    assert "CODEX_HOME" not in upper
    assert FORBIDDEN_CHILD_KEYS & CLAUDE_CHILD_ENVIRONMENT_KEYS == frozenset()


def test_codex_home_is_neither_set_nor_removed():
    """Forge must not relocate the Codex home; an operator's own value is unrelated configuration."""
    assert "CODEX_HOME" not in build_claude_child_environment({}, context())
    inherited = build_claude_child_environment(
        {"CODEX_HOME": r"C:\Users\operator\.codex"}, context()
    )
    assert inherited["CODEX_HOME"] == r"C:\Users\operator\.codex"


def test_hostile_inherited_configuration_is_neutralized():
    fresh = context(SAMPLE_TRACEPARENT)
    built = build_claude_child_environment(
        {**HOSTILE_ENVIRONMENT, **UNRELATED_ENVIRONMENT}, fresh
    )

    for key in HOSTILE_ENVIRONMENT:
        assert is_neutralized_key(key), key
        approved_value = expected_environment(fresh.invocation_id, SAMPLE_TRACEPARENT).get(
            key.upper()
        )
        if approved_value is None:
            assert key not in built and key.upper() not in built, key
        else:
            assert built[key.upper()] == approved_value, key

    telemetry_values = " ".join(built[key] for key in CLAUDE_CHILD_ENVIRONMENT_KEYS if key in built)
    for sentinel in LEAK_SENTINELS:
        assert sentinel not in telemetry_values, sentinel

    # The whole hostile block is gone, whatever case it arrived in.
    survivors = {key for key in built if is_neutralized_key(key)} - CLAUDE_CHILD_ENVIRONMENT_KEYS
    assert survivors == set()


def test_unrelated_variables_survive_unchanged():
    built = build_claude_child_environment(
        {**HOSTILE_ENVIRONMENT, **UNRELATED_ENVIRONMENT}, context(SAMPLE_TRACEPARENT)
    )
    for key, value in UNRELATED_ENVIRONMENT.items():
        assert built[key] == value, key


@pytest.mark.parametrize(
    "key,neutralized",
    [
        ("OTEL_TRACES_EXPORTER", True),
        ("otel_traces_exporter", True),
        ("OTEL_ANYTHING_AT_ALL", True),
        ("CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS", True),
        ("TRACEPARENT", True),
        ("TRACESTATE", True),
        ("BETA_TRACING_ENDPOINT", True),
        ("ENABLE_BETA_TRACING_DETAILED", True),
        ("CLAUDE_CODE_WORKSPACE_HOST_PATHS", True),
        ("CLAUDE_CODE_PROPAGATE_TRACEPARENT", True),
        ("OTELCOL_PATH", False),
        ("MY_OTEL_SETTING", False),
        ("CLAUDE_CODE_MAX_OUTPUT_TOKENS", False),
        ("CODEX_HOME", False),
        ("PATH", False),
        ("ANTHROPIC_API_KEY", False),
        ("TRACE", False),
        (None, False),
    ],
)
def test_neutralization_policy_is_exactly_as_documented(key, neutralized):
    assert is_neutralized_key(key) is neutralized


def test_resource_attributes_contain_only_the_two_approved_attributes():
    fresh = context(SAMPLE_TRACEPARENT)
    value = build_claude_child_environment({}, fresh)["OTEL_RESOURCE_ATTRIBUTES"]
    pairs = [item.split("=", 1) for item in value.split(",")]

    assert [key for key, _ in pairs] == [ATTR_INVOCATION_ID, ATTR_CONTRACT_VERSION]
    assert [unquote(item) for _, item in pairs] == [fresh.invocation_id, "1.0"]
    assert value == encode_resource_attributes(fresh)
    assert value.count(",") == 1


def test_resource_attribute_encoding_cannot_forge_a_third_attribute():
    """Percent-encoding is what stops a future value from smuggling a separator."""
    hostile = encode_resource_attributes(
        InvocationContext(invocation_id="x,user.email=operator@example.invalid,y")
    )
    assert hostile.count(",") == 1
    assert "user.email=" not in hostile
    assert unquote(hostile.split("=", 1)[1].split(",")[0]) == "x,user.email=operator@example.invalid,y"
    # The builder still refuses that identifier; the encoding is defence in depth beneath it.
    with pytest.raises(ValueError):
        build_claude_child_environment({}, InvocationContext(invocation_id="x,user.email=a,y"))


def test_no_builder_output_carries_task_project_or_operator_material():
    """Nothing the builder writes derives from a task, a path, a branch, an account, or a prompt."""
    fresh = context(SAMPLE_TRACEPARENT)
    for base in ({}, dict(HOSTILE_ENVIRONMENT), {**HOSTILE_ENVIRONMENT, **UNRELATED_ENVIRONMENT}):
        built = build_claude_child_environment(base, fresh)
        written = " ".join(
            f"{key}={built[key]}" for key in sorted(CLAUDE_CHILD_ENVIRONMENT_KEYS) if key in built
        )
        for material in HOSTILE_MATERIAL.values():
            assert material not in written
        for sentinel in LEAK_SENTINELS:
            assert sentinel not in written
        assert "sk-ant-operator-key" not in written
        assert r"C:\Users\operator" not in written


def test_builder_rejects_malformed_contexts():
    with pytest.raises(ValueError, match="invocation id"):
        build_claude_child_environment({}, InvocationContext(invocation_id="TASK-7f3ab21c9d"))
    with pytest.raises(ValueError, match="invocation id"):
        build_claude_child_environment({}, InvocationContext(invocation_id="A" * 32))
    with pytest.raises(ValueError, match="contract version"):
        build_claude_child_environment(
            {}, InvocationContext(invocation_id="0" * 32, contract_version="2.0")
        )
    with pytest.raises(ValueError, match="traceparent"):
        build_claude_child_environment(
            {}, InvocationContext(invocation_id="0" * 32, traceparent="not-a-traceparent")
        )
    with pytest.raises(TypeError):
        build_claude_child_environment({}, {"invocation_id": "0" * 32})
    with pytest.raises(TypeError):
        build_claude_child_environment({"PATH": 1}, context())
    with pytest.raises(TypeError):
        build_claude_child_environment({1: "value"}, context())


# ------------------------------------------------------------------------------------------------
# 17. Loopback endpoint validation
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://collector.evil.invalid:4317",
        "http://10.0.0.5:4317",
        "http://0.0.0.0:4317",
        "http://169.254.169.254:80",
        "http://127.0.0.1.evil.invalid:4317",
        "https://127.0.0.1:4317",
        "grpc://127.0.0.1:4317",
        "unix:///tmp/collector.sock",
        "127.0.0.1:4317",
        "//127.0.0.1:4317",
        "http://127.0.0.1",
        "http://localhost",
        "http://user:password@127.0.0.1:4317",
        "http://token@127.0.0.1:4317",
        "http://127.0.0.1:4317/v1/traces",
        "http://127.0.0.1:4317/",
        "http://127.0.0.1:4317?key=secret",
        "http://127.0.0.1:4317#fragment",
        "http://127.0.0.1:notaport",
        "http://[::1]:4317",
        "http://127.0.0.1:4317 ",
        " http://127.0.0.1:4317",
        "http://127.0.0.1:43 17",
        "",
        None,
        4317,
        "http://127.0.0.1:4317" + "/x" * 200,
    ],
)
def test_non_loopback_and_malformed_endpoints_fail_closed(endpoint):
    with pytest.raises(ValueError):
        validate_loopback_endpoint(endpoint)
    with pytest.raises(ValueError):
        build_claude_child_environment({}, context(), endpoint=endpoint)


@pytest.mark.parametrize(
    "endpoint", ["http://127.0.0.1:4317", "http://localhost:4317", "http://127.0.0.1:14317"]
)
def test_loopback_endpoints_are_accepted_unchanged(endpoint):
    assert validate_loopback_endpoint(endpoint) == endpoint
    built = build_claude_child_environment({}, context(), endpoint=endpoint)
    assert built["OTEL_EXPORTER_OTLP_ENDPOINT"] == endpoint


# ------------------------------------------------------------------------------------------------
# 17a-17k. The Codex child environment and the Codex `-c` overrides (Gate 7 foundation)
# ------------------------------------------------------------------------------------------------
#
# Gate 7 has not passed and no Codex process has run. Everything below tests the two pure builders
# against synthetic input; none of it is evidence that Codex accepts the override.

CODEX_ENDPOINT = "http://127.0.0.1:4318"


def test_codex_environment_builder_adds_nothing_and_removes_only_telemetry():
    base = {**HOSTILE_ENVIRONMENT, **UNRELATED_ENVIRONMENT}
    built = agent_telemetry.build_codex_child_environment(base, endpoint=CODEX_ENDPOINT)

    # Codex is configured by `-c`, so there is no approved block and nothing is added at all.
    assert agent_telemetry.CODEX_CHILD_ENVIRONMENT_KEYS == frozenset()
    assert set(built) - set(base) == set()
    for key, value in UNRELATED_ENVIRONMENT.items():
        assert built[key] == value, key
    assert {key for key in built if is_neutralized_key(key)} == set()
    joined = " ".join(f"{key}={value}" for key, value in built.items())
    for sentinel in LEAK_SENTINELS:
        if sentinel in " ".join(UNRELATED_ENVIRONMENT.values()):
            continue
        assert sentinel not in joined, sentinel


def test_codex_environment_builder_mutates_neither_os_environ_nor_the_supplied_mapping():
    before_environ = dict(os.environ)
    base = {**HOSTILE_ENVIRONMENT, **UNRELATED_ENVIRONMENT}
    before_base = dict(base)

    built = agent_telemetry.build_codex_child_environment(base, endpoint=CODEX_ENDPOINT)

    assert base == before_base
    assert dict(os.environ) == before_environ
    assert built is not base


@pytest.mark.parametrize(
    "key",
    [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "otel_exporter_otlp_endpoint",
        "Otel_Exporter_Otlp_Headers",
        "OTEL_LOGS_EXPORTER",
        "OTEL_SDK_DISABLED",
        "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS",
    ],
)
def test_codex_environment_removes_inherited_otel_configuration_case_insensitively(key):
    """Codex 0.146.0 embeds an OTLP exporter that reads these; an inherited one is a destination."""
    built = agent_telemetry.build_codex_child_environment(
        {key: "https://collector.evil.invalid:4317", "PATH": "/usr/bin"}, endpoint=CODEX_ENDPOINT
    )
    assert key not in built
    assert built == {"PATH": "/usr/bin"}


@pytest.mark.parametrize("key", ["TRACEPARENT", "TRACESTATE", "TraceParent", "tracestate"])
def test_codex_environment_removes_inherited_trace_context(key):
    built = agent_telemetry.build_codex_child_environment(
        {key: "00-" + "a" * 32 + "-" + "b" * 16 + "-01", "PATH": "/usr/bin"},
        endpoint=CODEX_ENDPOINT,
    )
    assert built == {"PATH": "/usr/bin"}


def test_codex_environment_preserves_an_existing_codex_home_exactly():
    """Contract section 4: Forge neither sets nor relocates the Codex home."""
    home = r"C:\Users\operator\.codex"
    built = agent_telemetry.build_codex_child_environment(
        {"CODEX_HOME": home, "OTEL_TRACES_EXPORTER": "console"}, endpoint=CODEX_ENDPOINT
    )
    assert built["CODEX_HOME"] == home
    assert built == {"CODEX_HOME": home}


def test_codex_environment_leaves_a_missing_codex_home_missing():
    built = agent_telemetry.build_codex_child_environment({"PATH": "/usr/bin"}, endpoint=CODEX_ENDPOINT)
    assert "CODEX_HOME" not in built
    assert not any(key.upper() == "CODEX_HOME" for key in built)


def test_codex_environment_leaves_authentication_variables_untouched():
    """A canary that cannot authenticate is a canary that proves nothing; these must survive."""
    credentials = {
        "CODEX_HOME": r"C:\Users\operator\.codex",
        "OPENAI_API_KEY": "sk-operator-key",
        "USERPROFILE": r"C:\Users\operator",
        "APPDATA": r"C:\Users\operator\AppData\Roaming",
    }
    built = agent_telemetry.build_codex_child_environment(
        {**credentials, **HOSTILE_ENVIRONMENT}, endpoint=CODEX_ENDPOINT
    )
    for key, value in credentials.items():
        assert built[key] == value, key


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://10.0.0.5:4318",
        "https://127.0.0.1:4318",
        "http://collector.example.invalid:4318",
        "http://127.0.0.1",
        "http://user:pass@127.0.0.1:4318",
        "http://127.0.0.1:4318/v1/traces",
        "",
    ],
)
def test_codex_builders_accept_only_a_loopback_collector(endpoint):
    with pytest.raises(ValueError):
        agent_telemetry.build_codex_child_environment({}, endpoint=endpoint)
    with pytest.raises(ValueError):
        agent_telemetry.build_codex_otel_overrides(context(), endpoint=endpoint)


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:4318", "http://localhost:14318"])
def test_codex_builders_accept_loopback_endpoints(endpoint):
    assert agent_telemetry.build_codex_child_environment({}, endpoint=endpoint) == {}
    overrides = agent_telemetry.build_codex_otel_overrides(context(), endpoint=endpoint)
    assert sum(1 for item in overrides if endpoint in item) == 2


def _override_values(overrides: tuple[str, ...]) -> dict[str, str]:
    assert len(overrides) % 2 == 0
    assert overrides[::2] == ("-c",) * (len(overrides) // 2), "every value must have its own -c"
    return dict(item.split("=", 1) for item in overrides[1::2])


def test_codex_overrides_are_an_immutable_vector_of_separate_arguments():
    overrides = agent_telemetry.build_codex_otel_overrides(context(), endpoint=CODEX_ENDPOINT)
    assert isinstance(overrides, tuple)
    assert all(isinstance(item, str) for item in overrides)
    # No element may carry an embedded space that a shim could re-split on, and none may be a joined
    # `-c key=value` pair.
    assert [item for item in overrides if " " in item] == []
    assert tuple(_override_values(overrides)) == agent_telemetry.CODEX_OTEL_OVERRIDE_KEYS


def test_codex_overrides_disable_logs_and_prompt_logging():
    values = _override_values(
        agent_telemetry.build_codex_otel_overrides(context(), endpoint=CODEX_ENDPOINT)
    )
    assert values["otel.exporter"] == '"none"'
    assert values["otel.log_user_prompt"] == "false"
    assert agent_telemetry.CODEX_LOGS_EXPORTER == "none"


def test_codex_overrides_send_traces_and_metrics_only_to_the_loopback_collector():
    values = _override_values(
        agent_telemetry.build_codex_otel_overrides(context(), endpoint=CODEX_ENDPOINT)
    )
    for key in ("otel.trace_exporter", "otel.metrics_exporter"):
        assert values[key] == (
            '{otlp-http={endpoint="http://127.0.0.1:4318",protocol="binary",headers={}}}'
        ), key
    assert values["otel.environment"] == '"forge-canary"'
    # Exactly one destination appears anywhere in the vector, and it is the loopback Collector.
    assert " ".join(values.values()).count("http://") == 2
    assert "otel.tracestate" not in values
    assert agent_telemetry.CODEX_FORBIDDEN_OVERRIDE_KEYS & set(values) == frozenset()


def test_codex_overrides_carry_both_canonical_correlation_attributes_and_no_third():
    fresh = context(SAMPLE_TRACEPARENT)
    values = _override_values(
        agent_telemetry.build_codex_otel_overrides(fresh, endpoint=CODEX_ENDPOINT)
    )
    attributes = values["otel.span_attributes"]

    assert attributes == (
        '{"forge.invocation.id"="%s","forge.telemetry.contract.version"="1.0"}' % fresh.invocation_id
    )
    assert attributes.count("=") == 2, "exactly two attributes, and no third"
    assert agent_telemetry.CODEX_SPAN_ATTRIBUTE_KEYS == (ATTR_INVOCATION_ID, ATTR_CONTRACT_VERSION)
    # A traceparent is never forwarded to Codex: contract section 4 admits inbound context only.
    assert SAMPLE_TRACEPARENT not in " ".join(values.values())


def test_codex_overrides_use_the_canonical_invocation_id_format():
    """One identifier format across both providers. A second format is a second thing to review."""
    fresh = new_invocation_context()
    values = _override_values(
        agent_telemetry.build_codex_otel_overrides(fresh, endpoint=CODEX_ENDPOINT)
    )
    embedded = values["otel.span_attributes"].split('"forge.invocation.id"="', 1)[1].split('"', 1)[0]

    assert embedded == fresh.invocation_id
    assert is_valid_invocation_id(embedded)
    assert len(embedded) == INVOCATION_ID_CHARS == 32
    claude = build_claude_child_environment({}, fresh)["OTEL_RESOURCE_ATTRIBUTES"]
    assert fresh.invocation_id in claude, "both providers carry the same value, in the same format"


@pytest.mark.parametrize(
    "invalid",
    [
        InvocationContext(invocation_id="not-hex"),
        InvocationContext(invocation_id=new_invocation_id().upper()),
        InvocationContext(invocation_id=new_invocation_id(), contract_version="2.0"),
        InvocationContext(invocation_id='x" ,user.email="a'),
    ],
)
def test_codex_overrides_fail_closed_on_a_context_they_cannot_render(invalid):
    with pytest.raises(ValueError):
        agent_telemetry.build_codex_otel_overrides(invalid, endpoint=CODEX_ENDPOINT)


def test_codex_override_values_are_never_escaped_into_safety():
    """A value that would need escaping came from somewhere the builder does not accept input."""
    for hostile in ('a"b', "a\\b", "a b", "a\nb", "", "x" * 201):
        with pytest.raises(ValueError):
            agent_telemetry._toml_string(hostile)
    assert agent_telemetry._toml_string("http://127.0.0.1:4318") == '"http://127.0.0.1:4318"'


def test_codex_overrides_carry_no_task_project_or_operator_material():
    values = " ".join(
        _override_values(
            agent_telemetry.build_codex_otel_overrides(context(), endpoint=CODEX_ENDPOINT)
        ).values()
    )
    for material in HOSTILE_MATERIAL.values():
        assert material not in values


# ------------------------------------------------------------------------------------------------
# 18. Nothing is wired
# ------------------------------------------------------------------------------------------------


def test_no_production_module_imports_agent_telemetry():
    """The proof that this slice changes no runtime behavior: nothing calls it."""
    importers = [
        path.name
        for path in sorted(SOURCE_ROOT.glob("*.py"))
        if path.name != "agent_telemetry.py"
        and "agent_telemetry" in path.read_text(encoding="utf-8")
    ]
    assert importers == []


def test_runner_carries_no_agent_telemetry_wiring():
    """`CodexRunner` builds the same arguments and the same environment as before this slice."""
    source = (SOURCE_ROOT / "runner.py").read_text(encoding="utf-8")
    for token in (
        "agent_telemetry",
        "TRACEPARENT",
        "OTEL_",
        "CLAUDE_CODE",
        "invocation",
        "CODEX_HOME",
    ):
        assert token not in source, token


def test_worker_environment_introduces_no_agent_telemetry_variables():
    from codex_forge.runner import _worker_environment

    environment = _worker_environment()
    added = set(environment) - set(os.environ)
    assert added <= {"PATH", "VIRTUAL_ENV", "PYTHONIOENCODING", "PYTHONUTF8"}
    for key in CLAUDE_CHILD_ENVIRONMENT_KEYS:
        assert environment.get(key) == os.environ.get(key), key
    assert "CODEX_HOME" not in added


# ------------------------------------------------------------------------------------------------
# 19-20. Import purity, and no dependency on the tracing SDK
# ------------------------------------------------------------------------------------------------


def test_importing_the_module_has_no_side_effects():
    result = run_child(
        """
        import os, sys
        before = dict(os.environ)
        import codex_forge.agent_telemetry as module
        assert dict(os.environ) == before, "import mutated os.environ"
        loaded = sorted(name for name in sys.modules if "opentelemetry" in name or name == "grpc")
        assert loaded == [], loaded
        assert module.AGENT_TELEMETRY_CONTRACT_VERSION == "1.0"
        print("NO_SIDE_EFFECTS")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "NO_SIDE_EFFECTS" in result.stdout


def test_module_is_fully_usable_without_the_telemetry_extra():
    result = run_child(
        """
        from codex_forge.agent_telemetry import (
            build_claude_child_environment, current_traceparent, is_valid_invocation_id,
            new_invocation_context,
        )
        assert current_traceparent() is None
        fresh = new_invocation_context()
        assert is_valid_invocation_id(fresh.invocation_id)
        assert fresh.traceparent is None
        built = build_claude_child_environment({"PATH": "/usr/bin"}, fresh)
        assert built["OTEL_LOGS_EXPORTER"] == "none"
        assert built["PATH"] == "/usr/bin"
        assert "TRACEPARENT" not in built
        print("NO_EXTRA_OK")
        """,
        block_opentelemetry=True,
    )
    assert result.returncode == 0, result.stderr
    assert "NO_EXTRA_OK" in result.stdout


def test_helpers_degrade_in_process_without_opentelemetry(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    for name in [key for key in sys.modules if key.startswith("opentelemetry")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    assert current_traceparent() is None
    fresh = new_invocation_context()
    assert fresh.traceparent is None
    assert build_claude_child_environment({}, fresh)["OTEL_LOGS_EXPORTER"] == "none"
