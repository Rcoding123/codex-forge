"""One request, one model invocation, caller-owned validation, one result.

Git-visible source auditing is evidence, not an operating-system security boundary.
The caller remains responsible for accepting the resulting task.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .contract import (
    FINAL_SCHEMA, RequestError, build_prompt, compact_handoff, normalize_usage,
    parse_final, parse_request, summarize_diagnostics,
)
from .policy import PolicyError, build_argv, inspect_policy
from .process import run_owned


class SourceError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _git(workspace: Path, *args: str) -> bytes:
    executable = shutil.which("git")
    if not executable:
        raise SourceError("git_unavailable", "Git is required for source binding")
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    process = subprocess.run(
        [executable, "--no-optional-locks", "-C", str(workspace), *args],
        env=env, capture_output=True, timeout=20, check=False,
    )
    if process.returncode:
        raise SourceError("source_unavailable", "Git could not verify the requested workspace")
    if len(process.stdout) > 4 * 1024 * 1024:
        raise SourceError("source_limit", "Git source inventory exceeds the adapter limit")
    return process.stdout


def _plain(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SourceError("source_reparse", "Source symlinks and reparse points are unsupported")


def _snapshot(workspace: Path) -> dict[str, str]:
    """Hash tracked and nonignored untracked files plus the worktree binding file."""
    names = _git(workspace, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    relative_paths = set(names.decode("utf-8").rstrip("\0").split("\0")) - {""}
    if (workspace / ".git").is_file():
        relative_paths.add(".git")
    if len(relative_paths) > 20_000:
        raise SourceError("source_limit", "Workspace exceeds the 20000-file audit limit")
    total = 0
    snapshot = {}
    for relative in sorted(relative_paths):
        path = workspace / relative
        if not path.exists() and not path.is_symlink():
            continue
        for part in [path, *path.parents]:
            if part == workspace:
                break
            _plain(part)
        if not path.resolve().is_relative_to(workspace):
            raise SourceError("source_escape", "Source path escapes the caller worktree")
        if not path.is_file():
            raise SourceError("source_type", "Submodules and special source files are unsupported")
        total += path.stat().st_size
        if total > 512 * 1024 * 1024:
            raise SourceError("source_limit", "Workspace exceeds the 512 MiB audit limit")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        snapshot[relative.replace("\\", "/")] = digest.hexdigest()
    return snapshot


def _git_metadata(workspace: Path) -> dict:
    """Bind semantic index entries/flags, branch identity and local configuration.

    Index stat-cache refreshes may change bytes without changing Git semantics.
    Pin the staged objects, stages and assume-unchanged/skip-worktree flags instead.
    """
    common = Path(_git(workspace, "rev-parse", "--git-common-dir").decode().strip())
    git_dir = Path(_git(workspace, "rev-parse", "--git-dir").decode().strip())
    common = (workspace / common).resolve() if not common.is_absolute() else common.resolve()
    git_dir = (workspace / git_dir).resolve() if not git_dir.is_absolute() else git_dir.resolve()
    configs = {}
    for path in {common / "config", git_dir / "config.worktree"}:
        if path.exists():
            _plain(path)
            configs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            configs[str(path)] = None
    return {
        "index": hashlib.sha256(_git(workspace, "ls-files", "--stage", "-v", "-z")).hexdigest(),
        "branch": _git(workspace, "rev-parse", "--symbolic-full-name", "HEAD").decode().strip(),
        "git_dir": str(git_dir),
        "common_dir": str(common),
        "configs": configs,
    }


def _binding(workspace: Path) -> tuple[int, int]:
    _plain(workspace)
    binding = workspace / ".git"
    _plain(binding)
    info = binding.stat()
    return info.st_dev, info.st_ino


def _assert_binding(workspace: Path, before: dict, request: dict, git_before: dict,
                    binding_before: tuple[int, int]) -> None:
    if _binding(workspace) != binding_before:
        raise SourceError("worktree_binding_changed", "The checkout's Git binding changed")
    if ".git" in before and hashlib.sha256((workspace / ".git").read_bytes()).hexdigest() != before[".git"]:
        raise SourceError("worktree_binding_changed", "The linked worktree binding changed")
    if _git(workspace, "rev-parse", "HEAD").decode().strip().lower() != request["workspace"]["base_revision"].lower():
        raise SourceError("worktree_head_changed", "The candidate changed the caller-owned HEAD")
    if _git_metadata(workspace) != git_before:
        raise SourceError("worktree_metadata_changed", "The candidate changed caller-owned Git metadata")


def _source_state(workspace: Path, *, in_place: bool) -> tuple[str, str]:
    binding = workspace / ".git"
    linked = binding.is_file()
    if not workspace.is_dir() or not (linked or (in_place and binding.is_dir())):
        raise SourceError("worktree_required", "Supply a linked Git worktree or opt in to an ordinary checkout")
    _binding(workspace)
    if Path(_git(workspace, "rev-parse", "--show-toplevel").decode().strip()).resolve() != workspace:
        raise SourceError("worktree_root", "Workspace must be the exact Git checkout root")
    if not linked:
        git_dir = Path(_git(workspace, "rev-parse", "--absolute-git-dir").decode().strip()).resolve()
        common = Path(_git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
                      .decode().strip()).resolve()
        if git_dir != binding or common != binding:
            raise SourceError("worktree_required", "An ordinary checkout must use its own .git directory")
    head = _git(workspace, "rev-parse", "HEAD").decode().strip()
    flags = _git(workspace, "ls-files", "-v", "-z").split(b"\0")
    if any(record and not record.startswith(b"H ") for record in flags):
        raise SourceError("unsupported_index_flags", "Hidden, sparse or unmerged index states are unsupported")
    dirty = bool(_git(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    return head, "dirty" if dirty else "clean"


def capture_checkout(path: Path) -> dict:
    """Capture the exact source pins for an explicitly in-place request."""
    workspace = path.resolve(strict=True)
    head, state = _source_state(workspace, in_place=True)
    return {
        "path": str(workspace), "base_revision": head, "expected_state": state,
        "expected_files": _snapshot(workspace), "in_place": True,
    }


def _workspace(request: dict) -> tuple[Path, dict[str, str]]:
    spec = request["workspace"]
    workspace = Path(spec["path"]).resolve(strict=True)
    in_place = spec.get("in_place", False)
    head, state = _source_state(workspace, in_place=in_place)
    base = spec["base_revision"]
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base) or head.lower() != base.lower():
        raise SourceError("stale_source", "Workspace HEAD differs from the exact base revision")
    if state != spec["expected_state"]:
        raise SourceError("stale_source", "Workspace clean/dirty state differs from the request")
    expected = spec.get("expected_files")
    if (in_place or state == "dirty") and expected is None:
        raise SourceError("source_binding_required", "Supply exact expected_files pins for this checkout")
    snapshot = _snapshot(workspace)
    if (in_place or state == "dirty" or expected) and expected != snapshot:
        raise SourceError("stale_source", "Workspace files differ from caller-supplied pins")
    return workspace, snapshot


def _component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value) or value.endswith("."):
        raise SourceError("invalid_identity", "Job and attempt identities must be safe path components")
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                                         *[f"LPT{i}" for i in range(1, 10)]}:
        raise SourceError("invalid_identity", "Reserved Windows path component")
    return value


def _artifacts(request: dict, workspace: Path) -> Path:
    root = Path(request["artifact_root"]).resolve()
    implementation_root = Path(__file__).resolve().parents[3]
    if root.is_relative_to(workspace) or root.is_relative_to(implementation_root):
        raise SourceError("artifact_location", "Evidence must live outside source and adapter repositories")
    artifact = root / _component(request["job_id"]) / _component(request["attempt_id"])
    artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for ancestor in [artifact.parent, *artifact.parent.parents]:
        _plain(ancestor)
    artifact.mkdir(mode=0o700, exist_ok=False)
    return artifact


def _write_json(path: Path, data: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _audit(before: dict, after: dict, allowed: list[str]) -> dict:
    changes = [
        {"path": path, "before": before.get(path), "after": after.get(path)}
        for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)
    ]
    forbidden = [entry["path"] for entry in changes if entry["path"] == ".git" or
                 not any(fnmatch.fnmatchcase(entry["path"], pattern) for pattern in allowed)]
    return {"passed": not forbidden, "changes": changes, "out_of_policy": forbidden,
            "scope": "tracked and nonignored untracked source; ignored paths are not audited",
            "security_boundary": False}


def _event_log(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("Invalid event envelope")
        events.append(event)
    return events


def _events_complete(events: list[dict]) -> bool:
    """Admit only a successful terminal turn with every started item closed."""
    pending = set()
    turn_open = False
    terminal = None
    completed = 0
    for event in events:
        kind = event["type"]
        if kind in {"error", "turn.failed"}:
            return False
        if kind == "turn.started":
            if turn_open or pending:
                return False
            turn_open = True
            terminal = kind
        elif kind in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                return False
            # Any item work after a completed turn needs a later terminal turn.
            terminal = kind
            turn_open = True
            if kind == "item.started":
                if item["id"] in pending:
                    return False
                pending.add(item["id"])
            else:
                pending.discard(item["id"])
        elif kind == "turn.completed":
            if pending:
                return False
            turn_open = False
            terminal = kind
            completed += 1
    return completed > 0 and terminal == "turn.completed" and not turn_open and not pending


def _empty_result(data: object) -> dict:
    identity = data if isinstance(data, dict) else {}
    return {
        "contract_version": "adapter/1", "caller_id": identity.get("caller_id"),
        "job_id": identity.get("job_id"), "attempt_id": identity.get("attempt_id"),
        "adapter_status": "rejected_preflight", "factory_acceptance": "not_determined",
        "transport": {"launched": False}, "protocol": None, "semantic": None, "handoff": None,
        "validation": {"state": "not_requested", "expected": 0, "run": 0, "commands": []},
        "edit_audit": None, "usage": normalize_usage([]), "error": None, "evidence": {},
        "unknowns": [],
    }


def run_job(data: object, *, executable: str | None = None,
            cancel_event: threading.Event | None = None) -> dict:
    """Run exactly one caller assignment. No queue, retry, routing or acceptance decision."""
    started = time.monotonic()
    result = _empty_result(data)
    artifact = None
    inference_started = False
    try:
        request = parse_request(data)
        commands = (request.get("validation") or {}).get("commands", [])
        result["validation"].update(state="not_run" if commands else "not_requested", expected=len(commands))
        workspace, before = _workspace(request)
        git_before = _git_metadata(workspace)
        binding_before = _binding(workspace)
        prompt, context = build_prompt(request, workspace)
        artifact = _artifacts(request, workspace)
        _write_json(artifact / "request.json", request)
        _write_json(artifact / "source-before.json", before)
        _write_json(artifact / "git-before.json", git_before)
        _write_json(artifact / "context.json", context)
        (artifact / "prompt.txt").write_text(prompt, encoding="utf-8")
        schema_path, final_path = artifact / "final.schema.json", artifact / "final.json"
        _write_json(schema_path, FINAL_SCHEMA)
        result["evidence"] = {"artifact_root": str(artifact), "prompt": str(artifact / "prompt.txt"),
                              "events": str(artifact / "events.jsonl"), "final": str(final_path),
                              "stderr": str(artifact / "stderr.log")}
        report = inspect_policy(request, executable=executable)
        result["provenance"] = {key: report.get(key) for key in
                                ("executable", "executable_sha256", "version", "auth", "capabilities", "config_digest")}
        argv = build_argv(report, request, schema_path=schema_path, final_path=final_path)
        result["provenance"]["argv"] = argv
        remaining = request["deadline_s"] - (time.monotonic() - started)
        if remaining <= 0 or (cancel_event and cancel_event.is_set()):
            raise SourceError("deadline_or_cancel", "Job expired or was cancelled before inference")
        if request["workspace"].get("in_place"):
            _assert_binding(workspace, before, request, git_before, binding_before)
            if _snapshot(workspace) != before:
                raise SourceError("stale_source", "Workspace files changed before inference")
            remaining = request["deadline_s"] - (time.monotonic() - started)
            if remaining <= 0 or (cancel_event and cancel_event.is_set()):
                raise SourceError("deadline_or_cancel", "Job expired or was cancelled before inference")
        inference_started = True
        outcome = run_owned(
            argv, cwd=workspace, env=report["environment"], stdin=prompt.encode("utf-8"),
            stdout_path=artifact / "events.jsonl", stderr_path=artifact / "stderr.log",
            timeout_s=remaining, max_output_bytes=request["output_bytes"], cancel_event=cancel_event,
            monitored_paths=(final_path,),
        )
        result["transport"] = asdict(outcome)
        result["adapter_status"] = "failed_transport"
        events = []
        try:
            events = _event_log(artifact / "events.jsonl")
            result["protocol"] = {"events_parseable": True, "final_present": final_path.is_file(),
                                  "schema_valid": False}
        except (OSError, ValueError, UnicodeError):
            result["protocol"] = {"events_parseable": False, "final_present": final_path.is_file(),
                                  "schema_valid": False}
        result["usage"] = normalize_usage(events)
        _assert_binding(workspace, before, request, git_before, binding_before)
        after = _snapshot(workspace)
        _write_json(artifact / "source-after.json", after)
        result["edit_audit"] = _audit(before, after, request["policy"]["allowed_paths"])
        if not (outcome.launched and outcome.exit_code == 0 and outcome.cleanup_verified and
                not outcome.timed_out and not outcome.cancelled and not outcome.output_limit and not outcome.error):
            return result
        result["adapter_status"] = "failed_protocol"
        if not result["protocol"]["events_parseable"] or not _events_complete(events):
            result["error"] = {"code": "event_protocol", "message": "Model event stream is invalid or failed"}
            return result
        if not final_path.is_file() or final_path.stat().st_size > request["output_bytes"]:
            result["error"] = {"code": "final_missing_or_large", "message": "Final result missing or exceeds byte cap"}
            return result
        final = parse_final(final_path.read_text(encoding="utf-8"))
        result["protocol"]["schema_valid"] = True
        result["semantic"] = {"status": final["status"], "is_model_claim": True}
        result["handoff"] = compact_handoff(final, request["handoff_chars"])
        if not result["edit_audit"]["passed"]:
            result["adapter_status"] = "partial"
            result["error"] = {"code": "out_of_policy_edit", "message": "Candidate edited paths outside allowed scope"}
            return result
        result["adapter_status"] = "partial"
        if final["status"] != "done" or final["blockers"] or final["decisions"]:
            return result
        validation = result["validation"]
        for index, command in enumerate(commands, start=1):
            remaining = request["deadline_s"] - (time.monotonic() - started)
            if remaining <= 0 or (cancel_event and cancel_event.is_set()):
                validation["state"] = "incomplete" if validation["run"] else "not_run"
                break
            stdout = artifact / f"validation-{index:02}.stdout.log"
            stderr = artifact / f"validation-{index:02}.stderr.log"
            checked = run_owned(
                command["argv"], cwd=workspace, env=report["environment"], stdin=None,
                stdout_path=stdout, stderr_path=stderr,
                timeout_s=min(command["timeout_s"], remaining), max_output_bytes=request["output_bytes"],
                cancel_event=cancel_event,
            )
            logs = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in (stdout, stderr)
                             if path.exists())
            validation["commands"].append({"argv": command["argv"], "transport": asdict(checked),
                                           "stdout": str(stdout), "stderr": str(stderr),
                                           "summary": summarize_diagnostics(logs)})
            validation["run"] += 1
            passed = (checked.launched and checked.exit_code == 0 and checked.cleanup_verified and
                      not checked.timed_out and not checked.cancelled and not checked.output_limit and
                      not checked.error)
            validation["state"] = "passed" if passed else "failed"
            if not passed:
                break
        if commands:
            _assert_binding(workspace, before, request, git_before, binding_before)
            current = _snapshot(workspace)
            _write_json(artifact / "source-after-validation.json", current)
            if current != after:
                validation["state"] = "stale"
            elif validation["run"] < len(commands) and validation["state"] == "passed":
                validation["state"] = "incomplete"
            if validation["state"] == "passed" and request["validation"]["authoritative"]:
                result["adapter_status"] = "completed"
            elif validation["state"] in {"failed", "stale"}:
                result["adapter_status"] = "validation_failed"
        return result
    except (RequestError, PolicyError, SourceError) as exc:
        result["adapter_status"] = "failed_protocol" if inference_started else "rejected_preflight"
        result["error"] = {"code": exc.code, "message": str(exc)}
        return result
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["adapter_status"] = "failed_transport" if inference_started else "rejected_preflight"
        result["error"] = {"code": "adapter_io", "message": type(exc).__name__}
        return result
    finally:
        result["duration_s"] = round(time.monotonic() - started, 6)
        if artifact is not None:
            try:
                _write_json(artifact / "result.json", result)
            except OSError:
                result["adapter_status"] = "failed_transport"
                result["error"] = {"code": "evidence_write", "message": "Could not preserve result evidence"}
