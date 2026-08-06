"""Proof that Forge works on an installation without the `telemetry` extra.

This module must never import `opentelemetry`, and must never call `pytest.importorskip`. Its whole
purpose is to run on a machine where the tracing packages are absent, so anything that would skip
it there defeats it. `tests/test_telemetry.py` covers the behavior that genuinely needs the SDK and
is skipped when it is missing; this file covers what must hold regardless.

Absence is simulated two ways: in-process, by blocking the import; and out-of-process, by running a
child interpreter with the packages hidden, which also proves nothing imports them eagerly.
"""

from __future__ import annotations

import builtins
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from codex_forge.config import TelemetryConfig, load_config
from codex_forge.telemetry import (
    OUTCOME_COMPLETED,
    OUTCOME_OK,
    PHASE_IMPLEMENTATION,
    ROLE_IMPLEMENTER,
    Telemetry,
    build_telemetry,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Blocks the tracing packages for a child interpreter without uninstalling anything.
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


def run_child(body: str) -> subprocess.CompletedProcess:
    """Run a snippet in a child interpreter with the tracing packages hidden."""
    return subprocess.run(
        [sys.executable, "-c", BLOCK_OTEL + textwrap.dedent(body)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def without_opentelemetry(monkeypatch):
    """Make every `opentelemetry` import raise, in-process."""
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    for name in [key for key in sys.modules if key.startswith("opentelemetry")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    return blocked


# ------------------------------------------------------------------------------------------------
# Import and configuration without the extra
# ------------------------------------------------------------------------------------------------


def test_forge_imports_without_the_telemetry_extra():
    result = run_child(
        """
        import codex_forge.cli, codex_forge.orchestrator, codex_forge.telemetry, codex_forge.config
        print("IMPORTED_OK")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORTED_OK" in result.stdout


def test_no_module_imports_opentelemetry_eagerly():
    """Importing Forge must not pull in the SDK, exporter, or grpc — even when they are installed."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import codex_forge.cli, codex_forge.orchestrator, codex_forge.telemetry;"
            " print(sorted(m for m in sys.modules if 'opentelemetry' in m or m == 'grpc'))",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", f"eagerly imported: {result.stdout.strip()}"


def test_existing_configurations_load_without_the_extra(tmp_path: Path):
    config = tmp_path / "forge.toml"
    config.write_text(
        '[paths]\ndata_root = "data"\n\n[codex]\napproval_policy = "never"\n', encoding="utf-8"
    )
    result = run_child(
        f"""
        from codex_forge.config import load_config
        c = load_config(r"{config}")
        assert c.telemetry.enabled is False
        assert c.max_task_tokens == 2000000
        print("CONFIG_OK")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "CONFIG_OK" in result.stdout


def test_enabled_configuration_still_loads_without_the_extra(tmp_path: Path):
    """Enabling telemetry without installing the extra must be a no-op, not a configuration error."""
    config = tmp_path / "forge.toml"
    config.write_text(
        '[paths]\ndata_root = "data"\n\n[codex]\napproval_policy = "never"\n\n'
        "[telemetry]\nenabled = true\n",
        encoding="utf-8",
    )
    result = run_child(
        f"""
        from codex_forge.config import load_config
        from codex_forge.telemetry import build_telemetry
        t = build_telemetry(load_config(r"{config}").telemetry)
        assert t.enabled is True
        assert t.active is False
        assert t.warnings == ("telemetry init unavailable (ModuleNotFoundError)",)
        print("DEGRADED_OK")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "DEGRADED_OK" in result.stdout


def test_full_span_tree_runs_without_the_extra():
    """Every span helper must be usable and inert when the SDK cannot be imported."""
    result = run_child(
        """
        from codex_forge.config import TelemetryConfig
        from codex_forge.telemetry import (
            OUTCOME_COMPLETED, OUTCOME_OK, PHASE_IMPLEMENTATION, ROLE_IMPLEMENTER, build_telemetry,
        )
        t = build_telemetry(TelemetryConfig(enabled=True))
        with t.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
            with t.attempt_span(attempt_number=1) as attempt:
                with t.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
                    phase.set_outcome(OUTCOME_OK)
                attempt.set_outcome(OUTCOME_COMPLETED)
            task.set_outcome(OUTCOME_COMPLETED)
        t.flush()
        t.shutdown()
        print("SPANS_OK")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "SPANS_OK" in result.stdout


# ------------------------------------------------------------------------------------------------
# In-process equivalents
# ------------------------------------------------------------------------------------------------


def test_disabled_telemetry_needs_nothing_installed(without_opentelemetry):
    telemetry = build_telemetry(TelemetryConfig(enabled=False))
    assert telemetry.enabled is False
    assert telemetry.active is False
    assert telemetry.warnings == ()
    with telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        task.set_outcome(OUTCOME_COMPLETED)
    telemetry.shutdown()


def test_enabled_telemetry_degrades_and_stays_usable(without_opentelemetry):
    telemetry = Telemetry(TelemetryConfig(enabled=True))
    telemetry.start()
    telemetry.start()
    assert telemetry.active is False
    with telemetry.task_span(task_id="T", project_id="p", risk="low", max_attempts=1) as task:
        with telemetry.attempt_span(attempt_number=1) as attempt:
            with telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER) as phase:
                assert phase.recording is False
                phase.set_outcome(OUTCOME_OK)
            attempt.set_outcome(OUTCOME_COMPLETED)
        task.set_outcome(OUTCOME_COMPLETED)
    telemetry.flush()
    telemetry.shutdown()
    assert telemetry.warnings == ("telemetry init unavailable (ModuleNotFoundError)",)


def test_exception_path_is_unchanged_without_the_extra(without_opentelemetry):
    """A missing SDK must not swallow or replace an orchestration exception."""
    telemetry = build_telemetry(TelemetryConfig(enabled=True))
    sentinel = RuntimeError("original failure")
    with pytest.raises(RuntimeError) as caught:
        with telemetry.phase_span(phase_name=PHASE_IMPLEMENTATION, role=ROLE_IMPLEMENTER):
            raise sentinel
    assert caught.value is sentinel


def test_config_validation_is_identical_without_the_extra(without_opentelemetry, tmp_path: Path):
    """Configuration validation is pure Python and must not depend on the extra being installed."""
    config = tmp_path / "forge.toml"
    config.write_text(
        '[paths]\ndata_root = "data"\n\n[codex]\napproval_policy = "never"\n\n'
        '[telemetry]\nendpoint = "http://example.com:4317"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="loopback"):
        load_config(config)
