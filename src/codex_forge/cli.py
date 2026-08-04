from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ForgeConfig, load_config
from .control_plane import (
    StructuredError,
    decision_to_public,
    error_exit_code,
    project_to_public,
    task_to_public,
    write_response,
)
from .db import ForgeDB
from .gitops import clone_repository, verify_repository
from .models import Project, Task, TaskState
from .orchestrator import Orchestrator
from .runner import _build_process_command
from .task_submission import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_PRIORITY,
    DEFAULT_RISK,
    RISK_LEVELS,
    SUBMISSION_COMMAND,
    TaskSubmissionError,
    build_task_request,
    submit_task,
)
from .utils import command_exists, ensure_dir, run_process, stable_id


class CLIArgumentError(ValueError):
    pass


class ForgeArgumentParser(argparse.ArgumentParser):
    structured_errors = False

    def error(self, message: str) -> None:
        if self.structured_errors:
            raise CLIArgumentError(message)
        super().error(message)


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = ForgeArgumentParser(prog="forge", description="Autonomous Codex engineering controller")
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
    project_list = project_sub.add_parser("list", help="List projects")
    project_list.add_argument("--json", action="store_true")

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
    # These three default to None so the contract can report which values it resolved. The
    # human-readable path applies the same defaults explicitly.
    task_add.add_argument("--risk", choices=list(RISK_LEVELS), default=None)
    task_add.add_argument("--priority", type=int, default=None)
    task_add.add_argument("--max-attempts", type=int, default=None)
    task_add.add_argument("--id")
    task_add.add_argument(
        "--dry-run", action="store_true", help="Validate the request without writing to the database"
    )
    task_add.add_argument("--json", action="store_true")
    task_list = task_sub.add_parser("list", help="List tasks")
    task_list.add_argument("--state", choices=[state.value for state in TaskState])
    task_list.add_argument("--json", action="store_true")
    task_show = task_sub.add_parser("show", help="Show one task")
    task_show.add_argument("task_id")
    task_show.add_argument("--json", action="store_true")
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
    decision_list = decision_sub.add_parser("list")
    decision_list.add_argument("--json", action="store_true")

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


def cmd_project_list(config: ForgeConfig, as_json: bool = False) -> int:
    db = ForgeDB(config.database_path)
    projects = db.list_projects()
    if as_json:
        write_response(
            "project.list",
            data={"projects": [project_to_public(project) for project in projects]},
        )
        return 0
    for project in projects:
        print(f"{project.name}\t{project.repo_path}\tbase={project.default_branch}")
    return 0


def cmd_task_add(config: ForgeConfig, args: argparse.Namespace) -> int:
    if getattr(args, "json", False) or getattr(args, "dry_run", False):
        return cmd_task_submit(config, args)
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
        priority=DEFAULT_PRIORITY if args.priority is None else args.priority,
        risk=DEFAULT_RISK if args.risk is None else args.risk,
        tags=args.tag,
        dependencies=dependencies,
        max_attempts=DEFAULT_MAX_ATTEMPTS if args.max_attempts is None else args.max_attempts,
    )
    db.add_task(task)
    print(f"Added {task.id} [{task.state.value}] {task.title}")
    return 0


def cmd_task_submit(config: ForgeConfig, args: argparse.Namespace) -> int:
    """Handle `task add --json` and `task add --dry-run` through the versioned contract."""
    as_json = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)
    try:
        request = build_task_request(args)
        data = submit_task(config, request, dry_run=dry_run)
    except TaskSubmissionError as exc:
        if as_json:
            print(f"ERROR: {exc.code}", file=sys.stderr)
            write_response(
                SUBMISSION_COMMAND,
                error=StructuredError(code=exc.code, message=exc.message, details=exc.details),
            )
        else:
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return error_exit_code(exc.code)
    if as_json:
        write_response(SUBMISSION_COMMAND, data=data)
        return 0
    task = data["task"]
    if dry_run:
        verb = "Would add" if data["would_create"] else "Already present:"
    else:
        verb = "Added" if data["created"] else "Already present:"
    print(f"{verb} {task['task_id']} [{task['state']}] {task['title']}")
    return 0


def cmd_task_list(config: ForgeConfig, args: argparse.Namespace) -> int:
    db = ForgeDB(config.database_path)
    state = TaskState(args.state) if args.state else None
    tasks = db.list_tasks(state)
    if getattr(args, "json", False):
        projects = {project.id: project.name for project in db.list_projects()}
        write_response(
            "task.list",
            data={
                "tasks": [
                    task_to_public(
                        task,
                        project_name=projects.get(task.project_id),
                        token_usage=db.task_token_usage(task.id),
                        token_limit=config.max_task_tokens,
                        runtime=db.task_runtime(task.id),
                    )
                    for task in tasks
                ],
                "filters": {"state": args.state},
            },
        )
        return 0
    for task in tasks:
        print(f"{task.id}\t{task.state.value}\tP{task.priority}\t{task.title}")
    return 0


