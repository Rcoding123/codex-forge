from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ForgeConfig, load_config
from .db import ForgeDB
from .gitops import clone_repository, verify_repository
from .models import Project, Task, TaskState
from .orchestrator import Orchestrator
from .runner import _build_process_command
from .utils import command_exists, ensure_dir, run_process, stable_id


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge", description="Autonomous Codex engineering controller")
    parser.add_argument("--config", help="Path to forge.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize directories and database")
    sub.add_parser("doctor", help="Check the VPS and toolchain")

    project = sub.add_parser("project", help="Manage projects")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_add = project_sub.add_parser("add", help="Register or clone a project")
    project_add.add_argument("name")
    source = project_add.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="Existing repository path")
    source.add_argument("--url", help="Git URL to clone")
    project_add.add_argument("--default-branch", default="main")
    project_add.add_argument("--validation", action="append", default=[])
    project_add.add_argument("--no-fetch", action="store_true")
    project_add.add_argument("--no-commit", action="store_true")
    project_add.add_argument("--push", action="store_true")
    project_sub.add_parser("list", help="List projects")

    task = sub.add_parser("task", help="Manage tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_add = task_sub.add_parser("add", help="Add a task")
    task_add.add_argument("--project", required=True)
    task_add.add_argument("--title", required=True)
    task_add.add_argument("--objective", required=True)
    task_add.add_argument("--acceptance", action="append", default=[])
    task_add.add_argument("--validation", action="append", default=[])
    task_add.add_argument("--tag", action="append", default=[])
    task_add.add_argument("--depends-on", action="append", default=[])
    task_add.add_argument("--risk", choices=["low", "medium", "high", "critical"], default="medium")
    task_add.add_argument("--priority", type=int, default=100)
    task_add.add_argument("--max-attempts", type=int, default=3)
    task_add.add_argument("--id")
    task_list = task_sub.add_parser("list", help="List tasks")
    task_list.add_argument("--state", choices=[state.value for state in TaskState])
    task_retry = task_sub.add_parser("retry", help="Retry a failed or blocked task")
    task_retry.add_argument("task_id")

    run = sub.add_parser("run", help="Run ready tasks")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--daemon", action="store_true")

    status = sub.add_parser("status", help="Show queue status")
    status.add_argument("--json", action="store_true")

    decision = sub.add_parser("decision", help="View decisions requiring a person")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)
    decision_sub.add_parser("list")

    return parser


def load_or_exit(path: str | None) -> ForgeConfig:
    try:
        return load_config(path)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_init(config: ForgeConfig) -> int:
    for path in (
        config.workspace_root,
        config.data_root,
        config.repository_root,
        config.inbox_root,
        config.evidence_root,
        config.knowledge_root,
    ):
        ensure_dir(path)
    ForgeDB(config.database_path).initialize()
    print(f"Codex Forge initialized. Database: {config.database_path}")
    return 0


def cmd_doctor(config: ForgeConfig) -> int:
    codex_available = command_exists(config.codex_executable)
    codex_authenticated = False
    if codex_available:
        try:
            codex_authenticated = (
                run_process(
                    _build_process_command(config.codex_executable, ["login", "status"]), timeout=30
                ).returncode
                == 0
            )
        except Exception:
            codex_authenticated = False
    checks = {
        "git": command_exists("git"),
        "python": bool(sys.executable) and Path(sys.executable).is_file(),
        "codex": codex_available,
        "codex_auth": codex_authenticated,
        "rtk": command_exists("rtk"),
        "rg": command_exists("rg"),
        "fd": command_exists("fd"),
        "ast-grep": command_exists("ast-grep") or command_exists("sg"),
        "PowerShell": command_exists("pwsh") or command_exists("powershell"),
        "config": config.config_path.exists(),
        "data_root": config.data_root.exists(),
        "database": config.database_path.exists(),
    }
    for name, ok in checks.items():
        print(f"[{'OK' if ok else 'MISSING'}] {name}")
    required = [
        "git",
        "python",
        "codex",
        "codex_auth",
        "PowerShell",
        "config",
        "data_root",
        "database",
    ]
    missing = [name for name in required if not checks[name]]
    if missing:
        print(f"Required checks failed: {', '.join(missing)}", file=sys.stderr)
        return 1
    print("Forge is operational and Codex authentication is ready for unattended tasks.")
    return 0


