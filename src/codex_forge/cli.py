from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from .config import ForgeConfig, load_config
from .control_plane import (
    DEFAULT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    StructuredError,
    UnsupportedSchemaVersion,
    decision_to_public,
    error_exit_code,
    project_to_public,
    resolve_schema_version,
    task_to_public,
    unsupported_schema_error,
    write_response,
)
from .db import (
    ACTIVE_TASK_STATES,
    ForgeDB,
    ProjectAlreadyRegistered,
    RepositoryAlreadyRegistered,
)
from .evidence import (
    EVIDENCE_LIST_COMMAND,
    EVIDENCE_SHOW_COMMAND,
    EvidenceError,
    evidence_list,
    evidence_show,
    safe_path_component,
    task_evidence_root,
)
from .gitops import (
    GitError,
    clone_repository,
    ensure_disposable_checkout,
    pin_plain_directory,
    reconcile_missing_workspace,
    remove_registered_checkouts,
    remove_worktree,
    revalidate_pinned_directory,
    verify_repository,
)
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
    _normalize_base_commit,
    _normalize_expected_registration,
    _normalize_task_id,
    admit_task,
    build_task_request,
    require_expected_registration,
    resolve_base_commit,
    submit_task,
    task_conflict_error,
    verify_expected_registration,
)
from .utils import (
    command_exists,
    derived_worktree_path,
    ensure_dir,
    run_process,
    stable_id,
    windows_alias_key,
    controller_lock,
    ControllerAlreadyRunning,
)
from .workflows import ProjectWorkflows


class CLIArgumentError(ValueError):
    pass


class _ProjectNameCollision(Exception):
    """A project name that differs only by case from a registered one, caught in-transaction."""

    def __init__(self, existing: str):
        super().__init__(existing)
        self.existing = existing


