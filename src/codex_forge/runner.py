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
from .processes import run_supervised
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
BLOCK_REVIEW_TRANSPORT_FAILED = "review_transport_failed"
BLOCK_REVIEW_OUTPUT_UNUSABLE = "review_output_unusable"

# Controller-owned failure kinds for a review run whose process exited 0 but whose final output
# never became a valid ReviewResult. Forge itself names which decode step failed; no reviewer
# byte and no parser prose participates in the kind, so these are safe for durable state and
# for failure identity.
REVIEW_OUTPUT_MISSING = "missing_output"
REVIEW_OUTPUT_INVALID_JSON = "invalid_json"
REVIEW_OUTPUT_SCHEMA_INVALID = "schema_invalid"
# Decoding proves the bytes; it does not prove the run behind them. A review whose own event stream
# shows a command it launched and never recorded an outcome for — or whose stream cannot be read at
# all — asserted its verdict on evidence the run never produced, so the verdict is refused with the
# same controller-owned ownership as any other unusable output.
REVIEW_OUTPUT_COMMANDS_UNCLOSED = "commands_unclosed"

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
    http_status: int | None = None
    kind: str | None = None

    def summary(self) -> str:
        codes = ", ".join(str(code) for code in self.exit_codes) or "none recorded"
        if self.reason == BLOCK_REVIEW_OUTPUT_UNUSABLE:
            return (
                f"The review transport completed but produced no usable structured review result "
                f"(reason {self.reason}, kind {self.kind}, exit codes {codes}), so nothing was "
                f"said about the candidate. {self.detail}"
            )
        if self.reason == BLOCK_REVIEW_TRANSPORT_FAILED:
            return (
                f"The review provider transport could not execute the review request at all "
                f"(reason {self.reason}, exit codes {codes}), so nothing was said about the "
                f"candidate. {self.detail}"
            )
        return (
            f"The review command runner could not execute any command "
            f"(reason {self.reason}, requested sandbox {self.requested_sandbox}, "
            f"effective sandbox {self.sandbox or 'none launchable'}, exit codes {codes}). "
            f"{self.detail}"
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
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
        if self.http_status is not None:
            data["http_status"] = self.http_status
        if self.kind is not None:
            data["kind"] = self.kind
        return data


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


@dataclass(frozen=True, slots=True)
class ReviewCommandClosure:
    """Whether a review run's own event stream accounted for every command the run launched.

    Two explicit fields rather than an optional tuple: a certified review and a review whose event
    stream could not be read at all both carry no open commands, and they must not be
    distinguishable only by truthiness at the call site — one is admitted, the other refused.
    """

    certified: bool
    open_commands: tuple[str, ...]


def review_command_closure(result: CodexRunResult) -> ReviewCommandClosure:
    """Report the commands a review launched whose outcome its own event stream never recorded.

    The rule is deliberately one-directional: what impeaches a verdict is an *unobserved outcome*,
    not an unobserved launch. Over `command_execution` items only, the open set is the started ids
    minus the completed ids, so a completion with no matching start is fine, duplicate ids and
    out-of-order lines cannot manufacture a refusal, and a review that ran no command at all is
    trivially closed — this never obliges a reviewer to execute anything.

    Identity is explicit and never inferred from counts or chronology: a start pairs only through a
    non-empty string `id`, so a start carrying no usable identity is unmatchable and stays open.
    Anything the stream does not claim an outcome for — malformed lines, other item types — is
    outside the judgment; only `command_execution` items assert command outcomes.

    Timing is settled rather than racy: `CodexRunner.run` joins the CLI process and writes the full
    stream before returning, so at the caller's door "no matching completion" is a final fact about
    the run's recorded evidence. Note the deliberate asymmetry with
    `detect_review_infrastructure_block`, whose consequence is terminal and which therefore stays
    silent when the stream cannot be read: here the consequence is a bounded retry, and admitting a
    verdict whose evidence stream cannot be read at all would be evidence ambiguity failing open,
    so an unreadable stream is refused with no open commands to name.

    What this certifies is the reviewer's recorded evidence, never the operating system: a closed
    command proves an outcome was recorded, not that a child process exited.

    The returned command texts are bounded exactly like the sibling detector's — at most
    `_MAX_RECORDED_COMMANDS` entries, each cut to `_MAX_RECORDED_COMMAND_CHARS` — because they are
    reviewer-influenced bytes and belong only in the raw-facing block artifact. `certified` reflects
    every open command, including any beyond that bound.
    """
    started: dict[str, str] = {}
    unidentified: list[str] = []
    completed: set[str] = set()
    try:
        stream = result.jsonl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ReviewCommandClosure(certified=False, open_commands=())
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        identity = item.get("id")
        identified = isinstance(identity, str) and identity != ""
        if event.get("type") == "item.started":
            if not identified:
                unidentified.append(_bounded_command(item.get("command")))
            elif identity not in started:
                started[identity] = _bounded_command(item.get("command"))
        elif event.get("type") == "item.completed" and identified:
            completed.add(identity)
    open_commands = [
        command for identity, command in started.items() if identity not in completed
    ]
    open_commands.extend(unidentified)
    return ReviewCommandClosure(
        certified=not open_commands,
        open_commands=tuple(open_commands[:_MAX_RECORDED_COMMANDS]),
    )


def transport_http_status(result: CodexRunResult) -> int | None:
    """The numeric HTTP status of a refused run, if the provider's error event carried one.

    This is the only fact read out of provider output for durable use, and it is admitted under
    a strict numeric type check: a status counts only as a JSON integer, so no provider-generated
    string can ride through this path. Everything else the provider said — error codes, types,
    params, messages, request IDs — stays in the raw event stream and stderr artifacts the runner
    retained, which are the only owners of verbatim provider text. The last ``error`` event wins
    because it is the provider's own statement of why the request never produced an answer.
    """
    try:
        stream = result.jsonl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    last_error: str | None = None
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "error" and isinstance(event.get("message"), str):
            last_error = event["message"]
    if not last_error:
        return None
    try:
        payload = json.loads(last_error)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def decode_review_result(
    result: CodexRunResult, schema: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """The single transition from untrusted reviewer bytes to a trusted semantic ReviewResult.

    A semantic review exists if and only if this function returns one: the reviewer's final
    output file must exist, be non-empty, parse as JSON, and satisfy the same review-result
    schema document the transport already enforces. Process exit 0 proves nothing by itself.

    On failure the return value is ``(None, kind)`` where ``kind`` is a controller-owned token
    naming which decode step failed — never any reviewer byte, parser message, or path. The raw
    final output stays in its artifact file, which remains the only owner of the unusable text.
    """
    try:
        raw = result.final_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, REVIEW_OUTPUT_MISSING
    if not raw.strip():
        return None, REVIEW_OUTPUT_MISSING
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, REVIEW_OUTPUT_INVALID_JSON
    if not isinstance(payload, dict) or not _instance_conforms(payload, schema):
        return None, REVIEW_OUTPUT_SCHEMA_INVALID
    return payload, None


def _instance_conforms(instance: object, schema: dict[str, Any]) -> bool:
    """Whether `instance` satisfies `schema`, for the keyword subset the transported schemas use.

    Deliberately boolean: a validation failure must contribute nothing describable to durable
    state, so no violation prose is ever produced. The supported keywords — `type` (including
    list form), `enum`, `properties`, `required`, `additionalProperties: false`, `items` — are
    exactly the set `schemas/*.schema.json` use; tests pin that the shipped documents stay
    inside it by exercising this gate against them.
    """
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_is_json_type(instance, name) for name in names):
            return False
    if "enum" in schema and instance not in schema["enum"]:
        return False
    if isinstance(instance, dict):
        properties = schema.get("properties") or {}
        if any(key not in instance for key in schema.get("required") or []):
            return False
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in instance
        ):
            return False
        return all(
            _instance_conforms(value, properties[key])
            for key, value in instance.items()
            if key in properties
        )
    if isinstance(instance, list) and "items" in schema:
        return all(_instance_conforms(item, schema["items"]) for item in instance)
    return True


def _is_json_type(value: object, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "null":
        return value is None
    return False


class ReviewTransportError(RuntimeError):
    """The review transport could not execute the review request at all.

    Raised when the Codex CLI exits non-zero for a review phase: a provider API rejection (for
    example an invalid structured-output schema refused before any model answer), a transport-level
    CLI failure, or an equivalent inability of the review infrastructure to run. It deliberately
    subclasses ``RuntimeError`` so any handler that predates it treats it exactly like the generic
    phase failure it replaces. It is not a review verdict: nothing was said about the candidate.

    ``http_status`` is the numeric HTTP status when the provider's error event carried one — the
    only provider-derived fact admitted into durable state. The provider's verbatim output stays
    only in the retained event stream and stderr artifacts that ``evidence`` points at.
    """

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        exit_code: int | None,
        http_status: int | None,
        evidence: dict[str, str],
    ):
        super().__init__(message)
        self.phase = phase
        self.exit_code = exit_code
        self.http_status = http_status
        self.evidence = evidence


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
                process = run_supervised(
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
