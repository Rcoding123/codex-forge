from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ForgeConfig
from .models import CodexRunResult
from .utils import ensure_dir, tail_text


# A Windows image that fails initialization never reaches its own entry point: the loader aborts
# and the exit status is the raw NTSTATUS. Forge has to read these as "the command never started",
# which is a different fact from "the command ran and failed".
STATUS_DLL_INIT_FAILED = -1073741502  # 0xC0000142
STATUS_DLL_NOT_FOUND = -1073741515  # 0xC0000135
STATUS_INVALID_IMAGE_FORMAT = -1073741701  # 0xC000007B
PROCESS_LAUNCH_FAILURE_EXIT_CODES = frozenset(
    {STATUS_DLL_INIT_FAILED, STATUS_DLL_NOT_FOUND, STATUS_INVALID_IMAGE_FORMAT}
)

# Codex enforces these two modes on Windows by running every model command through a restricted
# token; `danger-full-access` launches commands directly, which is why an implementation phase and a
# review phase of the same task can disagree about whether the command runner works at all.
RESTRICTED_TOKEN_SANDBOXES = frozenset({"read-only", "workspace-write"})

SANDBOX_PROBE_TIMEOUT_SECONDS = 120

BLOCK_SANDBOX_UNLAUNCHABLE = "sandbox_launch_unavailable"
BLOCK_COMMANDS_NEVER_EXECUTED = "review_commands_never_executed"

# Evidence stays useful without becoming unbounded: enough failing commands to identify the runner,
# each cut to a length a reader can scan.
_MAX_RECORDED_COMMANDS = 10
_MAX_RECORDED_COMMAND_CHARS = 500


def normalize_exit_code(exit_code: object) -> int | None:
    """Read a Windows exit status as the signed 32-bit value NTSTATUS is documented with.

    The same failure reaches Forge in two representations. Codex's event stream carries Rust's
    `ExitStatus::code()`, an `i32`, so `STATUS_DLL_INIT_FAILED` arrives as `-1073741502`. Python's
    `subprocess` returns the raw `GetExitCodeProcess` DWORD, so the identical status arrives as
    `3221225794`. Only a status with the high bit set is folded, which no ordinary exit code has.
    """
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return None
    if exit_code > 0x7FFF_FFFF:
        return exit_code - 0x1_0000_0000
    return exit_code


def is_process_launch_failure(exit_code: object) -> bool:
    """Report whether an exit status means the image never started."""
    return normalize_exit_code(exit_code) in PROCESS_LAUNCH_FAILURE_EXIT_CODES


@dataclass(frozen=True, slots=True)
class SandboxProbe:
    """The observed ability of the Codex sandbox to launch a real process in this logon session."""

    launchable: bool
    exit_code: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class SandboxResolution:
    """The sandbox a phase will actually run under, and why it differs from the one requested."""

    requested: str
    sandbox: str | None
    probe: SandboxProbe | None

    @property
    def degraded(self) -> bool:
        return self.sandbox is not None and self.sandbox != self.requested


@dataclass(frozen=True, slots=True)
class ReviewInfrastructureBlock:
    """A review that could not inspect anything because its command runner was unavailable.

    This is deliberately not a review verdict. It records a fact about the reviewer's runtime, so
    the controller can refuse to read it as a statement about the candidate's source code.
    """

    reason: str
    requested_sandbox: str
    sandbox: str | None
    exit_codes: tuple[int, ...]
    commands: tuple[str, ...]
    detail: str
    evidence: dict[str, str]

    def summary(self) -> str:
        codes = ", ".join(str(code) for code in self.exit_codes) or "none recorded"
        return (
            f"The review command runner could not execute any command "
            f"(reason {self.reason}, requested sandbox {self.requested_sandbox}, "
            f"effective sandbox {self.sandbox or 'none launchable'}, exit codes {codes}). "
            f"{self.detail}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": "REVIEW_INFRASTRUCTURE_BLOCKED",
            "reason": self.reason,
            "requested_sandbox": self.requested_sandbox,
            "effective_sandbox": self.sandbox,
            "exit_codes": list(self.exit_codes),
            "failed_commands": list(self.commands),
            "detail": self.detail,
            "evidence": dict(self.evidence),
            "summary": self.summary(),
        }


def _bounded_command(command: object) -> str:
    text = str(command or "")
    return text if len(text) <= _MAX_RECORDED_COMMAND_CHARS else text[:_MAX_RECORDED_COMMAND_CHARS] + "..."


def sandbox_infrastructure_block(resolution: SandboxResolution) -> ReviewInfrastructureBlock:
    """Describe a review that cannot start because no sandbox on this host can launch a command."""
    probe = resolution.probe
    return ReviewInfrastructureBlock(
        reason=BLOCK_SANDBOX_UNLAUNCHABLE,
        requested_sandbox=resolution.requested,
        sandbox=None,
        exit_codes=(probe.exit_code,) if probe and probe.exit_code is not None else (),
        commands=(),
        detail=(
            "The Codex sandbox cannot launch a process in this logon session, and the configured "
            "sandbox is restricted as well, so no review command runner is available. "
            f"{probe.detail if probe else ''}".strip()
        ),
        evidence={},
    )


