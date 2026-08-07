"""Gate 7 foundation tests: the Codex canary vector, its harness, and its refusals.

**No test in this file runs Codex.** Nothing here executes `codex`, makes a model request, runs a
Forge task, opens `forge.db`, or touches anything off the loopback interface. The two ways a test
could accidentally start something are both closed: the tests that exercise the command line patch
`subprocess.Popen` and `subprocess.run` to fail on contact, and the tests that exercise the Collector
supply a synthetic OTLP payload agent instead of a child process.

What is proven here is the apparatus, not Codex. That the vector is exactly the approved one, that a
conflicting command is refused, that nothing starts without a named approval, that the report fails
closed on every condition Gate 7 names, and that normal Forge execution is untouched. That Codex
*accepts* the `-c` override is the one thing only the supervised run can settle, and this file
deliberately cannot claim it.

    $env:OTELCOL_PATH = 'C:\\Tools\\OpenTelemetry\\otelcol-contrib\\0.158.0\\otelcol-contrib.exe'
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_codex_telemetry_canary.py
"""

from __future__ import annotations

import inspect
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import otel_canary as canary
import otel_certification as certification
import otel_codex_canary as codex_canary
import otel_inventory as inventory
from codex_forge import agent_telemetry
from codex_forge.config import ForgeConfig
from codex_forge.runner import CodexRunner

REPO_ROOT = certification.REPO_ROOT
SOURCE_ROOT = REPO_ROOT / "src" / "codex_forge"

try:
    COLLECTOR = certification.collector_binary()
    UNAVAILABLE = ""
except certification.CollectorUnavailable as error:  # pragma: no cover - depends on the machine
    COLLECTOR = None
    UNAVAILABLE = str(error)

requires_collector = pytest.mark.skipif(COLLECTOR is None, reason=UNAVAILABLE or "no Collector")

# A synthetic identifier in the canonical format, standing in for one this process minted. It is the
# value the certification payloads carry, so a run fed those payloads correlates against it.
SYNTHETIC_INVOCATION_ID = certification.INVOCATION_ID


