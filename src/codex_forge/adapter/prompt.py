"""Read one terminal task and submit it through Codex Forge."""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
BIN = HOME / ".local" / "bin"
STATE = HOME / ".local" / "state" / "adapter-launchers"


def git(cwd, *args):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "-C", str(cwd), *args],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git could not read the checkout.")
    return result.stdout


def forge_checkout(cwd):
    return Path(git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()


def forge_job(workspace, job, task, args):
    from codex_forge.adapter.engine import SourceError, capture_checkout

    try:
        source = capture_checkout(workspace)
    except SourceError as exc:
        raise ValueError(str(exc)) from exc
    request = {
        "contract_version": "adapter/1",
        "caller_id": "forge-terminal",
        "job_id": job.name,
        "attempt_id": "a1",
        "role": "implementation",
        "workspace": source,
        "task": {
            "objective": task,
            "acceptance": ["Satisfy the requested task and report any checks you ran."],
            "constraints": [
                "Edit only what the task requires and preserve unrelated staged, unstaged, and untracked changes.",
                "Keep edits inside this checkout. Do not stage, stash, reset, commit, or push.",
                "If the requested edit conflicts with unrelated user work, report the conflict and stop.",
                "Summarize choices already made. Reserve decisions for unresolved questions for the user.",
            ],
            "scope": ["Project files needed for the task"],
            "out_of_scope": [],
        },
        "policy": {
            "sandbox": "danger-full-access", "allowed_paths": ["*"], "tool_network": "unspecified",
            "search": False, "mcp": False, "plugins": False,
        },
        "context": [],
        "model": args.model,
        "effort": "medium",
        "deadline_s": 900,
        "artifact_root": str(job / "evidence"),
    }
    return [str(args.adapter), "run", "--request", "-"], json.dumps(request)


def report(result, returncode):
    handoff = result.get("handoff") or {}
    audit = result.get("edit_audit") or {}
    candidate = (
        result.get("adapter_status") == "partial" and audit.get("passed") is True
        and (result.get("protocol") or {}).get("schema_valid") is True
        and (result.get("semantic") or {}).get("status") == "done"
        and not handoff.get("blockers") and not handoff.get("decisions")
        and not result.get("error")
    )
    if handoff.get("summary"):
        print(handoff["summary"])
    if candidate:
        print("Forge returned a candidate. No independent project test was configured.")
        return 0
    error = result.get("error") or {}
    reasons = handoff.get("blockers", []) + handoff.get("decisions", [])
    print(error.get("message") or "; ".join(reasons) or result.get("adapter_status"), file=sys.stderr)
    return returncode or 1


def read_prompt(label):
    if sys.stdin.isatty():
        return input(label + " ").strip()
    print(label, file=sys.stderr)
    return input().strip()


def main():
    parser = argparse.ArgumentParser(description="Run one coding task in the current checkout through Codex Forge.")
    parser.add_argument("--adapter", type=Path, default=BIN / "forge-adapter",
                        help="Absolute path to the configured adapter launcher.")
    parser.add_argument("--model", default="gpt-5.6-sol", help="Exact native model ID.")
    args = parser.parse_args()
    os.umask(0o077)
    cwd = Path.cwd()
    if cwd == HOME:
        value = read_prompt("Project directory:")
        if not value:
            return 0
        cwd = Path(value).expanduser().resolve(strict=True)
    if not cwd.is_dir() or STATE.resolve().is_relative_to(cwd):
        raise ValueError("Choose a project directory outside the adapter state directory's ancestors.")
    if not args.adapter.is_absolute():
        raise ValueError("Pass an absolute adapter launcher path.")
    cwd = forge_checkout(cwd)
    if STATE.resolve().is_relative_to(cwd):
        raise ValueError("Choose a Git project outside the adapter state directory's ancestors.")

    task = read_prompt("Task (one line):")
    if not task:
        return 0
    if len(task.encode("utf-8")) > 24_000:
        raise ValueError("Use at most 24,000 UTF-8 bytes in the task prompt.")

    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    locks = STATE / "locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    lock_id = hashlib.sha256(os.fsencode(cwd)).hexdigest()
    with (locks / lock_id).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("A Codex task is already running in this checkout.") from exc
        return run_task(cwd, task, args)


def run_task(cwd, task, args):
    job = Path(tempfile.mkdtemp(prefix="codex-", dir=STATE))
    argv, data = forge_job(cwd, job, task, args)
    print(f"Codex Forge workspace: {cwd}", flush=True)
    print(f"Run files: {job}", flush=True)
    result_path = job / "adapter-result.json"
    with result_path.open("x", encoding="utf-8") as output:
        proc = subprocess.run(argv, input=data, stdout=output, encoding="utf-8", check=False)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    return report(result, proc.returncode)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Adapter launcher: {exc}", file=sys.stderr)
        sys.exit(1)
