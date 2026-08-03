from pathlib import Path

from codex_forge.validation import run_validations


def test_validation_stops_after_failure(tmp_path: Path):
    commands = [
        "python -c \"print('ok')\"",
        "python -c \"import sys; print('bad'); sys.exit(3)\"",
        "python -c \"raise RuntimeError('must not run')\"",
    ]
    results = run_validations(
        commands,
        cwd=tmp_path,
        artifact_dir=tmp_path / "logs",
        timeout_seconds=30,
    )
    assert len(results) == 2
    assert results[0].passed
    assert not results[1].passed
    assert results[1].exit_code == 3


def test_powershell_wrapper_preserves_native_exit_code():
    from codex_forge.validation import _powershell_wrapper

    wrapped = _powershell_wrapper("python -c \"import sys; sys.exit(3)\"")
    assert "$forgeExitCode = $LASTEXITCODE" in wrapped
    assert "exit $forgeExitCode" in wrapped


def test_validation_missing_executable_is_failure(tmp_path: Path):
    results = run_validations(
        [r'& "Z:\definitely-missing\nope.exe"'],
        cwd=tmp_path,
        artifact_dir=tmp_path / "logs",
        timeout_seconds=30,
    )
    assert results[0].exit_code == 1
    assert not results[0].passed
    assert "not recognized" in results[0].stderr_path.read_text(encoding="utf-8")


def test_validation_preserves_unicode_output(tmp_path: Path):
    command = "python -c \"print('Zażółć — 東京 — 🚀')\""
    results = run_validations(
        [command],
        cwd=tmp_path,
        artifact_dir=tmp_path / "logs",
        timeout_seconds=30,
    )
    assert results[0].exit_code == 0
    assert "Zażółć — 東京 — 🚀" in results[0].stdout_path.read_text(encoding="utf-8")


def test_validation_marks_controller_owned_execution():
    from codex_forge.validation import _validation_environment

    assert _validation_environment()["CODEX_FORGE_CONTROLLER_VALIDATION"] == "1"