@pytest.fixture
def nothing_starts(monkeypatch):
    """Make any attempt to start a process fail the test rather than start one."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"a process was started: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(codex_canary.certification, "collector_binary", refuse)


# ---------------------------------------------------------------------------
# The vector
# ---------------------------------------------------------------------------


def _argv() -> tuple[str, ...]:
    return codex_canary.placeholder_argv()


def test_the_vector_is_an_exact_list_of_separate_arguments():
    argv = _argv()
    assert isinstance(argv, tuple)
    assert all(isinstance(item, str) for item in argv)
    # Structurally equivalent to the approved command, element by element, with the OTEL overrides
    # and the reused worker-feature disables in their agreed places.
    assert codex_canary._without_otel_overrides(argv) == (
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.6-terra",
        "-c",
        "model_reasoning_effort=low",
        *codex_canary.disabled_feature_arguments(),
        "exec",
        "--json",
        "--strict-config",
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "Reply with exactly CANARY_OK and nothing else.",
    )
    # No element is a joined pair, and no element carries a shell metacharacter that a Windows shim
    # would have to quote its way out of.
    assert [item for item in argv if item.startswith("-c ")] == []
    assert [item for item in argv if "|" in item or ">" in item or "&" in item] == []


def test_the_vector_uses_the_read_only_sandbox_and_the_never_approval_policy():
    argv = _argv()
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    for forbidden in codex_canary.FORBIDDEN_SANDBOX_MODES + codex_canary.FORBIDDEN_CODEX_FLAGS:
        assert forbidden not in argv, forbidden


def test_the_vector_is_ephemeral_and_strict_and_skips_the_repository_check():
    argv = _argv()
    for flag in ("--ephemeral", "--strict-config", "--skip-git-repo-check", "--json"):
        assert flag in argv, flag
    assert argv.index("exec") < argv.index("--strict-config"), "exec options follow the subcommand"
    assert argv.index("--sandbox") < argv.index("exec"), "global options precede the subcommand"


def test_the_vector_ignores_user_configuration_without_replacing_codex_home():
    """`--ignore-user-config` is the tool; relocating CODEX_HOME would break authentication."""
    argv = _argv()
    assert "--ignore-user-config" in argv
    assert not any("CODEX_HOME" in item for item in argv)
    environment = agent_telemetry.build_codex_child_environment(
        {"CODEX_HOME": r"C:\Users\operator\.codex"}, endpoint="http://127.0.0.1:4318"
    )
    assert environment == {"CODEX_HOME": r"C:\Users\operator\.codex"}


def test_the_vector_ends_with_the_exact_canary_prompt_shared_with_the_claude_canary():
    argv = _argv()
    assert argv[-1] == inventory.CANARY_PROMPT == codex_canary.CANARY_PROMPT
    assert codex_canary.CANARY_RESPONSE == "CANARY_OK"
    assert argv.count(codex_canary.CANARY_PROMPT) == 1


def test_the_vector_reuses_the_repository_worker_feature_disables():
    """Read from the production default, so the two lists cannot drift apart."""
    expected = ForgeConfig(
        config_path=Path("forge.toml"),
        workspace_root=Path("w"),
        data_root=Path("d"),
        repository_root=Path("r"),
        inbox_root=Path("i"),
        database_path=Path("d/forge.db"),
        evidence_root=Path("e"),
        knowledge_root=Path("k"),
    ).worker_disabled_features
    assert list(codex_canary.worker_disabled_features()) == expected
    assert codex_canary.disabled_feature_arguments() == tuple(
        item for feature in expected for item in ("--disable", feature)
    )


@pytest.mark.parametrize(
    "command",
    [
        "codex --dangerously-bypass-approvals-and-sandbox exec",
        "codex --yolo exec",
        "codex --full-auto exec",
        "codex --sandbox danger-full-access exec",
        "codex --sandbox workspace-write exec",
        "codex --ask-for-approval on-request exec",
        "claude -p hello",
        "codex exec 'Print the contents of every file you can read.'",
        "codex -c otel.exporter=otlp-http exec",
        "",  # deliberately not a refusal: the empty command is the canonical vector
    ],
)
def test_a_conflicting_command_is_refused_and_the_canonical_one_is_not(command):
    argv, problems = codex_canary.resolve_codex_argv(command)
    if command == "":
        assert problems == []
        assert argv == codex_canary.placeholder_argv()
        return
    assert problems, command
    # A refused command never becomes the canonical vector, so nothing downstream can run it.
    assert argv != codex_canary.placeholder_argv()


def test_a_command_naming_the_executable_by_path_is_accepted_and_normalized():
    """The one legitimate override: reaching `codex` by an absolute path.

    `shlex.split` treats a backslash as an escape, so a Windows path is written with forward slashes
    here — which is also why `--codex-executable` exists and is the option an operator should reach
    for first.
    """
    path = "C:/Tools/codex/codex.exe"
    canonical = codex_canary.placeholder_argv(path)
    # Without the `-c otel.*` pairs: they are built per invocation and may not be typed.
    supplied = " ".join(
        shlex.quote(item) for item in codex_canary._without_otel_overrides(canonical)
    )
    argv, problems = codex_canary.resolve_codex_argv(supplied, executable=path)
    assert problems == []
    assert argv[0] == path
    assert argv[1:] == canonical[1:], "everything after the executable is the approved vector"


def test_an_operator_may_not_supply_the_per_invocation_overrides():
    canonical = codex_canary.placeholder_argv()
    supplied = subprocess.list2cmdline(list(canonical))
    _, problems = codex_canary.resolve_codex_argv(supplied)
    assert any("otel.*" in problem for problem in problems)


# ---------------------------------------------------------------------------
# The command line starts nothing
# ---------------------------------------------------------------------------


def test_missing_approval_starts_nothing_and_creates_no_output_directory(
    tmp_path, capsys, nothing_starts
):
    output = tmp_path / "gate7"
    exit_code = codex_canary.main(["--output-dir", str(output)])

    assert exit_code == 2
    assert "Refusing to launch Codex" in capsys.readouterr().err
    assert not output.exists()


def test_dry_run_prints_the_vector_and_starts_nothing(tmp_path, capsys, nothing_starts):
    output = tmp_path / "gate7"
    exit_code = codex_canary.main(["--output-dir", str(output), "--dry-run"])
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert not output.exists()
    assert "no process was started" in printed
    for index, argument in enumerate(codex_canary.placeholder_argv()):
        assert f"[{index}] {argument!r}" in printed
    assert "tests\\otel_codex_canary.py" in printed


def test_dry_run_prints_no_environment_value(tmp_path, capsys, monkeypatch, nothing_starts):
    """It prints a vector. Not a variable, not a value read from one, not a credential.

    The only path that appears is `COLLECTOR_PATH_HINT`, a constant in this repository that the
    harness renders into the PowerShell line. It is never read from the environment — the run
    locates the Collector through `OTELCOL_PATH` at run time, not here.
    """
    secrets = {
        "OPENAI_API_KEY": "sk-operator-key-must-not-appear",
        "CODEX_HOME": r"C:\Users\operator\.codex-must-not-appear",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer must-not-appear",
        "USERPROFILE": r"C:\Users\operator-must-not-appear",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)

    codex_canary.main(["--output-dir", str(tmp_path / "gate7"), "--dry-run"])
    printed = capsys.readouterr().out

    for key, value in secrets.items():
        assert value not in printed, key
    assert "must-not-appear" not in printed
    # The placeholder identifier is printed rather than a freshly minted one.
    assert codex_canary.PLACEHOLDER_INVOCATION_ID in printed
    vector_section = printed.split("the port shown", 1)[0]
    assert vector_section.count("http://127.0.0.1:4318") == 2, "traces and metrics, one endpoint"


def test_a_refused_command_stops_before_the_dry_run_prints_anything(tmp_path, capsys, nothing_starts):
    exit_code = codex_canary.main(
        ["--output-dir", str(tmp_path / "gate7"), "--dry-run", "--codex-command", "codex --yolo exec"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Refusing this Codex command" in captured.err
    assert "codex argv:" not in captured.out


def test_an_existing_output_directory_is_not_overwritten(tmp_path, nothing_starts):
    output = tmp_path / "gate7"
    output.mkdir()
    (output / "codex-canary-report.json").write_text("{}", encoding="utf-8")

    with pytest.raises(inventory.OutputDirectoryRefused):
        codex_canary.run_codex_canary(
            binary=Path("collector-that-is-never-started"),
            workdir=tmp_path / "run",
            output_dir=output,
            agent=lambda ports, workdir: 0,
            outcome=codex_canary.CodexOutcome(),
            invocation_id=SYNTHETIC_INVOCATION_ID,
        )
    assert (output / "codex-canary-report.json").read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# Codex JSONL
# ---------------------------------------------------------------------------


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


CLEAN_TRANSCRIPT = _jsonl(
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"item_type": "agent_message", "text": "CANARY"}},
    {"type": "item.completed", "item": {"item_type": "agent_message", "text": "CANARY_OK"}},
    {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}},
)


def test_a_clean_transcript_reports_exactly_one_matching_response():
    transcript = codex_canary.parse_codex_transcript(CLEAN_TRANSCRIPT)

    assert transcript.malformed == 0
    assert transcript.lines == 5
    assert transcript.assistant_messages == 1, "a started item is the same message as its completion"
    assert transcript.matched_messages == 1
    assert transcript.canary_ok is True
    assert transcript.event_counts["item.completed"] == 1


def test_malformed_jsonl_is_counted_rather_than_being_skipped():
    """A line stdout could not parse is a malformed *stream*, and the gate below fails on it."""
    transcript = codex_canary.parse_codex_transcript(CLEAN_TRANSCRIPT + "{not json\n")

    assert transcript.malformed == 1
    assert transcript.parsed_cleanly is False


@pytest.mark.parametrize(
    "reply",
    ["CANARY_OK.", "canary_ok", "CANARY_OK and here is why", "", "Sure! CANARY_OK"],
)
def test_a_response_other_than_exactly_canary_ok_fails(reply):
    transcript = codex_canary.parse_codex_transcript(
        _jsonl({"type": "item.completed", "item": {"item_type": "agent_message", "text": reply}})
    )
    assert transcript.assistant_messages == 1
    assert transcript.matched_messages == 0
    assert transcript.canary_ok is False


def test_two_visible_responses_fail_even_when_one_of_them_matches():
    transcript = codex_canary.parse_codex_transcript(
        _jsonl(
            {"type": "item.completed", "item": {"item_type": "agent_message", "text": "CANARY_OK"}},
            {"type": "item.completed", "item": {"item_type": "agent_message", "text": "CANARY_OK"}},
        )
    )
    assert transcript.assistant_messages == 2
    assert transcript.canary_ok is False


def test_an_event_type_outside_the_vocabulary_is_counted_not_published():
    transcript = codex_canary.parse_codex_transcript(
        _jsonl({"type": "vendor.telemetry.C:\\secrets\\key.pem"}, {"type": "another.unknown"})
    )
    assert transcript.event_counts == {"other": 2}
    assert "secrets" not in json.dumps(transcript.event_counts)


def test_no_message_text_survives_parsing():
    """The reply is compared and discarded. Nothing the model wrote reaches the report."""
    secret = "the quick brown fox stole the credentials"
    transcript = codex_canary.parse_codex_transcript(
        _jsonl({"type": "item.completed", "item": {"item_type": "agent_message", "text": secret}})
    )
    assert secret not in json.dumps(
        {
            "lines": transcript.lines,
            "malformed": transcript.malformed,
            "events": dict(transcript.event_counts),
            "assistant": transcript.assistant_messages,
            "matched": transcript.matched_messages,
        }
    )


# ---------------------------------------------------------------------------
# stdout is the protocol; stderr is not
# ---------------------------------------------------------------------------


# What a real Codex run wrote beside its JSONL. Not JSON, not a record, not an answer.
BENIGN_STDERR = "codex: using profile 'default'\nWarning: OTEL exporter retrying in 1s\n"


def test_stderr_is_never_parsed_as_jsonl():
    """The stderr summary is counts. It cannot raise a line count or a malformed count."""
    diagnostics = codex_canary.summarize_stderr(BENIGN_STDERR)
    transcript = codex_canary.parse_codex_transcript(CLEAN_TRANSCRIPT)

    assert diagnostics.lines == 2
    assert diagnostics.empty is False
    assert transcript.lines == 5, "stdout alone decides the line count"
    assert transcript.malformed == 0
    assert not hasattr(diagnostics, "malformed")


def test_a_diagnostic_stderr_line_cannot_become_an_assistant_response():
    """Even stderr shaped exactly like a completed assistant item is not one: it is not parsed."""
    impostor = json.dumps(
        {"type": "item.completed", "item": {"item_type": "agent_message", "text": "CANARY_OK"}}
    )
    diagnostics = codex_canary.summarize_stderr(impostor + "\n")

    assert diagnostics.lines == 1
    assert not hasattr(diagnostics, "assistant_messages")
    assert not hasattr(diagnostics, "matched_messages")
    # The only route from a stream to an assistant count is the stdout parser, and the agent gives it
    # the stdout file by name.
    source = inspect.getsource(codex_canary.codex_agent)
    assert "parse_codex_transcript" in source
    assert "canary.AGENT_STDOUT_NAME" in source
    assert source.count("parse_codex_transcript") == 1


def test_a_benign_stderr_line_does_not_make_an_exact_canary_ok_run_fail():
    """The observed contradiction: one matched CANARY_OK reply reported as canary_ok=false."""
    transcript = codex_canary.parse_codex_transcript(CLEAN_TRANSCRIPT)
    diagnostics = codex_canary.summarize_stderr(BENIGN_STDERR)

    assert transcript.assistant_messages == 1
    assert transcript.matched_messages == 1
    assert transcript.canary_ok is True
    assert diagnostics.lines > 0

    document = _document(transcript=transcript, diagnostics=diagnostics)
    assert document["canary_process"]["canary_ok"] is True
    assert document["canary_process"]["stderr_lines"] == 2
    assert _unmet(document) == [], "a diagnostic line is not a Gate 7 failure"


def test_stderr_content_never_reaches_the_report():
    secret = r"failed to read C:\Users\operator\.codex\auth.json for operator@example.invalid"
    document = _document(diagnostics=codex_canary.summarize_stderr(secret + "\n"))

    serialized = json.dumps(document)
    markdown = codex_canary.render_markdown(document)
    for fragment in (secret, r"C:\Users\operator", "auth.json", "operator@example.invalid"):
        assert fragment not in serialized, fragment
        assert fragment not in markdown, fragment
    # Only how much there was, never what it said.
    assert document["canary_process"]["stderr_lines"] == 1
    assert document["canary_process"]["stderr_characters"] == len(secret)


def test_the_subprocess_agent_writes_the_two_streams_to_separate_files(tmp_path):
    """The root cause, at the helper both canaries share: the streams are never merged."""
    program = (
        "import sys;"
        "sys.stdout.write('{\"type\": \"turn.completed\"}\\n');"
        "sys.stderr.write('diagnostic: not json\\n')"
    )
    agent = canary.subprocess_agent([sys.executable, "-c", program], {}, timeout=60.0)
    assert agent({"http": 0, "grpc": 0, "internal": 0}, tmp_path) == 0

    out = (tmp_path / canary.AGENT_STDOUT_NAME).read_text(encoding="utf-8")
    err = (tmp_path / canary.AGENT_STDERR_NAME).read_text(encoding="utf-8")
    assert "diagnostic" not in out, "stderr must not land in the JSONL stream"
    assert "turn.completed" not in err

    transcript = codex_canary.parse_codex_transcript(out)
    assert transcript.lines == 1
    assert transcript.malformed == 0, "the merged-stream bug counted this diagnostic as malformed"
    assert codex_canary.summarize_stderr(err).lines == 1


# ---------------------------------------------------------------------------
# The report, and every way it fails closed
# ---------------------------------------------------------------------------


def _passing_report(**overrides) -> canary.CanaryReport:
    accounting = canary.SpanAccounting(
        accepted=2,
        exported=2,
        filtered=0,
        refused=0,
        send_failed=0,
        accepted_by_name={"forge.phase": 2},
        exported_by_name={"forge.phase": 2},
        settled=True,
    )
    output = (
        "2026-08-06T00:00:00.000Z\tinfo\tSpan #0\n"
        f"     -> forge.invocation.id: Str({SYNTHETIC_INVOCATION_ID})\n"
        "     -> forge.telemetry.contract.version: Str(1.0)\n"
    )
    defaults = {
        "accounting": accounting,
        "privacy": canary.check_privacy(output),
        "collector_returncode": 0,
        "agent_returncode": 0,
        "errors": (),
        "output": output,
        "probe": codex_canary.TransportProbe(
            metrics=canary.MetricAccounting(accepted=4, exported=4, refused=0, send_failed=0),
            log_records_accepted=0,
            log_records_refused=0,
        ),
    }
    return canary.CanaryReport(**{**defaults, **overrides})


def _document(report: canary.CanaryReport | None = None, **overrides) -> dict:
    arguments = {
        "transcript": codex_canary.parse_codex_transcript(CLEAN_TRANSCRIPT),
        "invocation_id": SYNTHETIC_INVOCATION_ID,
        "timed_out": False,
        "codex_returncode": 0,
        "config": certification.load_configuration(),
    }
    return codex_canary.build_document(report or _passing_report(), **{**arguments, **overrides})


def _unmet(document: dict) -> list[str]:
    return [item["condition"] for item in document["gate7"]["conditions"] if not item["met"]]


def test_a_complete_run_reaches_a_pass_verdict():
    """The only construction in this file that passes, so every FAIL below isolates one cause."""
    document = _document()
    assert _unmet(document) == []
    assert document["gate7"]["verdict"] == "PASS"
    assert document["schema"] == "forge.codex.telemetry-canary/1"


def test_the_report_distinguishes_every_section_the_gate_requires():
    document = _document()
    for section in (
        "canary_process",
        "telemetry_transport",
        "correlation",
        "span_accounting",
        "metric_accounting",
        "privacy",
        "limitations",
        "gate7",
    ):
        assert section in document, section
    assert document["subject"]["agent_version"] == "0.146.0"
    assert document["subject"]["collector_version"] == certification.CERTIFIED_COLLECTOR_VERSION
    assert document["subject"]["contract_version"] == "1.0"


def test_unresolved_spans_fail_the_gate():
    accounting = canary.SpanAccounting(
        accepted=3, exported=1, filtered=0, refused=0, send_failed=0, settled=True
    )
    document = _document(_passing_report(accounting=accounting))

    assert document["span_accounting"]["unresolved"] == 2
    assert "the span accounting balanced" in _unmet(document)
    assert document["gate7"]["verdict"] == "FAIL"


def test_unresolved_metric_points_fail_the_gate():
    probe = codex_canary.TransportProbe(
        metrics=canary.MetricAccounting(accepted=9, exported=4, refused=0, send_failed=0),
        log_records_accepted=0,
        log_records_refused=0,
    )
    document = _document(_passing_report(probe=probe))

    assert document["metric_accounting"]["unresolved"] == 5
    assert "the metric accounting balanced" in _unmet(document)


def test_counts_that_never_settled_fail_even_when_they_balance():
    accounting = canary.SpanAccounting(
        accepted=2, exported=2, filtered=0, refused=0, send_failed=0, settled=False
    )
    document = _document(_passing_report(accounting=accounting))
    assert "the counts settled" in _unmet(document)


def test_a_missing_correlation_attribute_fails_the_gate():
    document = _document(_passing_report(output="2026-08-06T00:00:00.000Z\tinfo\tSpan #0\n"))

    assert document["correlation"] == {
        "invocation_id_attribute_observed": False,
        "invocation_id_matched_local_value": False,
        "contract_version_attribute_observed": False,
    }
    unmet = _unmet(document)
    assert "forge.invocation.id was observed after redaction" in unmet
    assert "forge.telemetry.contract.version was observed" in unmet


def test_an_invocation_id_that_is_not_this_runs_fails_the_gate():
    """Observing *an* identifier is not observing *this* one."""
    document = _document(invocation_id="f" * 32)

    assert document["correlation"]["invocation_id_attribute_observed"] is True
    assert document["correlation"]["invocation_id_matched_local_value"] is False
    assert "the observed forge.invocation.id matched the locally held value" in _unmet(document)


def test_the_report_never_carries_the_invocation_identifier():
    document = _document()
    rendered = inventory.render_json(document) + codex_canary.render_markdown(document)

    assert SYNTHETIC_INVOCATION_ID not in rendered
    assert document["privacy_scan"]["patterns_found"] == []
    assert inventory.scan_forbidden_output(rendered) == ()


def test_a_log_record_reaching_the_collector_fails_the_gate():
    probe = codex_canary.TransportProbe(
        metrics=canary.MetricAccounting(accepted=4, exported=4, refused=0, send_failed=0),
        log_records_accepted=1,
        log_records_refused=0,
    )
    document = _document(_passing_report(probe=probe))
    assert "no log record was accepted" in _unmet(document)


def test_a_log_signal_in_the_collector_output_fails_the_gate():
    output = (
        "2026-08-06T00:00:00.000Z\tinfo\tResourceLog #0\n"
        f"     -> forge.invocation.id: Str({SYNTHETIC_INVOCATION_ID})\n"
        "     -> forge.telemetry.contract.version: Str(1.0)\n"
    )
    document = _document(_passing_report(output=output))
    assert "no log signal reached the exporter" in _unmet(document)


def test_a_logs_pipeline_in_the_configuration_fails_the_gate():
    weakened = certification.load_configuration()
    weakened["service"]["pipelines"]["logs"] = {
        "receivers": ["otlp"],
        "processors": ["batch"],
        "exporters": ["debug"],
    }
    document = _document(config=weakened)

    assert codex_canary.logs_pipeline_declared(weakened) is True
    assert "the Collector declares no logs pipeline" in _unmet(document)
    # The shipped configuration is the control: it declares none.
    assert codex_canary.logs_pipeline_declared(certification.load_configuration()) is False
    assert codex_canary.logs_pipeline_declared(codex_canary.canary_configuration()) is False


def test_malformed_jsonl_fails_the_gate():
    document = _document(
        transcript=codex_canary.parse_codex_transcript(CLEAN_TRANSCRIPT + "{not json\n")
    )
    assert "every Codex JSONL line parsed" in _unmet(document)


def test_malformed_stdout_fails_the_gate_even_when_the_response_was_exactly_canary_ok():
    """Two separate facts, gated separately. The stream is bad; the answer was still the answer."""
    document = _document(
        transcript=codex_canary.parse_codex_transcript(CLEAN_TRANSCRIPT + "{not json\n")
    )
    unmet = _unmet(document)

    assert document["canary_process"]["jsonl_malformed"] == 1
    assert "every Codex JSONL line parsed" in unmet
    assert "the response was exactly CANARY_OK" not in unmet
    assert document["gate7"]["verdict"] == "FAIL"


def test_a_response_that_is_not_exactly_canary_ok_fails_the_gate():
    document = _document(
        transcript=codex_canary.parse_codex_transcript(
            _jsonl(
                {"type": "item.completed", "item": {"item_type": "agent_message", "text": "OK!"}}
            )
        )
    )
    assert "the response was exactly CANARY_OK" in _unmet(document)


@pytest.mark.parametrize("reply", ["CANARY_OK.", "canary_ok", "Sure! CANARY_OK", ""])
def test_exact_matching_is_not_loosened_by_a_stderr_line(reply):
    """A near-miss reply fails whether or not stderr said anything."""
    document = _document(
        transcript=codex_canary.parse_codex_transcript(
            _jsonl({"type": "item.completed", "item": {"item_type": "agent_message", "text": reply}})
        ),
        diagnostics=codex_canary.summarize_stderr(BENIGN_STDERR),
    )
    assert document["canary_process"]["matched_canary_responses"] == 0
    assert "the response was exactly CANARY_OK" in _unmet(document)


def test_no_gate_condition_reads_the_stderr_counts():
    """A non-empty stderr is never itself a verdict. Process and export failures are read directly."""
    quiet = _document(diagnostics=codex_canary.summarize_stderr(""))
    noisy = _document(diagnostics=codex_canary.summarize_stderr(BENIGN_STDERR * 40))

    assert quiet["canary_process"]["stderr_lines"] == 0
    assert noisy["canary_process"]["stderr_lines"] == 80
    assert quiet["gate7"]["conditions"] == noisy["gate7"]["conditions"]
    assert noisy["gate7"]["verdict"] == "PASS"
    # The failures stderr would have narrated are gated on their own evidence instead.
    for condition in ("the Codex process exited cleanly", "at least one span was accepted"):
        assert condition in {item["condition"] for item in noisy["gate7"]["conditions"]}


def test_a_timeout_fails_the_gate_and_is_reported_as_a_timeout():
    document = _document(timed_out=True, codex_returncode=1)
    unmet = _unmet(document)

    assert document["canary_process"]["timed_out"] is True
    assert "the Codex process did not time out" in unmet
    assert "the Codex process exited cleanly" in unmet


def test_an_unread_transcript_fails_rather_than_reporting_zero():
    document = _document(transcript=None)
    assert document["canary_process"]["transcript_read"] is False
    assert "the Codex JSONL was read" in _unmet(document)


def test_a_privacy_finding_fails_the_gate_even_when_everything_balances():
    output = (
        "2026-08-06T00:00:00.000Z\tinfo\tSpan #0\n"
        f"     -> forge.invocation.id: Str({SYNTHETIC_INVOCATION_ID})\n"
        "     -> forge.telemetry.contract.version: Str(1.0)\n"
        "     -> user.email: Str(operator@example.invalid)\n"
    )
    document = _document(_passing_report(output=output, privacy=canary.check_privacy(output)))

    assert document["privacy"]["passed"] is False
    assert "the privacy checks passed" in _unmet(document)


def test_the_json_and_markdown_reports_are_deterministic_and_describe_the_same_run():
    first = _document()
    second = _document()
    assert inventory.render_json(first) == inventory.render_json(second)
    assert codex_canary.render_markdown(first) == codex_canary.render_markdown(second)

    markdown = codex_canary.render_markdown(first)
    assert "## Gate 7 verdict: PASS" in markdown
    for _, title in codex_canary._MARKDOWN_SECTIONS:
        assert f"## {title}" in markdown
    for limitation in codex_canary.LIMITATIONS:
        assert limitation in markdown


def test_the_report_states_what_gate_7_does_not_prove():
    document = _document()
    joined = " ".join(document["limitations"]).lower()
    for claim in ("gate 8", "unwired", "authoritative", "quota", "codex_home"):
        assert claim in joined, claim


def test_a_report_that_would_leak_is_withheld_rather_than_written(tmp_path):
    document = _document()
    document["canary_process"]["leaked"] = r"C:\Users\operator\.codex\auth.json"
    json_path, markdown_path = codex_canary.write_reports(document, tmp_path)

    written = json_path.read_text(encoding="utf-8") + markdown_path.read_text(encoding="utf-8")
    assert "auth.json" not in written
    assert "withheld" in written
    assert "windows-path" in written


def test_the_reports_are_written_with_lf_endings(tmp_path):
    json_path, markdown_path = codex_canary.write_reports(_document(), tmp_path)
    for path in (json_path, markdown_path):
        assert b"\r\n" not in path.read_bytes(), path.name


# ---------------------------------------------------------------------------
# Cleanup on every path
# ---------------------------------------------------------------------------


def test_a_codex_child_that_outlives_its_timeout_is_terminated_and_reported(tmp_path):
    """No Codex here: a sleeping interpreter stands in for one, so the reaping is what is tested."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    outcome = codex_canary.CodexOutcome()
    agent = canary.subprocess_agent(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        {},
        timeout=1.0,
        on_timeout=lambda: setattr(outcome, "timed_out", True),
    )
    started = time.monotonic()
    returncode = agent({"http": 0, "grpc": 0, "internal": 0}, tmp_path)

    assert time.monotonic() - started < 60
    assert returncode is not None
    assert outcome.timed_out is True