def cmd_task_show(config: ForgeConfig, task_id: str, as_json: bool = False) -> int:
    db = ForgeDB(config.database_path)
    task = db.get_task(task_id)
    if not task:
        if as_json:
            write_response(
                "task.show",
                error=StructuredError(
                    code="TASK_NOT_FOUND",
                    message="Task was not found.",
                    details={"task_id": task_id},
                ),
            )
        else:
            print(f"Unknown task: {task_id}", file=sys.stderr)
        return 2
    if as_json:
        project = db.get_project(task.project_id)
        write_response(
            "task.show",
            data={
                "task": task_to_public(
                    task,
                    project_name=project.name if project else None,
                    token_usage=db.task_token_usage(task.id),
                    token_limit=config.max_task_tokens,
                    runtime=db.task_runtime(task.id),
                )
            },
        )
        return 0
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
        write_response("status", data=payload, legacy_fields=payload)
    else:
        print(" | ".join(f"{name}={count}" for name, count in counts.items() if count))
        print(f"OPEN_DECISIONS={len(decisions)}")
    return 0


def cmd_decision_list(config: ForgeConfig, as_json: bool = False) -> int:
    db = ForgeDB(config.database_path)
    rows = db.list_open_decisions()
    if as_json:
        write_response(
            "decision.list",
            data={"decisions": [decision_to_public(row) for row in rows]},
        )
        return 0
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


def _requested_command(argv: list[str]) -> str:
    tokens = list(argv)
    if "--config" in tokens:
        index = tokens.index("--config")
        del tokens[index : index + 2]
    words = [token for token in tokens if not token.startswith("-")]
    if not words:
        return "unknown"
    if words[0] in {"project", "task", "decision"} and len(words) > 1:
        return f"{words[0]}.{words[1]}"
    return words[0]


def _write_cli_error(command: str, code: str, message: str, details: dict | None = None) -> None:
    write_response(
        command,
        error=StructuredError(code=code, message=message, details=details or {}),
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    json_requested = "--json" in raw_argv
    ForgeArgumentParser.structured_errors = json_requested
    parser = build_parser()
    try:
        args = parser.parse_args(raw_argv)
    except CLIArgumentError as exc:
        if json_requested:
            _write_cli_error(
                _requested_command(raw_argv),
                "INVALID_ARGUMENT",
                "Command arguments were invalid.",
            )
        else:
            parser.print_usage(sys.stderr)
            print(f"forge: error: {exc}", file=sys.stderr)
        return 2
    finally:
        ForgeArgumentParser.structured_errors = False

    try:
        config = load_config(args.config)
    except Exception as exc:
        if json_requested:
            print(f"ERROR: {type(exc).__name__}: Forge configuration could not be loaded.", file=sys.stderr)
            _write_cli_error(
                _requested_command(raw_argv),
                "CONFIG_ERROR",
                "Forge configuration could not be loaded.",
            )
            return 2
        return load_or_exit(args.config)

    try:
        if args.command == "init":
            return cmd_init(config)
        if args.command == "doctor":
            return cmd_doctor(config)
        if args.command == "project":
            if args.project_command == "add":
                return cmd_project_add(config, args)
            return cmd_project_list(config, args.json)
        if args.command == "task":
            if args.task_command == "add":
                return cmd_task_add(config, args)
            if args.task_command == "list":
                return cmd_task_list(config, args)
            if args.task_command == "show":
                return cmd_task_show(config, args.task_id, args.json)
            return cmd_task_retry(config, args.task_id)
        if args.command == "status":
            return cmd_status(config, args.json)
        if args.command == "decision":
            return cmd_decision_list(config, args.json)
        if args.command == "run":
            orchestrator = Orchestrator(config, package_root())
            if args.daemon:
                orchestrator.run_daemon()
                return 0
            return 0 if orchestrator.run_once() >= 0 else 1
        raise CLIArgumentError("Unknown command")
    except Exception as exc:
        if json_requested:
            print(f"ERROR: {type(exc).__name__}: command failed.", file=sys.stderr)
            _write_cli_error(
                _requested_command(raw_argv),
                "COMMAND_FAILED",
                "The command could not be completed.",
            )
            return 1
        raise