class ForgeArgumentParser(argparse.ArgumentParser):
    structured_errors = False

    def error(self, message: str) -> None:
        if self.structured_errors:
            raise CLIArgumentError(message)
        super().error(message)


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _add_schema_version_argument(parser: argparse.ArgumentParser) -> None:
    """Let a caller pin the contract version a command's JSON response declares.

    Omitting it keeps the response on the default version, so an installed consumer keeps receiving
    exactly the contract it was built against.
    """
    parser.add_argument(
        "--schema-version",
        default=None,
        help=f"Contract version to emit ({' or '.join(SUPPORTED_SCHEMA_VERSIONS)}).",
    )


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
    project_relocate = project_sub.add_parser(
        "relocate",
        help="Point an existing project registration at a different local repository path",
        description=(
            "Move one registration to a different local path for the same repository. The operator "
            "asserts that it is the same repository; Forge verifies only that the target is a Git "
            "working tree that no other registration claims, and refuses while a task of this "
            "project is active. The repository registration identity, the project id, the name and "
            "the default branch are all preserved. Binding a project to a different repository is "
            "not a relocation: delete the project and register it again, which mints a new "
            "repository registration identity."
        ),
    )
    project_relocate.add_argument("project", help="Project id or exact name")
    project_relocate.add_argument("--repo", required=True, help="New repository path")
    _add_schema_version_argument(project_relocate)
    project_relocate.add_argument("--json", action="store_true")
    project_delete = project_sub.add_parser(
        "delete",
        help="Delete a project that has no tasks; evidence and repositories are retained",
        description=(
            "Delete an empty project from the Forge registry. A project that still has tasks is "
            "refused; delete its tasks explicitly first. Retained task evidence and the project's "
            "repository (cloned or user-supplied) are never deleted. Only the project's disposable "
            "worktree directory is removed."
        ),
    )
    project_delete.add_argument("project", help="Project id or exact name")
    _add_schema_version_argument(project_delete)
    project_delete.add_argument("--json", action="store_true")
    project_list = project_sub.add_parser("list", help="List projects")
    _add_schema_version_argument(project_list)
    project_list.add_argument("--json", action="store_true")
    for action in ("plan", "extend", "progress", "resume"):
        command = project_sub.add_parser(action, help={
            "plan": "Install an immutable project integration and phase plan",
            "extend": "Continue from a completed plan with another immutable phase plan",
            "progress": "Show project integration and phase progress",
            "resume": "Resume a blocked project plan after resolving its cause",
        }[action])
        command.add_argument("project", help="Project id or exact name")
        if action in ("plan", "extend"):
            command.add_argument("--file", required=True, help="JSON plan definition")
        _add_schema_version_argument(command)
        command.add_argument("--json", action="store_true")

    task = sub.add_parser("task", help="Manage tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_add = task_sub.add_parser("add", help="Add a task")
    task_add.add_argument("--project", required=True)
    task_add.add_argument("--title", required=True)
    task_add.add_argument("--objective", required=True)
    task_add.add_argument("--acceptance", action="append", default=[])
    task_add.add_argument("--decision", action="append", default=[])
    task_add.add_argument("--constraint", action="append", default=[])
    task_add.add_argument("--scope")
    task_add.add_argument("--out-of-scope")
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
        "--base-commit",
        help=(
            "Exact Git commit this task's execution lineage is admitted from. Must be a full "
            "commit object id in the project's registered repository. Omit to keep Forge's "
            "existing base selection."
        ),
    )
    task_add.add_argument(
        "--expected-repository-registration",
        help=(
            "The project's repository_registration_id this task is bound to. Admission is refused "
            "with REPOSITORY_REGISTRATION_MISMATCH unless the project still carries exactly this "
            "registration. Required at --schema-version 1.4 and later; always enforced when given."
        ),
    )
    task_add.add_argument(
        "--dry-run", action="store_true", help="Validate the request without writing to the database"
    )
    _add_schema_version_argument(task_add)
    task_add.add_argument("--json", action="store_true")
    task_list = task_sub.add_parser("list", help="List tasks")
    task_list.add_argument("--state", choices=[state.value for state in TaskState])
    _add_schema_version_argument(task_list)
    task_list.add_argument("--json", action="store_true")
    task_show = task_sub.add_parser("show", help="Show one task")
    task_show.add_argument("task_id")
    _add_schema_version_argument(task_show)
    task_show.add_argument("--json", action="store_true")
    task_retry = task_sub.add_parser("retry", help="Retry a failed or blocked task")
    task_retry.add_argument("task_id")
    blocker = task_sub.add_parser("resolve-blocker", help="Continue a closed model blocker using an unused existing repair slot")
    blocker.add_argument("task_id")
    blocker.add_argument("--reason", required=True)
    blocker.add_argument("--receipt", type=Path, required=True)
    blocker.add_argument("--receipt-sha256", required=True)
    _add_schema_version_argument(blocker)
    blocker.add_argument("--json", action="store_true")
    handoff = task_sub.add_parser("handoff-prerequisite", help="Import a gated prerequisite and continue one sealed model blocker")
    handoff.add_argument("task_id")
    handoff.add_argument("--reason", required=True)
    handoff.add_argument("--receipt", type=Path)
    handoff.add_argument("--receipt-sha256")
    handoff.add_argument("--dry-run", action="store_true")
    for pin in ("source-task", "source-registration", "source-candidate", "workflow-id", "gate-id", "gate-candidate"):
        handoff.add_argument("--" + pin)
    _add_schema_version_argument(handoff)
    handoff.add_argument("--json", action="store_true")
    finalization = task_sub.add_parser("finalize-prerequisite", help="Replace a terminal prerequisite and finalize with one review, no repairs")
    finalization.add_argument("task_id")
    finalization.add_argument("--reason", required=True)
    finalization.add_argument("--attestation")
    finalization.add_argument("--parent-continuation")
    finalization.add_argument("--receipt", type=Path)
    finalization.add_argument("--receipt-sha256")
    finalization.add_argument("--dry-run", action="store_true")
    for pin in ("source-task", "source-registration", "source-candidate", "workflow-id", "gate-id", "gate-candidate"):
        finalization.add_argument("--" + pin)
        finalization.add_argument("--capability-" + pin)
    finalization.add_argument("--selection-manifest")
    _add_schema_version_argument(finalization)
    finalization.add_argument("--json", action="store_true")
    active_states = ", ".join(state.value for state in ACTIVE_TASK_STATES)
    task_delete = task_sub.add_parser(
        "delete",
        help="Delete a non-running task; evidence and the task branch are retained",
        description=(
            "Delete an obsolete task from the Forge registry. A task in an active execution state "
            f"({active_states}) is refused; deletion is not cancellation. Retained evidence and "
            "the task branch are never deleted. Only the task's disposable worktree checkout is "
            "removed."
        ),
    )
    task_delete.add_argument("task_id", help="Exact task id")
    _add_schema_version_argument(task_delete)
    task_delete.add_argument("--json", action="store_true")

    task_evidence = task_sub.add_parser("evidence", help="Inspect retained task evidence")
    evidence_sub = task_evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_list = evidence_sub.add_parser("list", help="List publishable evidence artifacts")
    evidence_list.add_argument("task_id")
    _add_schema_version_argument(evidence_list)
    evidence_list.add_argument("--json", action="store_true")
    evidence_show = evidence_sub.add_parser("show", help="Show one evidence artifact")
    evidence_show.add_argument("task_id")
    evidence_show.add_argument(
        "--artifact-id", required=True, help="Opaque artifact identifier from `task evidence list`"
    )
    _add_schema_version_argument(evidence_show)
    evidence_show.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="Run ready tasks")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--daemon", action="store_true")

    status = sub.add_parser("status", help="Show queue status")
    _add_schema_version_argument(status)
    status.add_argument("--json", action="store_true")

    decision = sub.add_parser("decision", help="View decisions requiring a person")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)
    decision_list = decision_sub.add_parser("list")
    _add_schema_version_argument(decision_list)
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
    # A project name is a workspace and evidence path component; it must never be able to escape
    # those roots, and two names differing only by case would alias one directory on Windows.
    if not safe_path_component(args.name):
        print(
            "INVALID_ARGUMENT: a project name must be a single safe path component "
            "(no separators, colons, or dot navigation).",
            file=sys.stderr,
        )
        return 2
    registered = db.list_projects()
    # Registration creates; it never replaces one. A second `project add` for a project that is
    # already registered would otherwise silently overwrite its repository path, its default branch
    # and its policies, which is exactly the replacement the registration identity exists to stop.
    # Checked before any clone so a refused registration never leaves a repository behind.
    identity = stable_id("project", args.name)
    occupied = next(
        (
            existing
            for existing in registered
            if existing.name == args.name or existing.id == identity
        ),
        None,
    )
    if occupied is not None:
        return _project_exists_refusal(occupied)
    variant = next(
        (
            existing
            for existing in registered
            if existing.name != args.name
            and windows_alias_key(existing.name) == windows_alias_key(args.name)
        ),
        None,
    )
    if variant is not None:
        print(
            f"INVALID_ARGUMENT: project name {args.name!r} differs only by case from "
            f"{variant.name!r}; the two would alias one evidence and workspace directory.",
            file=sys.stderr,
        )
        return 2
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
    def _name_guard(connection) -> None:
        for row in connection.execute("SELECT name FROM projects"):
            if row["name"] != project.name and windows_alias_key(row["name"]) == windows_alias_key(
                project.name
            ):
                raise _ProjectNameCollision(row["name"])

    try:
        project = db.add_project_guarded(project, _name_guard)
    except _ProjectNameCollision as exc:
        print(
            f"INVALID_ARGUMENT: project name {project.name!r} differs only by case from "
            f"{exc.existing!r}; the two would alias one evidence and workspace directory.",
            file=sys.stderr,
        )
        return 2
    except ProjectAlreadyRegistered as exc:
        # The pre-check above already refused the ordinary case; this closes the window between it
        # and the insert, under the same transaction that performs the insert.
        return _project_exists_refusal(exc.existing)
    except RepositoryAlreadyRegistered as exc:
        return _structured_failure(
            "project.add",
            StructuredError(
                code="REPOSITORY_ALREADY_REGISTERED",
                message=(
                    "That repository path is already registered to another project; one "
                    "repository path is one registration."
                ),
                details={"registered_to": _project_detail_name(exc.existing)},
            ),
            False,
        )
    print(f"Registered project {project.name}: {project.repo_path}")
    print(f"Repository registration id: {project.repository_registration_id}")
    return 0


