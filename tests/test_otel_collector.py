"""Structural checks on the local Collector configuration and its PowerShell helpers.

These run without downloading, installing, or launching a Collector. They assert the two
properties that matter for this slice: the pipeline stays on the loopback interface, and it has no
exporter that would send Forge trace data anywhere.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_ROOT = REPO_ROOT / "ops" / "otel"
CONFIG_PATH = OPS_ROOT / "collector-config.yaml"
START_SCRIPT = OPS_ROOT / "Start-OtelCollector.ps1"
RESOLVE_SCRIPT = OPS_ROOT / "Resolve-OtelCollector.ps1"


@pytest.fixture(scope="module")
def collector_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


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
    """Any other exporter would be network egress for Forge trace data."""
    assert set(collector_config["exporters"]) == {"debug"}
    assert collector_config["exporters"]["debug"]["verbosity"] == "detailed"


def test_traces_pipeline_limits_then_batches_then_prints(collector_config):
    pipeline = collector_config["service"]["pipelines"]["traces"]
    assert pipeline["receivers"] == ["otlp"]
    # memory_limiter must precede batch so it sheds load before spans accumulate.
    assert pipeline["processors"] == ["memory_limiter", "batch"]
    assert pipeline["exporters"] == ["debug"]


def test_no_pipeline_references_an_undeclared_component(collector_config):
    for pipeline in collector_config["service"]["pipelines"].values():
        for section in ("receivers", "processors", "exporters"):
            for name in pipeline.get(section, []):
                assert name in collector_config[section], f"{name} is used but not declared"


def test_traces_are_the_only_pipeline(collector_config):
    assert set(collector_config["service"]["pipelines"]) == {"traces"}


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
