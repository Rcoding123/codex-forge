"""Structural checks on the local Collector configuration and its PowerShell helpers.

These run without downloading, installing, or launching a Collector. They assert the properties
that matter for this slice: the pipeline stays on the loopback interface, it has no exporter that
would send Forge telemetry anywhere, and the mandatory privacy processor of
docs/AGENT-TELEMETRY-CONTRACT.md covers every surface on every pipeline before batching.

The end-to-end proof that redaction actually happens lives in
tests/test_otel_redaction_certification.py, which runs a real Collector. These tests are the part
that stays active on a machine that has no Collector installed.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

import otel_certification as certification  # noqa: E402 - needs the yaml import guard above

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_ROOT = REPO_ROOT / "ops" / "otel"
CONFIG_PATH = OPS_ROOT / "collector-config.yaml"
START_SCRIPT = OPS_ROOT / "Start-OtelCollector.ps1"
RESOLVE_SCRIPT = OPS_ROOT / "Resolve-OtelCollector.ps1"

PRIVACY = certification.PRIVACY_PROCESSOR
FILTER = certification.UNREDACTABLE_FILTER


@pytest.fixture(scope="module")
def collector_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def mutable_config(collector_config) -> dict:
    """A throwaway copy for the fail-closed tests to damage."""
    return copy.deepcopy(collector_config)


def _powershell_code(path: Path) -> str:
    """Return a script with its comment-based help block and `#` comments removed.

    The absence checks below assert what a script *does*, not what its documentation mentions:
    the help text legitimately explains that the helper does not download a Collector or touch
    forge.toml.
    """
    text = path.read_text(encoding="utf-8")
    while "<#" in text and "#>" in text:
        start = text.index("<#")
        text = text[:start] + text[text.index("#>", start) + 2 :]
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _is_loopback(endpoint: str) -> bool:
    host = endpoint.rsplit(":", 1)[0].strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def test_collector_config_is_valid_yaml_with_the_expected_sections(collector_config):
    assert set(collector_config) == {"receivers", "processors", "exporters", "service"}


def test_otlp_grpc_listens_on_loopback_4317(collector_config):
    grpc = collector_config["receivers"]["otlp"]["protocols"]["grpc"]["endpoint"]
    assert grpc == "127.0.0.1:4317"
    assert _is_loopback(grpc)


def test_otlp_http_is_offered_on_loopback_4318(collector_config):
    http = collector_config["receivers"]["otlp"]["protocols"]["http"]["endpoint"]
    assert http == "127.0.0.1:4318"
    assert _is_loopback(http)


def test_every_listening_endpoint_is_loopback(collector_config):
    """No receiver or internal telemetry endpoint may be reachable from the network."""
    endpoints: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "endpoint" and isinstance(value, str):
                    endpoints.append(value)
                elif key == "host" and isinstance(value, str):
                    endpoints.append(f"{value}:0")
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(collector_config)
    assert endpoints, "no endpoints found to check"
    for endpoint in endpoints:
        assert _is_loopback(endpoint), f"{endpoint} is not loopback-bound"
    # Re-serialized so the check reads the resolved configuration, not the explanatory comments.
    assert "0.0.0.0" not in yaml.safe_dump(collector_config)


def test_memory_limiter_and_batch_processors_are_configured(collector_config):
    processors = collector_config["processors"]
    assert "memory_limiter" in processors
    assert "batch" in processors
    assert processors["memory_limiter"]["limit_mib"] > 0


def test_debug_is_the_only_exporter(collector_config):
    """Any other exporter would be network egress for Forge telemetry."""
    assert set(collector_config["exporters"]) == {"debug"}
    assert collector_config["exporters"]["debug"]["verbosity"] == "detailed"


@pytest.mark.parametrize("signal", ["traces", "metrics"])
def test_each_pipeline_limits_then_redacts_then_batches_then_prints(collector_config, signal):
    pipeline = collector_config["service"]["pipelines"][signal]
    assert pipeline["receivers"] == ["otlp"]
    # memory_limiter sheds load first; carriers that cannot be redacted are refused; the privacy
    # processor runs before anything is retained by batch or seen by an exporter.
    assert pipeline["processors"] == ["memory_limiter", FILTER, PRIVACY, "batch"]
    assert pipeline["exporters"] == ["debug"]


def test_no_pipeline_references_an_undeclared_component(collector_config):
    for pipeline in collector_config["service"]["pipelines"].values():
        for section in ("receivers", "processors", "exporters"):
            for name in pipeline.get(section, []):
                assert name in collector_config[section], f"{name} is used but not declared"


def test_traces_and_metrics_are_the_only_pipelines(collector_config):
    """Metrics are received deliberately; logs are the signal that must never have a pipeline."""
    assert set(collector_config["service"]["pipelines"]) == {"traces", "metrics"}


# ---------------------------------------------------------------------------
# The mandatory privacy processor (contract section 6).
# ---------------------------------------------------------------------------


def test_the_shipped_configuration_passes_certification(collector_config):
    assert certification.certify_configuration(collector_config) == []


def test_the_privacy_processor_fails_closed_on_a_statement_error(collector_config):
    """`propagate` drops the batch. `ignore`/`silent` would export it unredacted."""
    assert collector_config["processors"][PRIVACY]["error_mode"] == "propagate"


@pytest.mark.parametrize(
    ("signal", "surface"),
    [("traces", s) for s in certification.REQUIRED_TRACE_SURFACES]
    + [("metrics", s) for s in certification.REQUIRED_METRIC_SURFACES],
)
def test_every_surface_is_covered_on_both_signals(collector_config, signal, surface):
    """A `resource` context under `trace_statements` does not reach metric resources.

    This is the trap contract section 6 warns about: one processor type does not automatically
    cover all four surfaces, so both signals must name `resource` for themselves.
    """
    policies = certification.privacy_policies(collector_config)
    policy = policies.get((signal, surface))
    assert policy is not None, f"{signal} has no statements for the {surface} surface"
    assert policy.allowed_keys, f"{signal}/{surface} has no keep_keys allowlist"


@pytest.mark.parametrize("key", certification.FORBIDDEN_ATTRIBUTE_KEYS)
def test_every_forbidden_key_is_deleted_on_every_surface(collector_config, key):
    for (signal, surface), policy in certification.privacy_policies(collector_config).items():
        assert policy.deletes(key), f"{signal}/{surface} does not delete {key}"
        assert key not in (policy.allowed_keys or set()), f"{signal}/{surface} allows {key}"


@pytest.mark.parametrize("key", certification.REQUIRED_ALLOWED_ATTRIBUTE_KEYS)
def test_every_bounded_operational_field_is_preserved(collector_config, key):
    """Redaction must not quietly break the certified Forge span contract."""
    for (signal, surface), policy in certification.privacy_policies(collector_config).items():
        assert key in policy.allowed_keys, f"{signal}/{surface} drops {key}"
        assert not policy.deletes(key), f"{signal}/{surface} deletes {key}"


def test_the_allowlist_admits_nothing_beyond_the_approved_vocabulary(collector_config):
    """An unknown attribute is denied by default, not exported because nobody forbade it."""
    approved = set(certification.REQUIRED_ALLOWED_ATTRIBUTE_KEYS)
    for (signal, surface), policy in certification.privacy_policies(collector_config).items():
        extra = sorted(policy.allowed_keys - approved)
        assert extra == [], f"{signal}/{surface} allows unreviewed keys: {extra}"


def test_no_surface_uses_a_deprecated_unqualified_ottl_path(collector_config):
    """0.158.0 still accepts `attributes` but logs "please rewrite them accordingly"."""
    for (signal, surface), policy in certification.privacy_policies(collector_config).items():
        assert policy.unqualified_paths == 0, f"{signal}/{surface} uses an unqualified path"


# ---------------------------------------------------------------------------
# Fail-closed: each of these mutations must be rejected by certification.
#
# Without these, a later edit could quietly delete a protection and every other test here would
# still pass, because every other test describes the configuration as it is.
# ---------------------------------------------------------------------------


def _pattern_alternative(key: str) -> str:
    """How `key` appears inside a `delete_matching_keys` alternation in the shipped file."""
    return key.replace(".", "[.]")


def _drop_from_deletion_set(config: dict, key: str) -> None:
    alternative = _pattern_alternative(key)
    processor = config["processors"][PRIVACY]
    for block in ("trace_statements", "metric_statements"):
        for group in processor[block]:
            group["statements"] = [
                statement.replace(f"|{alternative}", "").replace(f"({alternative}|", "(")
                for statement in group["statements"]
            ]


def test_removing_the_privacy_processor_fails_certification(mutable_config):
    del mutable_config["processors"][PRIVACY]
    for pipeline in mutable_config["service"]["pipelines"].values():
        pipeline["processors"] = [name for name in pipeline["processors"] if name != PRIVACY]

    problems = certification.certify_configuration(mutable_config)

    assert any("privacy processor" in problem and "missing" in problem for problem in problems)


@pytest.mark.parametrize("signal", ["traces", "metrics"])
def test_ordering_the_privacy_processor_after_batch_fails_certification(mutable_config, signal):
    """Redacting after batching is not the same guarantee, even with the right keys."""
    processors = mutable_config["service"]["pipelines"][signal]["processors"]
    processors.remove(PRIVACY)
    processors.append(PRIVACY)

    problems = certification.certify_configuration(mutable_config)

    assert f"{signal}: privacy processor runs after batch" in problems


@pytest.mark.parametrize("signal", ["traces", "metrics"])
def test_dropping_the_privacy_processor_from_one_pipeline_fails_certification(mutable_config, signal):
    pipeline = mutable_config["service"]["pipelines"][signal]
    pipeline["processors"] = [name for name in pipeline["processors"] if name != PRIVACY]

    problems = certification.certify_configuration(mutable_config)

    assert f"{signal}: privacy processor {PRIVACY} is missing" in problems


def test_adding_a_logs_pipeline_fails_certification(mutable_config):
    mutable_config["service"]["pipelines"]["logs"] = {
        "receivers": ["otlp"],
        "processors": ["memory_limiter", PRIVACY, "batch"],
        "exporters": ["debug"],
    }

    problems = certification.certify_configuration(mutable_config)

    assert "forbidden pipeline present: logs" in problems


def test_removing_the_metrics_pipeline_fails_certification(mutable_config):
    del mutable_config["service"]["pipelines"]["metrics"]

    problems = certification.certify_configuration(mutable_config)

    assert "required pipeline missing: metrics" in problems


def test_adding_an_external_exporter_fails_certification(mutable_config):
    mutable_config["exporters"]["otlphttp"] = {"endpoint": "https://telemetry.example.invalid"}
    mutable_config["service"]["pipelines"]["traces"]["exporters"] = ["debug", "otlphttp"]

    problems = certification.certify_configuration(mutable_config)

    assert "exporter other than debug: ['otlphttp']" in problems
    assert "traces: exporter other than debug: otlphttp" in problems


def test_an_unused_external_exporter_still_fails_certification(mutable_config):
    """Declared but unwired today is one edit away from wired tomorrow."""
    mutable_config["exporters"]["otlp/remote"] = {"endpoint": "collector.example.invalid:4317"}

    assert "exporter other than debug: ['otlp/remote']" in certification.certify_configuration(mutable_config)


@pytest.mark.parametrize("endpoint", ["0.0.0.0:4317", "10.11.12.13:4317", "[::]:4317"])
def test_a_non_loopback_receiver_fails_certification(mutable_config, endpoint):
    mutable_config["receivers"]["otlp"]["protocols"]["grpc"]["endpoint"] = endpoint

    problems = certification.certify_configuration(mutable_config)

    assert f"non-loopback endpoint: {endpoint}" in problems


def test_a_non_loopback_internal_metrics_host_fails_certification(mutable_config):
    reader = mutable_config["service"]["telemetry"]["metrics"]["readers"][0]
    reader["pull"]["exporter"]["prometheus"]["host"] = "0.0.0.0"

    assert "non-loopback endpoint: 0.0.0.0:0" in certification.certify_configuration(mutable_config)


@pytest.mark.parametrize("key", certification.FORBIDDEN_ATTRIBUTE_KEYS)
def test_removing_one_forbidden_field_from_the_protection_set_fails_certification(mutable_config, key):
    """Every entry in the minimum deletion set is load-bearing, individually."""
    _drop_from_deletion_set(mutable_config, key)

    problems = certification.certify_configuration(mutable_config)

    assert any(problem.endswith(f"forbidden key not deleted: {key}") for problem in problems)


def test_widening_the_allowlist_to_admit_a_forbidden_key_fails_certification(mutable_config):
    group = mutable_config["processors"][PRIVACY]["trace_statements"][2]
    group["statements"][-1] = group["statements"][-1].replace(
        '"error.code"])', '"error.code", "user.email"])'
    )

    problems = certification.certify_configuration(mutable_config)

    assert "traces/span: allowlist admits forbidden keys: ['user.email']" in problems


def test_narrowing_the_allowlist_below_the_forge_span_contract_fails_certification(mutable_config):
    group = mutable_config["processors"][PRIVACY]["trace_statements"][2]
    group["statements"][-1] = group["statements"][-1].replace('"forge.invocation.id", ', "")

    problems = certification.certify_configuration(mutable_config)

    assert any("drops required safe keys" in problem and "forge.invocation.id" in problem for problem in problems)


@pytest.mark.parametrize(
    ("block", "index"),
    [("trace_statements", 0), ("trace_statements", 2), ("trace_statements", 3), ("metric_statements", 0), ("metric_statements", 2)],
)
def test_removing_one_surface_fails_certification(mutable_config, block, index):
    del mutable_config["processors"][PRIVACY][block][index]

    assert certification.certify_configuration(mutable_config) != []


def test_weakening_the_error_mode_fails_certification(mutable_config):
    """`ignore` would let a statement failure export the record unredacted."""
    mutable_config["processors"][PRIVACY]["error_mode"] = "ignore"

    problems = certification.certify_configuration(mutable_config)

    assert any("error_mode must be 'propagate'" in problem for problem in problems)


def test_dropping_the_memory_limiter_ahead_of_redaction_fails_certification(mutable_config):
    processors = mutable_config["service"]["pipelines"]["traces"]["processors"]
    processors.remove("memory_limiter")

    assert "traces: memory_limiter is missing" in certification.certify_configuration(mutable_config)


# ---------------------------------------------------------------------------
# Carriers that no 0.158.0 processor can redact are refused instead.
# ---------------------------------------------------------------------------


def test_the_unredactable_carrier_filter_runs_on_both_pipelines(collector_config):
    for signal in ("traces", "metrics"):
        processors = collector_config["service"]["pipelines"][signal]["processors"]
        assert FILTER in processors
        assert processors.index(FILTER) < processors.index("batch")


@pytest.mark.parametrize(
    ("signal", "surface", "condition"),
    [(signal, surface, condition) for (signal, surface), condition in certification.UNREDACTABLE_CONDITIONS.items()],
)
def test_link_and_exemplar_carriers_are_dropped(collector_config, signal, surface, condition):
    """OTTL exposes no `spanlink` or `exemplar` context, so the carrier itself is denied."""
    assert condition in collector_config["processors"][FILTER][signal][surface]


@pytest.mark.parametrize("signal", ["traces", "metrics"])
def test_dropping_the_unredactable_filter_from_a_pipeline_fails_certification(mutable_config, signal):
    pipeline = mutable_config["service"]["pipelines"][signal]
    pipeline["processors"] = [name for name in pipeline["processors"] if name != FILTER]

    assert f"{signal}: {FILTER} is missing" in certification.certify_configuration(mutable_config)


@pytest.mark.parametrize(("signal", "surface"), sorted(certification.UNREDACTABLE_CONDITIONS))
def test_removing_a_carrier_condition_fails_certification(mutable_config, signal, surface):
    mutable_config["processors"][FILTER][signal][surface] = []

    problems = certification.certify_configuration(mutable_config)

    assert any(f"{signal}/{surface} no longer drops" in problem for problem in problems)


def test_weakening_the_carrier_filter_error_mode_fails_certification(mutable_config):
    mutable_config["processors"][FILTER]["error_mode"] = "ignore"

    problems = certification.certify_configuration(mutable_config)

    assert f"{FILTER}: error_mode must be 'propagate'" in problems


def test_helper_resolves_the_binary_without_downloading_it():
    script = _powershell_code(RESOLVE_SCRIPT)
    assert "-CollectorPath" in script
    assert "$env:OTELCOL_PATH" in script
    assert "Get-Command otelcol-contrib" in script
    for forbidden in ("Invoke-WebRequest", "Invoke-RestMethod", "Start-BitsTransfer", "curl", "wget"):
        assert forbidden not in script, f"{forbidden} would download a Collector"


def test_helper_requires_a_native_executable():
    """PATHEXT lets Get-Command resolve .bat/.cmd, which run as scripts rather than a Collector."""
    script = _powershell_code(RESOLVE_SCRIPT)
    assert "Assert-NativeExecutable" in script
    assert '.ToLowerInvariant() -ne ".exe"' in script
    # Applied to all three resolution sources, not just the argument.
    assert script.count("Assert-NativeExecutable -Path") == 3
    assert '-Source "-CollectorPath"' in script
    assert '-Source "OTELCOL_PATH"' in script


def test_helper_uses_literal_paths_and_no_shell_string_building():
    """Paths with spaces must survive, and no argument may be concatenated into a command string."""
    for path in (START_SCRIPT, RESOLVE_SCRIPT):
        script = _powershell_code(path)
        assert "Invoke-Expression" not in script
        assert "cmd /c" not in script and "cmd.exe" not in script
        assert "-LiteralPath" in script or path is START_SCRIPT
    start = _powershell_code(START_SCRIPT)
    # The call operator splats an argument array, so each element crosses as its own token rather
    # than being re-parsed out of a command line.
    assert '$collectorArguments = @("validate", "--config", $ConfigPath)' in start
    assert '$collectorArguments = @("--config", $ConfigPath)' in start
    assert start.count("& $collector @collectorArguments") == 2


def test_start_script_propagates_the_collector_exit_code():
    script = _powershell_code(START_SCRIPT)
    assert script.count("exit $LASTEXITCODE") == 2


def test_helper_fails_clearly_when_the_collector_is_absent():
    script = RESOLVE_SCRIPT.read_text(encoding="utf-8")
    assert "otelcol-contrib.exe was not found" in script
    assert "Codex Forge does not download it" in script


def test_start_script_uses_the_repository_config_and_supports_validation():
    script = START_SCRIPT.read_text(encoding="utf-8")
    assert 'Join-Path $ScriptRoot "collector-config.yaml"' in script
    assert "[switch]$ValidateOnly" in script
    assert '"validate", "--config", $ConfigPath' in script
    assert 'Join-Path $ScriptRoot "Resolve-OtelCollector.ps1"' in script


def test_start_script_invokes_the_resolver_instead_of_dot_sourcing_it():
    """Regression guard for the resolution defect this file's behavioural tests cover.

    Dot-sourcing Resolve-OtelCollector.ps1 runs its `param([string]$CollectorPath)` block in
    Start-OtelCollector.ps1's own scope. The dot-source passes no arguments, so $CollectorPath is
    rebound to an empty string and the operator's explicit path is destroyed before the resolver
    ever sees it. A child invocation cannot reach the caller's variables.
    """
    script = _powershell_code(START_SCRIPT)
    assert ". (Join-Path $ScriptRoot" not in script
    assert ". $ResolverScript" not in script
    assert "$collector = & $ResolverScript -CollectorPath $CollectorPath" in script


def test_start_script_installs_no_service_or_scheduled_task():
    script = _powershell_code(START_SCRIPT)
    for forbidden in (
        "New-Service",
        "Register-ScheduledTask",
        "schtasks",
        "Set-Service",
        "Invoke-WebRequest",
        "Start-BitsTransfer",
    ):
        assert forbidden not in script, f"{forbidden} is outside the scope of this helper"


def test_helpers_do_not_touch_forge_state():
    for path in (START_SCRIPT, RESOLVE_SCRIPT):
        script = _powershell_code(path)
        for forbidden in ("forge.toml", "forge.db", "CodexData", "Remove-Item", "Set-Content"):
            assert forbidden not in script, f"{path.name} must not reference {forbidden}"


# ---------------------------------------------------------------------------
# Behavioural tests: these actually run the helpers.
#
# None of them need a real Collector. Resolution is proved by what the resolver prints, and
# argument forwarding is proved by a stand-in executable that records its own argv and exits with a
# chosen code. The stand-in is a copy of this interpreter, so it is a genuine PE and exercises the
# native-executable check for real rather than through a mock.
# ---------------------------------------------------------------------------

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
SYSTEM_ROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
SYSTEM32 = SYSTEM_ROOT / "System32"
# Any self-contained native binary works; this one is never executed, only resolved.
NATIVE_EXE_SOURCE = SYSTEM32 / "where.exe"
# The stand-in Collector is a copy of the base interpreter; a venv's python.exe is only a stub.
BASE_PYTHON = Path(getattr(sys, "_base_executable", None) or sys.executable)

requires_windows_powershell = pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL or not NATIVE_EXE_SOURCE.is_file(),
    reason="the Collector helpers are Windows PowerShell scripts",
)

# Imported during interpreter start-up, before the stand-in would complain that "validate" is not a
# Python script, so it can record argv and exit with the requested code.
_RECORDER = """\
import json
import os
import sys