def _project_exists_refusal(existing: Project) -> int:
    """Refuse a registration that would replace an existing one, naming what already holds it."""
    return _structured_failure(
        "project.add",
        StructuredError(
            code="PROJECT_EXISTS",
            message=(
                "That project is already registered; registration never replaces a repository, a "
                "default branch or a policy. Use `project relocate` to move the same repository "
                "registration to a new path, or delete the project and register it again, which "
                "mints a new repository registration identity."
            ),
            details={"project": _project_detail_name(existing)},
        ),
        False,
    )


def cmd_project_relocate(
    config: ForgeConfig, args: argparse.Namespace, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    """Move one registration to a new local path for the same repository, keeping its identity.

    The target is verified to be a Git working tree before the registry is touched, so a bad path
    cannot leave a registration pointing at something that is not a repository. Forge does not try
    to prove that it is the same repository -- no local repository identity survives every
    legitimate relocation, and a repository without a remote has none to compare -- so relocation
    is the operator's explicit assertion, and the immutable registration identity, not an inferred
    fingerprint, stays the authority.
    """
    as_json = getattr(args, "json", False)
    db = ForgeDB(config.database_path)
    db.initialize()
    repo_path = Path(args.repo).expanduser().resolve()
    try:
        verify_repository(repo_path)
    except GitError:
        return _structured_failure(
            "project.relocate",
            StructuredError(
                code="INVALID_ARGUMENT",
                message="The relocation target is not an existing Git working tree.",
                details={"project": args.project},
            ),
            as_json,
            schema_version,
        )
    relocation = db.relocate_project(args.project, repo_path)
    if relocation.outcome == "not_found":
        return _structured_failure(
            "project.relocate",
            StructuredError(
                code="PROJECT_NOT_FOUND",
                message="Project was not found.",
                details={"project": args.project},
            ),
            as_json,
            schema_version,
        )
    if relocation.outcome == "ambiguous":
        return _structured_failure(
            "project.relocate",
            StructuredError(
                code="PROJECT_AMBIGUOUS",
                message=(
                    "The identifier names one project's id and a different project's name; "
                    "relocation refuses to guess."
                ),
                details={"identifier": args.project},
            ),
            as_json,
            schema_version,
        )
    if relocation.outcome == "path_claimed":
        return _structured_failure(
            "project.relocate",
            StructuredError(
                code="REPOSITORY_ALREADY_REGISTERED",
                message=(
                    "That repository path is already registered to another project; one "
                    "repository path is one registration."
                ),
                details={
                    "project": _project_detail_name(relocation.project),
                    "registered_to": _project_detail_name(relocation.conflict),
                },
            ),
            as_json,
            schema_version,
        )
    if relocation.outcome == "active_workflow":
        return _structured_failure(
            "project.relocate", StructuredError(
                "PROJECT_ACTIVE", "Project acceptance is running; the repository cannot be relocated.", {},
            ), as_json, schema_version,
        )
    if relocation.outcome == "active_tasks":
        return _structured_failure(
            "project.relocate",
            StructuredError(
                code="TASK_ACTIVE",
                message=(
                    "A task of this project is being executed; the repository underneath a live "
                    "execution is not relocatable."
                ),
                details={
                    "project": _project_detail_name(relocation.project),
                    "active_tasks": relocation.active_tasks,
                },
            ),
            as_json,
            schema_version,
        )
    project = relocation.project
    relocated = relocation.outcome == "relocated"
    if as_json:
        write_response(
            "project.relocate",
            data={
                "relocated": relocated,
                "project": project_to_public(project, schema_version),
            },
            schema_version=schema_version,
        )
    elif relocated:
        print(f"Relocated project {project.name} to {project.repo_path}")
    else:
        print(f"Project {project.name} is already registered at {project.repo_path}")
    return 0


def cmd_project_list(
    config: ForgeConfig, as_json: bool = False, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    db = ForgeDB(config.database_path)
    projects = db.list_projects()
    if as_json:
        write_response(
            "project.list",
            data={
                "projects": [
                    project_to_public(project, schema_version) for project in projects
                ]
            },
            schema_version=schema_version,
        )
        return 0
    for project in projects:
        print(f"{project.name}\t{project.repo_path}\tbase={project.default_branch}")
    return 0


def cmd_project_workflow(config: ForgeConfig, args: argparse.Namespace, schema_version: str) -> int:
    db = ForgeDB(config.database_path)
    db.initialize()
    project = db.get_project(args.project)
    command = f"project.{args.project_command}"
    if not project:
        return _structured_failure(
            command, StructuredError("PROJECT_NOT_FOUND", "Project was not found.", {}), args.json, schema_version,
        )
    workflows = ProjectWorkflows(config, db)
    try:
        if args.project_command == "progress":
            document = workflows.get(project.id)
        else:
            with controller_lock(config.data_root / "controller.lock"):
                if args.project_command in ("plan", "extend"):
                    definition = json.loads(Path(args.file).read_text(encoding="utf-8"))
                    document = workflows.install(project, definition, extend=args.project_command == "extend")
                else:
                    document = workflows.resume(project.id)
    except (ValueError, GitError, OSError) as exc:
        if not args.json:
            print(str(exc), file=sys.stderr)
        return _structured_failure(
            command, StructuredError("INVALID_ARGUMENT", "Project plan request was refused.", {}),
            args.json, schema_version,
        )
    data = {"project_id": project.id, "plan": workflows.public(document)}
    if args.json:
        write_response(command, data=data, schema_version=schema_version)
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def cmd_task_add(
    config: ForgeConfig, args: argparse.Namespace, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    if getattr(args, "json", False) or getattr(args, "dry_run", False):
        return cmd_task_submit(config, args, schema_version)
    try:
        # The same contract law the structured path applies, before the database is opened: a
        # declared 1.4 must state its generation, and an option argparse accepted is never ignored.
        expected_registration = _normalize_expected_registration(
            getattr(args, "expected_repository_registration", None)
        )
        require_expected_registration(expected_registration, schema_version)
    except TaskSubmissionError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return error_exit_code(exc.code)
    db = ForgeDB(config.database_path)
    project = db.get_project(args.project)
    if not project:
        print(f"Unknown project: {args.project}", file=sys.stderr)
        return 2
    try:
        # The caller's generation check precedes every other admission step, exactly as in
        # `submit_task`; the locked guard then holds this resolved snapshot to the insert.
        verify_expected_registration(project, expected_registration)
        task_id = _normalize_task_id(args.id) or stable_id(
            "TASK", f"{project.id}:{args.title}:{args.objective}"
        )
    except TaskSubmissionError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return error_exit_code(exc.code)
    try:
        # Only the base is normalized and resolved here. The human path deliberately does not run
        # the contract request validator, so adding an option must not start applying it.
        admission_base_commit = resolve_base_commit(
            project, _normalize_base_commit(getattr(args, "base_commit", None))
        )
    except TaskSubmissionError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return error_exit_code(exc.code)
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
        architecture_decisions=args.decision,
        constraints=args.constraint,
        scope=args.scope or "",
        out_of_scope=args.out_of_scope or "",
        max_attempts=DEFAULT_MAX_ATTEMPTS if args.max_attempts is None else args.max_attempts,
        admission_base_commit=admission_base_commit,
    )
    try:
        # The same durable law the contract path uses. Reaching the database directly here is what
        # let one entry point answer an already-taken id with a raw integrity error while the other
        # answered with a structured refusal; there is one seam and one answer now.
        admission = admit_task(
            db,
            config,
            project,
            task,
            conflict=lambda: task_conflict_error(task.id, client_supplied=bool(args.id)),
        )
    except TaskSubmissionError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return error_exit_code(exc.code)
    verb = "Added" if admission.created else "Already present:"
    print(f"{verb} {admission.task.id} [{admission.task.state.value}] {admission.task.title}")
    return 0


def cmd_task_submit(
    config: ForgeConfig, args: argparse.Namespace, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    """Handle `task add --json` and `task add --dry-run` through the versioned contract."""
    as_json = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)
    try:
        request = build_task_request(args)
        data = submit_task(config, request, dry_run=dry_run, schema_version=schema_version)
    except TaskSubmissionError as exc:
        if as_json:
            print(f"ERROR: {exc.code}", file=sys.stderr)
            write_response(
                SUBMISSION_COMMAND,
                error=StructuredError(code=exc.code, message=exc.message, details=exc.details),
                schema_version=schema_version,
            )
        else:
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return error_exit_code(exc.code)
    if as_json:
        write_response(SUBMISSION_COMMAND, data=data, schema_version=schema_version)
        return 0
    task = data["task"]
    if dry_run:
        verb = "Would add" if data["would_create"] else "Already present:"
    else:
        verb = "Added" if data["created"] else "Already present:"
    print(f"{verb} {task['task_id']} [{task['state']}] {task['title']}")
    return 0


def cmd_task_list(
    config: ForgeConfig, args: argparse.Namespace, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
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
                        schema_version=schema_version,
                    )
                    for task in tasks
                ],
                "filters": {"state": args.state},
            },
            schema_version=schema_version,
        )
        return 0
    for task in tasks:
        print(f"{task.id}\t{task.state.value}\tP{task.priority}\t{task.title}")
    return 0