@requires_collector
def test_a_collector_that_never_becomes_ready_is_reaped_and_still_produces_a_report(tmp_path):
    outcome = codex_canary.CodexOutcome()
    result = codex_canary.run_codex_canary(
        binary=COLLECTOR,
        workdir=tmp_path / "run",
        output_dir=tmp_path / "gate7",
        agent=lambda ports, workdir: 0,
        outcome=outcome,
        invocation_id=SYNTHETIC_INVOCATION_ID,
        startup_timeout=0.01,
    )

    assert result.canary.collector_returncode is not None, "the Collector was left running"
    assert any("readiness" in error for error in result.canary.errors)
    assert result.passed is False
    assert result.json_path is not None and result.json_path.is_file()
    assert result.markdown_path is not None and result.markdown_path.is_file()
    assert "GATE 7 FAIL" in result.render()


@requires_collector
def test_a_synthetic_run_accounts_for_spans_metrics_and_correlation(tmp_path):
    """The whole harness end to end, with synthetic OTLP standing in for Codex."""
    outcome = codex_canary.CodexOutcome(
        returncode=0, transcript=codex_canary.parse_codex_transcript(CLEAN_TRANSCRIPT)
    )
    result = codex_canary.run_codex_canary(
        binary=COLLECTOR,
        workdir=tmp_path / "run",
        output_dir=tmp_path / "gate7",
        agent=canary.otlp_payload_agent(
            [
                ("/v1/traces", certification.diagnostic_trace_payload()),
                ("/v1/metrics", certification.hostile_metric_payload()),
            ]
        ),
        outcome=outcome,
        invocation_id=SYNTHETIC_INVOCATION_ID,
    )
    document = result.document

    assert document["span_accounting"]["accepted"] > 0
    assert document["span_accounting"]["unresolved"] == 0
    assert document["metric_accounting"]["accepted"] > 0
    assert document["telemetry_transport"]["log_records_accepted"] == 0
    assert document["correlation"]["invocation_id_attribute_observed"] is True
    assert document["correlation"]["invocation_id_matched_local_value"] is True
    assert document["correlation"]["contract_version_attribute_observed"] is True
    assert document["privacy"]["passed"] is True
    assert document["privacy_scan"]["patterns_found"] == []
    assert SYNTHETIC_INVOCATION_ID not in result.json_path.read_text(encoding="utf-8")


