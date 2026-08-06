"""Structural checks on the local Collector configuration and its PowerShell helpers.

These run without downloading, installing, or launching a Collector. They assert the two
properties that matter for this slice: the pipeline stays on the loopback interface, and it has no
exporter that would send Forge trace data anywhere.
"""

from __future__ import annotations

import ipaddress
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
    # The call operator passes each argument as its own token rather than a parsed command line.
    assert "& $collector --config $ConfigPath" in start
    assert "& $collector validate --config $ConfigPath" in start


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
    assert "validate --config $ConfigPath" in script
    assert "Resolve-OtelCollectorPath" in script


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