def detect_review_infrastructure_block(
    result: CodexRunResult, *, requested_sandbox: str, sandbox: str | None
) -> ReviewInfrastructureBlock | None:
    """Report a review whose commands never ran, reading the reviewer's own event stream.

    The test is deliberately conservative: a single command that actually executed — including one
    that executed and exited non-zero — means the reviewer had some ability to inspect the
    candidate, and its verdict is left alone. Only a review where every attempted command died in
    process launch is reported as blocked.
    """
    attempted: list[str] = []
    launch_failures: list[int] = []
    executed = 0
    try:
        stream = result.jsonl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") != "command_execution":
            continue
        exit_code = normalize_exit_code(item.get("exit_code"))
        if is_process_launch_failure(exit_code):
            launch_failures.append(exit_code)
            if len(attempted) < _MAX_RECORDED_COMMANDS:
                attempted.append(_bounded_command(item.get("command")))
        elif isinstance(exit_code, int):
            executed += 1
    if not launch_failures or executed:
        return None
    return ReviewInfrastructureBlock(
        reason=BLOCK_COMMANDS_NEVER_EXECUTED,
        requested_sandbox=requested_sandbox,
        sandbox=sandbox,
        exit_codes=tuple(dict.fromkeys(launch_failures)),
        commands=tuple(attempted),
        detail=(
            f"{len(launch_failures)} review command(s) failed during Windows process launch and "
            "none executed, so no part of the candidate was inspected."
        ),
        evidence={
            "events": str(result.jsonl_path),
            "final": str(result.final_path),
            "stderr": str(result.stderr_path),
        },
    )


def _build_process_command(executable: str, arguments: list[str]) -> list[str]:
    """Build a cross-platform command for native binaries, scripts, and Windows shims."""
    resolved = shutil.which(executable) or executable
    suffix = Path(resolved).suffix.lower()

    if suffix == ".py":
        return [sys.executable, resolved, *arguments]

    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        comspec = os.environ.get("ComSpec") or os.environ.get("COMSPEC") or "cmd.exe"
        # The shim path and its arguments stay separate elements: `subprocess` runs
        # `list2cmdline` over whatever it is handed, so pre-flattening here would quote
        # everything twice. The second pass rewrites the first pass's real quotes as
        # literal `\"`, which `cmd.exe` does not undo, so an argument containing a space
        # arrives at the shim's child split at every space.
        #
        # `call` is load-bearing: with `/s`, `cmd.exe` strips the first and last quote of
        # the command line whenever it begins with a quote, which mangles the vector as
        # soon as the shim path itself needs quoting. A leading unquoted word means there
        # is no leading quote to strip, so every quote reaches the shim as written.
        return [comspec, "/d", "/s", "/c", "call", resolved, *arguments]

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
        self._sandbox_probe: SandboxProbe | None = None
        self._sandbox_probe_lock = threading.Lock()

    def probe_sandbox_launch(self, *, cwd: Path) -> SandboxProbe:
        """Check that the Codex sandbox can start a process in the controller's own logon session.

        The Windows sandbox is a restricted token, and a restricted token cannot reach the
        non-interactive window station a service-style logon owns. Every executable a reviewer needs
        — the shell, `git`, `python` — links `USER32.dll`, whose initialization needs that window
        station, so all of them abort with `STATUS_DLL_INIT_FAILED` before running any of their own
        code. The probe launches the same interpreter Forge already exposes to worker shells, using
        the same command construction and the same environment as a real phase, so it observes the
        launch path a phase would take rather than predicting it.

        Only a recognized process-launch failure is decisive. Any other outcome — including a Codex
        build with no `sandbox` subcommand — leaves the requested sandbox in force, so this can
        never downgrade a sandbox on the strength of an inconclusive probe.
        """
        with self._sandbox_probe_lock:
            if self._sandbox_probe is not None:
                return self._sandbox_probe
            command = _build_process_command(
                self.config.codex_executable,
                ["sandbox", "--", sys.executable, "-c", "pass"],
            )
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=SANDBOX_PROBE_TIMEOUT_SECONDS,
                    env=_worker_environment(),
                )
                exit_code: int | None = normalize_exit_code(completed.returncode)
                detail = tail_text(completed.stderr or completed.stdout or "", 1_000)
            except subprocess.TimeoutExpired:
                exit_code = None
                detail = f"Sandbox probe timed out after {SANDBOX_PROBE_TIMEOUT_SECONDS} seconds."
            except OSError as exc:
                exit_code = None
                detail = f"Sandbox probe could not start: {exc!r}"
            probe = SandboxProbe(
                launchable=not is_process_launch_failure(exit_code),
                exit_code=exit_code,
                detail=detail or f"Sandbox probe exited {exit_code}.",
            )
            self._sandbox_probe = probe
            return probe

    def resolve_sandbox(self, requested: str, *, cwd: Path) -> SandboxResolution:
        """Choose the sandbox a phase can actually run commands under.

        A mode that does not use a restricted token is returned unchanged and unprobed. A restricted
        mode is returned only once the probe shows it can launch a process. When it cannot, the
        fallback is the sandbox the operator already trusts this host's Codex to run implementation
        phases under — never a wider one — and if that is restricted too, nothing is launchable and
        the caller is told so instead of being handed a runner that cannot execute anything.
        """
        if requested not in RESTRICTED_TOKEN_SANDBOXES:
            return SandboxResolution(requested=requested, sandbox=requested, probe=None)
        probe = self.probe_sandbox_launch(cwd=cwd)
        if probe.launchable:
            return SandboxResolution(requested=requested, sandbox=requested, probe=probe)
        fallback = self.config.sandbox
        usable = None if fallback in RESTRICTED_TOKEN_SANDBOXES else fallback
        return SandboxResolution(requested=requested, sandbox=usable, probe=probe)

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