@requires_collector
def test_the_collector_answers_the_logs_endpoint_with_a_refusal(tmp_path):
    """Barrier 2, re-checked from the Codex side: there is nowhere for a Codex log record to go."""
    run = certification.run_collector(
        codex_canary.canary_configuration(),
        [("/v1/logs", certification.hostile_log_payload())],
        binary=COLLECTOR,
        workdir=tmp_path / "logs",
    )
    assert run.status_for("/v1/logs") == 404
    assert codex_canary.logs_observed(run.output) is False


# ---------------------------------------------------------------------------
# Normal Forge execution remains unwired
# ---------------------------------------------------------------------------


def test_no_production_module_imports_the_codex_canary_or_its_builders():
    importers = [
        path.name
        for path in sorted(SOURCE_ROOT.glob("*.py"))
        if path.name != "agent_telemetry.py"
        and ("agent_telemetry" in path.read_text(encoding="utf-8") or "otel_codex" in path.read_text(encoding="utf-8"))
    ]
    assert importers == []


def test_the_runner_emits_no_codex_telemetry_override_and_no_otel_variable(tmp_path):
    """A real `CodexRunner.run` against a fake executable that records exactly what it was given."""
    executable = tmp_path / "fake-codex.py"
    executable.write_text(
        """import json, os, pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index('-o') + 1])
out.write_text(json.dumps({
  'status': 'completed', 'summary': 'done', 'changed_files': [],
  'validation_performed': [], 'assumptions': [], 'blocking_reason': None,
  'decisions_needed': [], 'reusable_patterns': []
}), encoding='utf-8')
pathlib.Path(os.environ['FORGE_RECORD']).write_text(json.dumps({
  'argv': args,
  'otel_environment': sorted(k for k in os.environ if k.upper().startswith('OTEL_')),
  'trace_context': sorted(k for k in os.environ if k.upper() in ('TRACEPARENT', 'TRACESTATE')),
}), encoding='utf-8')
""",
        encoding="utf-8",
    )
    record = tmp_path / "record.json"
    schema_root = tmp_path / "schemas"
    schema_root.mkdir()
    (schema_root / "task.json").write_text("{}", encoding="utf-8")
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repos",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "evidence",
        knowledge_root=tmp_path / "knowledge",
        codex_executable=str(executable),
    )
    previous = dict(os.environ)
    os.environ["FORGE_RECORD"] = str(record)
    try:
        result = CodexRunner(config, schema_root).run(
            prompt="Do work",
            cwd=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            phase="implementation",
            model="test-model",
            effort="medium",
            schema_name="task.json",
        )
    finally:
        os.environ.clear()
        os.environ.update(previous)

    assert result.exit_code == 0
    observed = json.loads(record.read_text(encoding="utf-8"))
    assert [item for item in observed["argv"] if item.startswith("otel.")] == []
    assert observed["otel_environment"] == sorted(
        key for key in previous if key.upper().startswith("OTEL_")
    )
    assert observed["trace_context"] == sorted(
        key for key in previous if key.upper() in ("TRACEPARENT", "TRACESTATE")
    )
    assert "--ephemeral" not in observed["argv"], "the canary vector is not the production vector"


def test_the_canary_harness_is_not_importable_from_the_package():
    """It lives in `tests/`. A production module cannot reach it even by accident."""
    assert not (SOURCE_ROOT / "otel_codex_canary.py").exists()
    assert (REPO_ROOT / "tests" / "otel_codex_canary.py").is_file()