def cmd_task_show(
    config: ForgeConfig,
    task_id: str,
    as_json: bool = False,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> int:
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
                schema_version=schema_version,
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
                    schema_version=schema_version,
                )
            },
            schema_version=schema_version,
        )
        return 0
    print(f"{task.id}\t{task.state.value}\tP{task.priority}\t{task.title}")
    return 0


def cmd_task_retry(config: ForgeConfig, task_id: str) -> int:
    db = ForgeDB(config.database_path)
    outcome, task = db.retry_task(task_id)
    if outcome == "not_found":
        print(f"Unknown task: {task_id}", file=sys.stderr)
        return 2
    if outcome == "exhausted":
        print(
            f"Task {task_id} exhausted its attempt limit ({task.attempts}/{task.max_attempts}).",
            file=sys.stderr,
        )
        return 1
    if outcome == "invalid_state":
        print(f"Task {task_id} cannot retry from {task.state.value}.", file=sys.stderr)
        return 2
    print(f"Task {task_id} returned to READY.")
    return 0


def cmd_task_resolve_blocker(config: ForgeConfig, args: argparse.Namespace, schema_version: str) -> int:
    orchestrator = None
    command = getattr(args, "task_command", "resolve-blocker")
    try:
        handoff = command == "handoff-prerequisite"
        if command == "finalize-prerequisite":
            from .finalization import execute, preflight, terminal_outcome

            if args.dry_run:
                ForgeDB(config.database_path, read_only=True)
                if not config.data_root.is_dir():
                    raise ValueError("An existing controller data root is required for preflight.")
                with controller_lock(config.data_root / "controller.lock"):
                    data = preflight(config, args.task_id, args.reason, args.attestation or '', args)
            else:
                if not args.receipt or not args.receipt_sha256:
                    raise ValueError("Pinned prospective receipt is required.")
                data = terminal_outcome(config, args.task_id, args.reason, args.receipt, args.receipt_sha256)
                if data is None:
                    orchestrator = Orchestrator(config, package_root())
                    data = execute(orchestrator, args.task_id, args.reason, args.receipt, args.receipt_sha256)
        elif handoff and args.dry_run:
            from .prerequisites import preflight_handoff

            ForgeDB(config.database_path, read_only=True)  # Refuse before lock setup can create a missing parent.
            if not config.data_root.is_dir():
                raise ValueError("An existing controller data root is required for preflight.")
            with controller_lock(config.data_root / "controller.lock"):
                data = preflight_handoff(config, args.task_id, args.reason, args)
        else:
            if not args.receipt or not args.receipt_sha256:
                raise ValueError("Pinned receipt is required.")
            orchestrator = Orchestrator(config, package_root())
            data = orchestrator.resolve_blocker(args.task_id, args.reason, args.receipt, args.receipt_sha256, prerequisite=handoff)
    except (ValueError, GitError, OSError, ControllerAlreadyRunning):
        return _structured_failure(
            f"task.{command}", StructuredError("INVALID_ARGUMENT", "Blocker continuation was refused.", {}),
            args.json, schema_version,
        )
    finally:
        if orchestrator is not None:
            orchestrator.telemetry.shutdown()
    if args.json:
        write_response(f"task.{command}", data=data, schema_version=schema_version)
    else:
        print(json.dumps(data, indent=2))
    # Successful invocation is distinct from its task outcome, as with `run --once`.
    return 0