_target = os.environ.get("FAKE_OTELCOL_ARGV")
if _target:
    with open(_target, "w", encoding="utf-8") as handle:
        json.dump(sys.argv, handle)
    os._exit(int(os.environ.get("FAKE_OTELCOL_EXIT", "0")))
"""


def _native_exe(directory: Path, name: str = "otelcol-contrib.exe") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    shutil.copyfile(NATIVE_EXE_SOURCE, target)
    return target


def _recording_collector(directory: Path, exit_code: int = 0) -> tuple[Path, Path, dict[str, str]]:
    """Return (executable, argv_file, environment) for a stand-in that records how it was called."""
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / "otelcol-contrib.exe"
    shutil.copyfile(BASE_PYTHON, executable)
    support = directory / "recorder"
    support.mkdir(exist_ok=True)
    (support / "sitecustomize.py").write_text(_RECORDER, encoding="utf-8")
    argv_file = directory / "argv.json"
    environment = {
        "PYTHONPATH": str(support),
        # The copy lives outside the installation, so point it back at the real one.
        "PYTHONHOME": sys.base_prefix,
        "FAKE_OTELCOL_ARGV": str(argv_file),
        "FAKE_OTELCOL_EXIT": str(exit_code),
    }
    return executable, argv_file, environment


def _run(
    script: Path,
    *args: str,
    otelcol_path: Path | str | None = None,
    path_dirs: tuple[Path, ...] = (),
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a helper with PATH and OTELCOL_PATH under the test's control."""
    env = {k: v for k, v in os.environ.items() if k not in {"OTELCOL_PATH", "PYTHONPATH", "PYTHONHOME"}}
    # A curated PATH keeps a Collector that happens to be installed on this machine out of the way.
    # The interpreter directories carry pythonNNN.dll, which the copied stand-in loads by name;
    # neither holds an otelcol-contrib, so they cannot satisfy a resolution under test.
    env["PATH"] = os.pathsep.join(
        [
            *(str(d) for d in path_dirs),
            str(SYSTEM32),
            str(SYSTEM_ROOT),
            str(BASE_PYTHON.parent),
            sys.base_prefix,
        ]
    )
    if otelcol_path is not None:
        env["OTELCOL_PATH"] = str(otelcol_path)
    env.update(environment or {})
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def _resolved(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


@requires_windows_powershell
def test_start_script_forwards_an_explicit_collector_path(tmp_path):
    """The defect: the explicit path was dropped and resolution fell through to OTELCOL_PATH/PATH."""
    collector, argv_file, environment = _recording_collector(tmp_path / "explicit")
    decoy_env = _native_exe(tmp_path / "decoy-env")
    decoy_path_dir = tmp_path / "decoy-path"
    _native_exe(decoy_path_dir)

    result = _run(
        START_SCRIPT,
        "-CollectorPath",
        str(collector),
        "-ValidateOnly",
        otelcol_path=decoy_env,
        path_dirs=(decoy_path_dir,),
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "was not found" not in result.stderr
    # Only the explicitly named stand-in records argv; neither decoy can.
    assert json.loads(argv_file.read_text(encoding="utf-8"))[0] == "validate"


@requires_windows_powershell
def test_resolver_accepts_an_absolute_native_executable_path(tmp_path):
    collector = _native_exe(tmp_path / "tools")

    result = _run(RESOLVE_SCRIPT, "-CollectorPath", str(collector))

    assert _same_path(_resolved(result), collector)


@requires_windows_powershell
def test_a_collector_path_containing_spaces_is_passed_through_unchanged(tmp_path):
    directory = tmp_path / "Program Files" / "otel collector"
    collector, argv_file, environment = _recording_collector(directory)

    resolved = _run(RESOLVE_SCRIPT, "-CollectorPath", str(collector))
    assert _same_path(_resolved(resolved), collector)

    started = _run(START_SCRIPT, "-CollectorPath", str(collector), "-ValidateOnly", environment=environment)
    assert started.returncode == 0, started.stderr
    # The whole path crossed as one argument; a split would have resolved to something else or failed.
    assert json.loads(argv_file.read_text(encoding="utf-8"))[0] == "validate"


@requires_windows_powershell
def test_explicit_collector_path_wins_over_otelcol_path_and_path(tmp_path):
    explicit = _native_exe(tmp_path / "explicit", "explicit-collector.exe")
    from_environment = _native_exe(tmp_path / "environment")
    path_dir = tmp_path / "on-path"
    _native_exe(path_dir)

    result = _run(
        RESOLVE_SCRIPT,
        "-CollectorPath",
        str(explicit),
        otelcol_path=from_environment,
        path_dirs=(path_dir,),
    )

    assert _same_path(_resolved(result), explicit)


@requires_windows_powershell
def test_otelcol_path_is_used_when_no_explicit_path_is_supplied(tmp_path):
    from_environment = _native_exe(tmp_path / "environment")
    path_dir = tmp_path / "on-path"
    _native_exe(path_dir)

    result = _run(RESOLVE_SCRIPT, otelcol_path=from_environment, path_dirs=(path_dir,))

    assert _same_path(_resolved(result), from_environment)


@requires_windows_powershell
def test_path_is_used_when_neither_explicit_path_nor_otelcol_path_is_set(tmp_path):
    path_dir = tmp_path / "on-path"
    on_path = _native_exe(path_dir)

    result = _run(RESOLVE_SCRIPT, path_dirs=(path_dir,))

    assert _same_path(_resolved(result), on_path)


@requires_windows_powershell
def test_a_cmd_file_named_otelcol_contrib_is_rejected(tmp_path):
    """PATHEXT makes otelcol-contrib.cmd an Application, but it runs as a shell script."""
    path_dir = tmp_path / "on-path"
    path_dir.mkdir(parents=True)
    script = path_dir / "otelcol-contrib.cmd"
    script.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")

    from_path = _run(RESOLVE_SCRIPT, path_dirs=(path_dir,))
    assert from_path.returncode != 0
    assert "must point at a native .exe" in from_path.stderr

    explicit = _run(RESOLVE_SCRIPT, "-CollectorPath", str(script))
    assert explicit.returncode != 0
    assert "must point at a native .exe" in explicit.stderr


@requires_windows_powershell
def test_missing_directory_and_non_native_paths_fail_clearly(tmp_path):
    missing = _run(RESOLVE_SCRIPT, "-CollectorPath", str(tmp_path / "absent" / "otelcol-contrib.exe"))
    assert missing.returncode != 0
    assert "Collector not found at the path supplied with -CollectorPath" in missing.stderr

    directory = tmp_path / "otelcol-contrib.exe"
    directory.mkdir()
    as_directory = _run(RESOLVE_SCRIPT, "-CollectorPath", str(directory))
    assert as_directory.returncode != 0
    assert "Collector not found at the path supplied with -CollectorPath" in as_directory.stderr

    renamed = tmp_path / "renamed" / "otelcol-contrib.exe"
    renamed.parent.mkdir()
    renamed.write_text("#!/bin/sh\necho not a collector\n", encoding="ascii")
    not_native = _run(RESOLVE_SCRIPT, "-CollectorPath", str(renamed))
    assert not_native.returncode != 0
    assert "not a native Windows executable" in not_native.stderr

    unset = _run(RESOLVE_SCRIPT, otelcol_path=tmp_path / "absent.exe")
    assert unset.returncode != 0
    assert "OTELCOL_PATH is set but does not point at a file" in unset.stderr


@requires_windows_powershell
def test_multiple_collectors_on_path_are_rejected_as_ambiguous(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _native_exe(first)
    _native_exe(second)

    result = _run(RESOLVE_SCRIPT, path_dirs=(first, second))

    assert result.returncode != 0
    assert "ambiguous on PATH" in result.stderr


@requires_windows_powershell
def test_validate_only_invokes_the_resolved_executable_with_the_repository_yaml(tmp_path):
    collector, argv_file, environment = _recording_collector(tmp_path / "validate")

    result = _run(START_SCRIPT, "-CollectorPath", str(collector), "-ValidateOnly", environment=environment)

    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[:2] == ["validate", "--config"]
    assert _same_path(argv[2], CONFIG_PATH)
    assert len(argv) == 3


@requires_windows_powershell
def test_a_nonzero_validation_exit_code_is_propagated(tmp_path):
    collector, _, environment = _recording_collector(tmp_path / "failing", exit_code=3)

    result = _run(START_SCRIPT, "-CollectorPath", str(collector), "-ValidateOnly", environment=environment)

    assert result.returncode == 3
