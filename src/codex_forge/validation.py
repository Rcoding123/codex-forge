from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .models import ValidationResult
from .processes import ProcessSupervisionError, run_supervised as run_process
from .utils import ensure_dir, tail_text


def _powershell_wrapper(command: str) -> str:
    """Run a command while preserving the exit code of native child processes."""
    return (
        "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$global:LASTEXITCODE = 0; "
        "$Error.Clear(); "
        f"& {{ {command} }}; "
        "$forgeSuccess = $?; "
        "$forgeExitCode = $LASTEXITCODE; "
        "$forgeErrorCount = $Error.Count; "
        "if ((-not $forgeSuccess -or $forgeErrorCount -gt 0) -and $forgeExitCode -eq 0) { $forgeExitCode = 1 }; "
        "exit $forgeExitCode"
    )


def _shell_command(command: str) -> list[str]:
    if shutil.which("pwsh"):
        return [
            "pwsh",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _powershell_wrapper(command),
        ]
    if shutil.which("powershell"):
        return [
            "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _powershell_wrapper(command),
        ]
    return ["bash", "-lc", command]


def _validation_environment() -> dict[str, str]:
    """Expose the running virtual environment to child shells without activation."""
    env = os.environ.copy()
    interpreter_dir = str(Path(sys.executable).resolve().parent)
    current_path = env.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    normalized_entries = {os.path.normcase(os.path.abspath(item)) for item in path_entries if item}
    normalized_interpreter_dir = os.path.normcase(os.path.abspath(interpreter_dir))
    if normalized_interpreter_dir not in normalized_entries:
        env["PATH"] = interpreter_dir + (os.pathsep + current_path if current_path else "")
    if sys.prefix != sys.base_prefix:
        env["VIRTUAL_ENV"] = sys.prefix
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env["CODEX_FORGE_CONTROLLER_VALIDATION"] = "1"
    return env


def run_validations(
    commands: list[str],
    *,
    cwd: Path,
    artifact_dir: Path,
    timeout_seconds: int,
    max_summary_chars: int = 12_000,
) -> list[ValidationResult]:
    ensure_dir(artifact_dir)
    results: list[ValidationResult] = []
    validation_env = _validation_environment()
    for index, command in enumerate(commands, start=1):
        started = time.monotonic()
        stdout_path = artifact_dir / f"validation-{index:02d}.stdout.log"
        stderr_path = artifact_dir / f"validation-{index:02d}.stderr.log"
        try:
            with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
                process = run_process(
                    _shell_command(command), cwd=cwd, timeout=timeout_seconds,
                    env=validation_env, stdout=out, stderr=err,
                )
            exit_code = process.returncode
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            with stderr_path.open("a", encoding="utf-8") as err:
                err.write("\n" + str(exc))
        except ProcessSupervisionError:
            raise
        except Exception as exc:  # Controller must preserve evidence, not crash silently.
            exit_code = 125
            with stderr_path.open("a", encoding="utf-8") as err:
                err.write("\n" + repr(exc))
        duration = time.monotonic() - started
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        combined = "\n".join(part for part in (stdout, stderr) if part)
        results.append(
            ValidationResult(
                command=command,
                exit_code=exit_code,
                passed=exit_code == 0,
                duration_seconds=duration,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                summary=tail_text(combined, max_summary_chars),
            )
        )
        if exit_code != 0:
            break
    return results