def _structured_failure(
    command: str,
    error: StructuredError,
    as_json: bool,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> int:
    """Render a structured refusal identically in both output modes and map it to an exit code."""
    if as_json:
        print(f"ERROR: {error.code}", file=sys.stderr)
        write_response(command, error=error, schema_version=schema_version)
    else:
        print(f"{error.code}: {error.message}", file=sys.stderr)
    return error_exit_code(error.code)


def _project_detail_name(project: Project) -> str:
    """Structured details never carry a path-shaped value; an unsafe stored name is reported by
    the backend-minted project id instead."""
    return project.name if safe_path_component(project.name) else project.id


def _task_detail_id(task_id: str) -> dict[str, str]:
    """Structured details never carry a path-shaped value; a legacy or requested task id that
    fails the shared safe-component validator is omitted from details entirely. The raw id is
    reported on stderr only."""
    return {"task_id": task_id} if safe_path_component(task_id) else {}


def _note_unsafe_task_id(task_id: str) -> None:
    if not safe_path_component(task_id):
        print(
            f"task id is not a safe path component and was omitted from details: {task_id}",
            file=sys.stderr,
        )


def _worktree_reclaimed(connection, config: ForgeConfig, task_id: str, worktree: Path) -> bool:
    """True when the deleted id or its checkout path is owned by a live task row again.

    A live row's effective checkout is its persisted path when present, otherwise the exact path
    `prepare_worktree` would derive for it.
    """
    target = os.path.normcase(os.path.normpath(str(worktree)))
    project_names = {
        row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM projects")
    }
    for row in connection.execute("SELECT id, project_id, worktree_path FROM tasks"):
        if row["id"] == task_id:
            return True
        effective = row["worktree_path"]
        if not effective:
            owner = project_names.get(row["project_id"])
            if owner is None or not safe_path_component(owner):
                raise GitError(
                    "a live task's checkout cannot be located from its project registration; "
                    "cleanup cannot prove ownership"
                )
            effective = str(derived_worktree_path(config.workspace_root, owner, row["id"]))
        if os.path.normcase(os.path.normpath(effective)) == target:
            return True
    return False


def _protected_paths(connection, config: ForgeConfig, project: Project) -> list[Path]:
    """Everything a cleanup must never touch, gathered under the held write claim.

    Every registered repository, every project workspace except the owner's own, and every live
    task's effective checkout — persisted, or the exact path `prepare_worktree` would derive.
    The owner's repository snapshot is included even when its registry row is already deleted.
    """
    protected: list[Path] = [project.repo_path]
    protected.extend(
        (
            config.config_path,
            config.database_path,
            config.evidence_root,
            config.repository_root,
            config.inbox_root,
            config.knowledge_root,
        )
    )
    if not _lexically_inside(config.workspace_root, config.data_root):
        # In the default layout the workspace root lives inside the data root, so the data root
        # cannot be protected against containment; its precious members are protected above. In
        # any other layout the data root is protected outright.
        protected.append(config.data_root)
    project_rows = {
        row["id"]: row
        for row in connection.execute("SELECT id, name, repo_path FROM projects")
    }
    for row in project_rows.values():
        protected.append(Path(row["repo_path"]))
        if row["id"] == project.id:
            continue
        if not safe_path_component(row["name"]):
            # A legacy name that cannot be trusted as a path component cannot have its workspace
            # located either; ownership is unprovable, so cleanup fails closed.
            raise GitError(
                "a registered project name is not a safe path component; "
                "cleanup cannot prove ownership"
            )
        protected.append(config.workspace_root / row["name"])
    for row in connection.execute("SELECT id, project_id, worktree_path FROM tasks"):
        stored = row["worktree_path"]
        if stored:
            protected.append(Path(stored))
            continue
        owner = project_rows.get(row["project_id"])
        if owner is None or not safe_path_component(owner["name"]):
            raise GitError(
                "a live task's checkout cannot be located from its project registration; "
                "cleanup cannot prove ownership"
            )
        protected.append(
            derived_worktree_path(config.workspace_root, owner["name"], row["id"])
        )
    return protected


def _lexically_inside(inner: Path, outer: Path) -> bool:
    inner_c = os.path.normcase(os.path.normpath(str(inner)))
    outer_c = os.path.normcase(os.path.normpath(str(outer)))
    return inner_c == outer_c or inner_c.startswith(outer_c + os.sep)


def _remove_worktree_claimed(
    db: ForgeDB, config: ForgeConfig, project: Project, worktree: Path, task_id: str
) -> bool:
    """Remove the checkout under a database write claim.

    The row deletion committed before cleanup runs, so the same task id can already have been
    reused. Holding an immediate transaction across the removal means no new owner can appear
    mid-removal, and an id or checkout path that is live again is left untouched. The checkout
    must live strictly inside this project's own workspace directory and stay disjoint from
    every registered repository.
    """
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if _worktree_reclaimed(connection, config, task_id, worktree):
            return False
        if not safe_path_component(project.name):
            raise GitError(
                "the stored project name is not a safe path component; the checkout was not touched"
            )
        protected = _protected_paths(connection, config, project)
        repositories = _registered_repositories(connection)
        return remove_worktree(
            project.repo_path,
            worktree,
            config.workspace_root / project.name,
            protected,
            repositories,
        )


def _registered_repositories(connection) -> list[Path]:
    """Every registered project repository, read under the caller's claim.

    Cleanup ownership checks must see the same registry snapshot the claim holds, so the list is
    read on the claimed connection, never through a separate database handle.
    """
    return [
        Path(row["repo_path"]) for row in connection.execute("SELECT repo_path FROM projects")
    ]


def _remove_workspace_claimed(
    db: ForgeDB, config: ForgeConfig, project: Project, workspace: Path
) -> bool:
    """Remove the project workspace under a database write claim.

    A project name registered again — exactly or as a case variant that aliases the same
    directory — owns the workspace path now, so the stale cleanup leaves it alone. The
    workspace itself is proven disposable — a plain, non-reparse directory strictly inside the
    workspace root and disjoint from every protected path — before any Git inventory or removal
    reads through it, and its physical identity is pinned so every later mutation acts on that
    directory and never on a swapped-in target; a symlinked or junction workspace fails without
    touching its target. Git worktree registrations inside the workspace are inventoried across
    every registered repository and cleared only through checked Git operations; a foreign or
    unverifiable registration, or a locked leftover checkout, fails the cleanup closed with the
    workspace intact, so deletion never raw-deletes a registered checkout and never leaves
    stale registration metadata behind. A workspace already missing on disk is still reconciled
    against stale registrations before success is reported.
    """
    key = windows_alias_key(project.name)
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for row in connection.execute("SELECT name FROM projects"):
            if windows_alias_key(row["name"]) == key:
                return False
        repositories = _registered_repositories(connection)
        if not os.path.lexists(workspace):
            reconcile_missing_workspace(project.repo_path, workspace, repositories)
            return False
        protected = _protected_paths(connection, config, project)
        ensure_disposable_checkout(workspace, config.workspace_root, protected)
        pin = pin_plain_directory(workspace)
        remove_registered_checkouts(
            project.repo_path, workspace, protected, repositories, pin=pin
        )
        revalidate_pinned_directory(workspace, pin)
        shutil.rmtree(workspace)
        if os.path.lexists(workspace):
            raise OSError(f"workspace directory still present after removal: {workspace}")
        return True


def cmd_task_delete(
    config: ForgeConfig, args: argparse.Namespace, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    as_json = getattr(args, "json", False)
    db = ForgeDB(config.database_path)
    outcome, task = db.delete_task(args.task_id)
    if outcome == "not_found":
        _note_unsafe_task_id(args.task_id)
        return _structured_failure(
            "task.delete",
            StructuredError(
                code="TASK_NOT_FOUND",
                message="Task was not found.",
                details=_task_detail_id(args.task_id),
            ),
            as_json,
            schema_version,
        )
    if outcome == "active":
        _note_unsafe_task_id(task.id)
        return _structured_failure(
            "task.delete",
            StructuredError(
                code="TASK_ACTIVE",
                message="Task is in an active execution state and cannot be deleted.",
                details={**_task_detail_id(task.id), "state": task.state.value},
            ),
            as_json,
            schema_version,
        )
    # The durable deletion has committed; everything after this point is cleanup and reporting,
    # and any failure here must still say deleted=true, never a generic command failure.
    evidence_path: str | None = None
    worktree: Path | None = None
    worktree_removed = False
    cleanup_error: str | None = None
    try:
        # The project row must outlive its tasks (deleting a non-empty project is refused), so
        # it is still present here and names both the retained evidence root and the worktree.
        project = db.get_project(task.project_id)
        if project is not None:
            try:
                evidence_path = str(task_evidence_root(config, project.name, task.id))
            except EvidenceError:
                evidence_path = None
        worktree = task.worktree_path
        if worktree is None and project is not None:
            worktree = derived_worktree_path(config.workspace_root, project.name, task.id)
        if worktree is not None and project is not None:
            worktree_removed = _remove_worktree_claimed(db, config, project, worktree, task.id)
    except (GitError, OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        cleanup_error = str(exc)
    data = {
        "deleted": True,
        "task_id": task.id,
        "state": task.state.value,
        "evidence_retained": True,
        "evidence_path": evidence_path,
        "worktree_removed": worktree_removed,
    }
    if cleanup_error is not None:
        # The contract keeps `data` null on failure and keeps paths and OS error text out of
        # `details`; the durable outcome travels in `details` and the diagnostics go to stderr.
        _note_unsafe_task_id(task.id)
        error = StructuredError(
            code="WORKTREE_CLEANUP_FAILED",
            message="The task was deleted and its evidence is retained, but its worktree could not be removed.",
            details={
                **_task_detail_id(task.id),
                "deleted": True,
                "evidence_retained": True,
                "worktree_removed": False,
            },
        )
        if as_json:
            print(f"ERROR: {error.code}", file=sys.stderr)
        else:
            print(f"Deleted task {task.id}; evidence retained.")
            print(f"{error.code}: {error.message}", file=sys.stderr)
        print(f"worktree not removed: {worktree}: {cleanup_error}", file=sys.stderr)
        if as_json:
            write_response("task.delete", error=error, schema_version=schema_version)
        return error_exit_code(error.code)
    if as_json:
        write_response("task.delete", data=data, schema_version=schema_version)
    else:
        worktree_note = "worktree removed" if worktree_removed else "no worktree to remove"
        print(f"Deleted task {task.id}; evidence retained; {worktree_note}.")
    return 0


def cmd_project_delete(
    config: ForgeConfig, args: argparse.Namespace, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    as_json = getattr(args, "json", False)
    db = ForgeDB(config.database_path)
    outcome, project, task_count = db.delete_project(args.project)
    if outcome == "not_found":
        return _structured_failure(
            "project.delete",
            StructuredError(
                code="PROJECT_NOT_FOUND",
                message="Project was not found.",
                details={"project": args.project},
            ),
            as_json,
            schema_version,
        )
    if outcome == "ambiguous":
        return _structured_failure(
            "project.delete",
            StructuredError(
                code="PROJECT_AMBIGUOUS",
                message=(
                    "The identifier names one project's id and a different project's name; "
                    "deletion refuses to guess."
                ),
                details={"identifier": args.project},
            ),
            as_json,
            schema_version,
        )
    if outcome == "has_tasks":
        return _structured_failure(
            "project.delete",
            StructuredError(
                code="PROJECT_NOT_EMPTY",
                message="Project still has tasks; delete them explicitly first.",
                details={"project": _project_detail_name(project), "task_count": task_count},
            ),
            as_json,
            schema_version,
        )
    # Only the disposable worktree directory is Forge state that dies with the project. Evidence
    # is retained, and the repository -- whether a Forge clone or a user-supplied path -- stays,
    # because retained evidence records commit hashes that only that repository can verify.
    workspace = config.workspace_root / project.name
    workspace_removed = False
    cleanup_error: str | None = None
    if not safe_path_component(project.name):
        # The stored name cannot be trusted as a path component, so the join above cannot be
        # trusted either; fail the cleanup closed without touching the filesystem.
        cleanup_error = "the stored project name is not a safe path component; the workspace was not touched"
    else:
        # Called even when the workspace path is missing: stale Git registrations under it must
        # be reconciled before success is reported.
        try:
            workspace_removed = _remove_workspace_claimed(db, config, project, workspace)
        except (GitError, OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            cleanup_error = str(exc)
    data = {
        "deleted": True,
        "project": project.name,
        "project_id": project.id,
        "evidence_retained": True,
        "repository_removed": False,
        "workspace_removed": workspace_removed,
    }
    if cleanup_error is not None:
        # Same failure contract as task deletion: `data` stays null, `details` carries the durable
        # outcome without paths or OS error text, and the diagnostics go to stderr.
        error = StructuredError(
            code="PROJECT_CLEANUP_FAILED",
            message="The project was deleted and evidence is retained, but its worktree directory could not be removed.",
            details={
                "project": _project_detail_name(project),
                "deleted": True,
                "evidence_retained": True,
                "workspace_removed": False,
            },
        )
        if as_json:
            print(f"ERROR: {error.code}", file=sys.stderr)
        else:
            print(f"Deleted project {project.name}; evidence retained.")
            print(f"{error.code}: {error.message}", file=sys.stderr)
        print(f"worktree directory not removed: {workspace}: {cleanup_error}", file=sys.stderr)
        if as_json:
            write_response("project.delete", error=error, schema_version=schema_version)
        return error_exit_code(error.code)
    if as_json:
        write_response("project.delete", data=data, schema_version=schema_version)
    else:
        print(f"Deleted project {project.name}; evidence and repository retained.")
    return 0


def _evidence_failure(
    command: str,
    exc: EvidenceError,
    as_json: bool,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> int:
    """Render an evidence failure identically in both output modes and map it to an exit code."""
    if as_json:
        print(f"ERROR: {exc.code}", file=sys.stderr)
        write_response(
            command,
            error=StructuredError(code=exc.code, message=exc.message, details=exc.details),
            schema_version=schema_version,
        )
    else:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
    return error_exit_code(exc.code)


def cmd_task_evidence_list(
    config: ForgeConfig, args: argparse.Namespace, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    as_json = getattr(args, "json", False)
    try:
        data = evidence_list(config, args.task_id, schema_version=args.schema_version)
    except EvidenceError as exc:
        return _evidence_failure(EVIDENCE_LIST_COMMAND, exc, as_json, schema_version)
    if as_json:
        write_response(EVIDENCE_LIST_COMMAND, data=data, schema_version=schema_version)
        return 0
    if not data["evidence_available"]:
        print(f"No evidence recorded for {data['task_id']}.")
        return 0
    for artifact in data["artifacts"]:
        print(
            f"{artifact['artifact_id']}\t{artifact['artifact_kind']}\t"
            f"{artifact['original_size_bytes']}\t{artifact['display_name']}"
        )
    return 0


def cmd_task_evidence_show(
    config: ForgeConfig, args: argparse.Namespace, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    as_json = getattr(args, "json", False)
    try:
        data = evidence_show(
            config, args.task_id, args.artifact_id, schema_version=args.schema_version
        )
    except EvidenceError as exc:
        return _evidence_failure(EVIDENCE_SHOW_COMMAND, exc, as_json, schema_version)
    if as_json:
        write_response(EVIDENCE_SHOW_COMMAND, data=data, schema_version=schema_version)
        return 0
    artifact = data["artifact"]
    content = data["content"]
    print(f"{artifact['artifact_kind']}\t{artifact['display_name']}")
    if content["truncated"]:
        print(f"[truncated to {content['returned_size_bytes']} of {content['original_size_bytes']} bytes]")
    if content["redacted"]:
        print("[content was redacted]")
    print(content["text"])
    return 0


def cmd_status(
    config: ForgeConfig, as_json: bool, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    db = ForgeDB(config.database_path)
    tasks = db.list_tasks()
    counts = {state.value: 0 for state in TaskState}
    for task in tasks:
        counts[task.state.value] += 1
    decisions = db.list_open_decisions()
    payload = {"tasks": counts, "open_decisions": len(decisions), "database": str(config.database_path)}
    if as_json:
        write_response(
            "status", data=payload, legacy_fields=payload, schema_version=schema_version
        )
    else:
        print(" | ".join(f"{name}={count}" for name, count in counts.items() if count))
        print(f"OPEN_DECISIONS={len(decisions)}")
    return 0


def cmd_decision_list(
    config: ForgeConfig, as_json: bool = False, schema_version: str = DEFAULT_SCHEMA_VERSION
) -> int:
    db = ForgeDB(config.database_path)
    rows = db.list_open_decisions()
    if as_json:
        write_response(
            "decision.list",
            data={"decisions": [decision_to_public(row) for row in rows]},
            schema_version=schema_version,
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
    if words[0] == "task" and len(words) > 2 and words[1] == "evidence" and words[2] in {"list", "show"}:
        return f"task.evidence.{words[2]}"
    if words[0] in {"project", "task", "decision"} and len(words) > 1:
        return f"{words[0]}.{words[1]}"
    return words[0]


def _requested_schema_version(argv: list[str]) -> str:
    """Best-effort recovery of a supported requested schema version from raw arguments.

    Used only for error envelopes emitted before parsing succeeds; an absent, malformed, or
    unsupported value falls back to the default, and the dedicated UNSUPPORTED_SCHEMA path keeps
    its own pinned contract.
    """
    tokens = list(argv)
    requested = None
    for index, token in enumerate(tokens):
        if token == "--schema-version" and index + 1 < len(tokens):
            requested = tokens[index + 1]
        elif token.startswith("--schema-version="):
            requested = token.split("=", 1)[1]
    try:
        return resolve_schema_version(requested)
    except UnsupportedSchemaVersion:
        return DEFAULT_SCHEMA_VERSION


def _write_cli_error(
    command: str,
    code: str,
    message: str,
    details: dict | None = None,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> None:
    write_response(
        command,
        error=StructuredError(code=code, message=message, details=details or {}),
        schema_version=schema_version,
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
                schema_version=_requested_schema_version(raw_argv),
            )
        else:
            parser.print_usage(sys.stderr)
            print(f"forge: error: {exc}", file=sys.stderr)
        return 2
    finally:
        ForgeArgumentParser.structured_errors = False

    # Resolved once, before any command runs, so an unknown version fails closed everywhere rather
    # than in each command's own path.
    try:
        schema_version = resolve_schema_version(getattr(args, "schema_version", None))
    except UnsupportedSchemaVersion as exc:
        if json_requested:
            print("ERROR: UNSUPPORTED_SCHEMA", file=sys.stderr)
            write_response(
                _requested_command(raw_argv), error=unsupported_schema_error(exc.requested)
            )
        else:
            print(
                f"UNSUPPORTED_SCHEMA: {exc.requested} is not a supported contract schema version.",
                file=sys.stderr,
            )
        return error_exit_code("UNSUPPORTED_SCHEMA")

    try:
        config = load_config(args.config)
    except Exception as exc:
        if json_requested:
            print(f"ERROR: {type(exc).__name__}: Forge configuration could not be loaded.", file=sys.stderr)
            _write_cli_error(
                _requested_command(raw_argv),
                "CONFIG_ERROR",
                "Forge configuration could not be loaded.",
                schema_version=schema_version,
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
            if args.project_command == "relocate":
                return cmd_project_relocate(config, args, schema_version)
            if args.project_command == "delete":
                return cmd_project_delete(config, args, schema_version)
            if args.project_command in ("plan", "extend", "progress", "resume"):
                return cmd_project_workflow(config, args, schema_version)
            return cmd_project_list(config, args.json, schema_version)
        if args.command == "task":
            if args.task_command == "add":
                return cmd_task_add(config, args, schema_version)
            if args.task_command == "list":
                return cmd_task_list(config, args, schema_version)
            if args.task_command == "show":
                return cmd_task_show(config, args.task_id, args.json, schema_version)
            if args.task_command in ("resolve-blocker", "handoff-prerequisite", "finalize-prerequisite"):
                return cmd_task_resolve_blocker(config, args, schema_version)
            if args.task_command == "delete":
                return cmd_task_delete(config, args, schema_version)
            if args.task_command == "evidence":
                if args.evidence_command == "list":
                    return cmd_task_evidence_list(config, args, schema_version)
                return cmd_task_evidence_show(config, args, schema_version)
            return cmd_task_retry(config, args.task_id)
        if args.command == "status":
            return cmd_status(config, args.json, schema_version)
        if args.command == "decision":
            return cmd_decision_list(config, args.json, schema_version)
        if args.command == "run":
            orchestrator = Orchestrator(config, package_root())
            # Telemetry is flushed and torn down on every exit path, including a daemon that is
            # interrupted. The shutdown is time-bounded, so an unreachable collector cannot delay
            # process exit.
            try:
                if args.daemon:
                    orchestrator.run_daemon()
                    return 0
                return 0 if orchestrator.run_once() >= 0 else 1
            finally:
                orchestrator.telemetry.shutdown()
        raise CLIArgumentError("Unknown command")
    except Exception as exc:
        if json_requested:
            print(f"ERROR: {type(exc).__name__}: command failed.", file=sys.stderr)
            _write_cli_error(
                _requested_command(raw_argv),
                "COMMAND_FAILED",
                "The command could not be completed.",
                schema_version=schema_version,
            )
            return 1
        raise