def cmd_project_add(config: ForgeConfig, args: argparse.Namespace) -> int:
    db = ForgeDB(config.database_path)
    db.initialize()
    if args.url:
        repo_path = config.repository_root / args.name
        if not repo_path.exists():
            clone_repository(args.url, repo_path)
    else:
        repo_path = Path(args.repo).expanduser().resolve()
    verify_repository(repo_path)
    project = Project(
        id=stable_id("project", args.name),
        name=args.name,
        repo_path=repo_path,
        default_branch=args.default_branch,
        validation_commands=args.validation,
        auto_fetch=not args.no_fetch,
        auto_commit=not args.no_commit,
        auto_push=args.push,
    )
    db.add_project(project)
    print(f"Registered project {project.name}: {project.repo_path}")
    return 0


def cmd_project_list(config: ForgeConfig) -> int:
    db = ForgeDB(config.database_path)
    for project in db.list_projects():
        print(f"{project.name}\t{project.repo_path}\tbase={project.default_branch}")
    return 0


def cmd_task_add(config: ForgeConfig, args: argparse.Namespace) -> int:
    db = ForgeDB(config.database_path)
    project = db.get_project(args.project)
    if not project:
        print(f"Unknown project: {args.project}", file=sys.stderr)
        return 2
    task_id = args.id or stable_id("TASK", f"{project.id}:{args.title}:{args.objective}")
    dependencies = args.depends_on or []
    task = Task(
        id=task_id,
        project_id=project.id,
        title=args.title,
        objective=args.objective,
        acceptance=args.acceptance,
        validation_commands=args.validation,
        state=TaskState.BACKLOG if dependencies else TaskState.READY,
        priority=args.priority,
        risk=args.risk,
        tags=args.tag,
        dependencies=dependencies,
        max_attempts=args.max_attempts,
    )
    db.add_task(task)
    print(f"Added {task.id} [{task.state.value}] {task.title}")
    return 0


def cmd_task_list(config: ForgeConfig, args: argparse.Namespace) -> int:
    db = ForgeDB(config.database_path)
    state = TaskState(args.state) if args.state else None
    for task in db.list_tasks(state):
        print(f"{task.id}\t{task.state.value}\tP{task.priority}\t{task.title}")
    return 0


def cmd_task_retry(config: ForgeConfig, task_id: str) -> int:
    db = ForgeDB(config.database_path)
    task = db.get_task(task_id)
    if not task:
        print(f"Unknown task: {task_id}", file=sys.stderr)
        return 2
    if task.attempts >= task.max_attempts:
        print(
            f"Task {task_id} exhausted its attempt limit ({task.attempts}/{task.max_attempts}).",
            file=sys.stderr,
        )
        return 1
    db.update_task(task_id, state=TaskState.READY)
    print(f"Task {task_id} returned to READY.")
    return 0


def cmd_status(config: ForgeConfig, as_json: bool) -> int:
    db = ForgeDB(config.database_path)
    tasks = db.list_tasks()
    counts = {state.value: 0 for state in TaskState}
    for task in tasks:
        counts[task.state.value] += 1
    decisions = db.list_open_decisions()
    payload = {"tasks": counts, "open_decisions": len(decisions), "database": str(config.database_path)}
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(" | ".join(f"{name}={count}" for name, count in counts.items() if count))
        print(f"OPEN_DECISIONS={len(decisions)}")
    return 0


def cmd_decision_list(config: ForgeConfig) -> int:
    db = ForgeDB(config.database_path)
    rows = db.list_open_decisions()
    if not rows:
        print("No open decisions.")
        return 0
    for row in rows:
        print(f"Decision {row['id']} for {row['task_id']}: {row['question']}")
        for option in json.loads(row["options_json"]):
            print(f"  - {option}")
        if row["recommendation"]:
            print(f"  Recommended: {row['recommendation']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_or_exit(args.config)

    if args.command == "init":
        return cmd_init(config)
    if args.command == "doctor":
        return cmd_doctor(config)
    if args.command == "project":
        if args.project_command == "add":
            return cmd_project_add(config, args)
        return cmd_project_list(config)
    if args.command == "task":
        if args.task_command == "add":
            return cmd_task_add(config, args)
        if args.task_command == "list":
            return cmd_task_list(config, args)
        return cmd_task_retry(config, args.task_id)
    if args.command == "status":
        return cmd_status(config, args.json)
    if args.command == "decision":
        return cmd_decision_list(config)
    if args.command == "run":
        orchestrator = Orchestrator(config, package_root())
        if args.daemon:
            orchestrator.run_daemon()
            return 0
        return 0 if orchestrator.run_once() >= 0 else 1
    parser.error("Unknown command")
    return 2
