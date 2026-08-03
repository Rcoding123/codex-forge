from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import ForgeConfig
from .models import CodexRunResult
from .utils import ensure_dir


def _build_process_command(executable: str, arguments: list[str]) -> list[str]:
    """Build a cross-platform command for native binaries, scripts, and Windows shims."""
    resolved = shutil.which(executable) or executable
    suffix = Path(resolved).suffix.lower()

    if suffix == ".py":
        return [sys.executable, resolved, *arguments]

    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        comspec = os.environ.get("ComSpec") or os.environ.get("COMSPEC") or "cmd.exe"
        command_line = subprocess.list2cmdline([resolved, *arguments])
        return [comspec, "/d", "/s", "/c", command_line]

    return [resolved, *arguments]


def _worker_environment() -> dict[str, str]:
    """Expose Forge's private interpreter to unattended Codex worker shells."""
    env = os.environ.copy()
    interpreter_dir = str(Path(sys.executable).resolve().parent)
    current_path = env.get("PATH", "")
    entries = current_path.split(os.pathsep) if current_path else []
    normalized = {os.path.normcase(os.path.abspath(item)) for item in entries if item}
    if os.path.normcase(os.path.abspath(interpreter_dir)) not in normalized:
        env["PATH"] = interpreter_dir + (os.pathsep + current_path if current_path else "")
    if sys.prefix != sys.base_prefix:
        env["VIRTUAL_ENV"] = sys.prefix
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


class CodexRunner:
    def __init__(self, config: ForgeConfig, schema_root: Path):
        self.config = config
        self.schema_root = schema_root

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        phase: str,
        model: str,
        effort: str,
        schema_name: str,
        sandbox: str | None = None,
    ) -> CodexRunResult:
        ensure_dir(artifact_dir)
        jsonl_path = artifact_dir / f"{phase}.events.jsonl"
        final_path = artifact_dir / f"{phase}.final.json"
        stderr_path = artifact_dir / f"{phase}.stderr.log"
        prompt_path = artifact_dir / f"{phase}.prompt.txt"
        schema_path = self.schema_root / schema_name
        prompt_path.write_text(prompt, encoding="utf-8")

        disabled_feature_args = [
            item
            for feature in dict.fromkeys(self.config.worker_disabled_features)
            for item in ("--disable", feature)
        ]
        arguments = [
            "--ask-for-approval",
            self.config.approval_policy,
            "--sandbox",
            sandbox or self.config.sandbox,
            "--model",
            model,
            "-c",
            f"model_reasoning_effort={effort}",
            *disabled_feature_args,
            "exec",
            "--json",
            "--strict-config",
            "--output-schema",
            str(schema_path),
            "-o",
            str(final_path),
            *self.config.extra_codex_args,
            "-",
        ]
        command = _build_process_command(self.config.codex_executable, arguments)

        timeout = self.config.task_timeout_minutes * 60
        exit_code = 1
        with jsonl_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_handle:
            try:
                process = subprocess.run(
                    command,
                    cwd=str(cwd),
                    input=prompt,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    timeout=timeout,
                    env=_worker_environment(),
                )
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                stderr_handle.write(f"\nCodex run timed out after {timeout} seconds.\n")
                exit_code = 124
            except OSError as exc:
                stderr_handle.write(f"\nCodex process could not start: {exc!r}\n")
                exit_code = 126

        stdout = jsonl_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")

        thread_id: str | None = None
        usage: dict[str, int] = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or usage

        final: dict[str, Any]
        if final_path.exists():
            try:
                final = json.loads(final_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                final = {
                    "status": "blocked",
                    "summary": "Codex returned a non-JSON final response.",
                    "blocking_reason": final_path.read_text(encoding="utf-8", errors="replace"),
                    "changed_files": [],
                    "validation_performed": [],
                    "assumptions": [],
                    "decisions_needed": [],
                    "reusable_patterns": [],
                }
        else:
            final = {
                "status": "blocked",
                "summary": "Codex did not create a final result file.",
                "blocking_reason": stderr[-12_000:],
                "changed_files": [],
                "validation_performed": [],
                "assumptions": [],
                "decisions_needed": [],
                "reusable_patterns": [],
            }

        return CodexRunResult(
            exit_code=exit_code,
            final=final,
            jsonl_path=jsonl_path,
            final_path=final_path,
            stderr_path=stderr_path,
            thread_id=thread_id,
            usage=usage,
        )
