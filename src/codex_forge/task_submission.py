"""Centralized validation, normalization, and serialization for contract task submission.

External applications submit tasks through `forge task add --json`. Everything that decides
whether a submission is acceptable, how it is normalized, how it is made retry-safe, and how it
is rendered back to the caller lives here so the CLI stays a thin transport layer.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

from .config import ForgeConfig
from .control_plane import redact_command, task_to_public
from .db import ForgeDB
from .models import Project, Task, TaskState
from .utils import flatten_unique, json_dumps, slugify, stable_id


SUBMISSION_COMMAND = "task.add"

DEFAULT_RISK = "medium"
DEFAULT_PRIORITY = 100
DEFAULT_MAX_ATTEMPTS = 3
RISK_LEVELS = ("low", "medium", "high", "critical")

MAX_TITLE_CHARS = 200
MAX_OBJECTIVE_CHARS = 20_000
MAX_TASK_ID_CHARS = 64
MAX_ACCEPTANCE_ITEMS = 50
MAX_ACCEPTANCE_CHARS = 1_000
MAX_VALIDATION_ITEMS = 20
MAX_VALIDATION_CHARS = 500
MAX_TAG_ITEMS = 30
MAX_TAG_CHARS = 60
MAX_DEPENDENCY_ITEMS = 50
MIN_MAX_ATTEMPTS = 1
MAX_MAX_ATTEMPTS = 50
MIN_PRIORITY = 0
MAX_PRIORITY = 10_000

# Characters that let a submitted validation command chain, redirect, or substitute another
# command. External submissions must name one command, not compose a shell script.
SHELL_METACHARACTERS = (";", "|", "&", "`", "<", ">")
SHELL_SUBSTITUTION = ("$(",)

# Task identifiers become branch and worktree components. Reject separators, Windows-reserved
# characters, and relative-path traversal outright rather than relying on downstream slugging.
TASK_ID_FORBIDDEN = ("/", "\\", ":", '"', "|", "?", "*", "<", ">")

# Bidirectional overrides can make submitted text render differently than it is stored.
_BIDI_CONTROLS = frozenset(
    chr(code)
    for code in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)
)


class TaskSubmissionError(Exception):
    """A submission failure that maps to a stable structured error code."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


@dataclass(frozen=True, slots=True)
class TaskRequest:
    """A normalized, validated submission request. Independent of any database state."""

    project: str
    title: str
    objective: str
    acceptance: tuple[str, ...]
    validation_commands: tuple[str, ...]
    tags: tuple[str, ...]
    dependencies: tuple[str, ...]
    risk: str
    priority: int
    max_attempts: int
    task_id: str | None
    defaults_applied: tuple[str, ...] = ()


def _is_forbidden_character(character: str, *, allow_whitespace: bool) -> bool:
    if character in _BIDI_CONTROLS:
        return True
    if allow_whitespace and character in "\r\n\t":
        return False
    return unicodedata.category(character) == "Cc"


def _reject_control_characters(
    value: str, *, field_name: str, allow_whitespace: bool, code: str, details: Mapping[str, Any]
) -> None:
    if any(_is_forbidden_character(character, allow_whitespace=allow_whitespace) for character in value):
        raise TaskSubmissionError(
            code,
            f"{field_name} contains control or bidirectional formatting characters.",
            details,
        )


# Whitespace, separators, and format characters carry no content even though `str.strip` keeps
# some of them. Text that renders as blank must not pass as a title, objective, or criterion.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp"})


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _has_visible_content(value: str) -> bool:
    return any(unicodedata.category(character) not in _INVISIBLE_CATEGORIES for character in value)


def _require_text(
    value: Any, *, field_name: str, maximum: int, allow_whitespace: bool = False
) -> str:
    if not isinstance(value, str):
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST", f"{field_name} must be text.", {"field": field_name}
        )
    normalized = _normalize_text(value)
    if not normalized or not _has_visible_content(normalized):
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST", f"{field_name} must not be empty.", {"field": field_name}
        )
    if len(normalized) > maximum:
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST",
            f"{field_name} exceeds the maximum of {maximum} characters.",
            {"field": field_name, "maximum": maximum, "length": len(normalized)},
        )
    _reject_control_characters(
        normalized,
        field_name=field_name,
        allow_whitespace=allow_whitespace,
        code="INVALID_TASK_REQUEST",
        details={"field": field_name},
    )
    return normalized


def _normalize_string_list(
    values: Any,
    *,
    field_name: str,
    max_items: int,
    max_chars: int,
    code: str = "INVALID_TASK_REQUEST",
) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise TaskSubmissionError(
            code, f"{field_name} must be a list of text values.", {"field": field_name}
        )
    normalized: list[str] = []
    for index, raw in enumerate(values):
        if not isinstance(raw, str):
            raise TaskSubmissionError(
                code, f"{field_name} entries must be text.", {"field": field_name, "index": index}
            )
        entry = _normalize_text(raw)
        if not entry or not _has_visible_content(entry):
            raise TaskSubmissionError(
                code, f"{field_name} entries must not be empty.", {"field": field_name, "index": index}
            )
        if len(entry) > max_chars:
            raise TaskSubmissionError(
                code,
                f"{field_name} entries must not exceed {max_chars} characters.",
                {"field": field_name, "index": index, "maximum": max_chars},
            )
        _reject_control_characters(
            entry,
            field_name=f"{field_name} entries",
            allow_whitespace=False,
            code=code,
            details={"field": field_name, "index": index},
        )
        normalized.append(entry)
    deduplicated = tuple(flatten_unique(normalized))
    if len(deduplicated) > max_items:
        raise TaskSubmissionError(
            code,
            f"{field_name} must not contain more than {max_items} entries.",
            {"field": field_name, "maximum": max_items, "count": len(deduplicated)},
        )
    return deduplicated


def _normalize_validation_commands(values: Any) -> tuple[str, ...]:
    commands = _normalize_string_list(
        values,
        field_name="validation_commands",
        max_items=MAX_VALIDATION_ITEMS,
        max_chars=MAX_VALIDATION_CHARS,
        code="INVALID_VALIDATION_COMMAND",
    )
    for index, command in enumerate(commands):
        offending = [token for token in SHELL_METACHARACTERS if token in command]
        offending += [token for token in SHELL_SUBSTITUTION if token in command]
        if offending:
            # The command text may carry credentials, so report position only.
            raise TaskSubmissionError(
                "INVALID_VALIDATION_COMMAND",
                "Validation commands must not chain, redirect, or substitute shell commands.",
                {"index": index, "forbidden": sorted(set(offending))},
            )
    return commands


def _normalize_task_id(value: Any, *, field_name: str = "task_id") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskSubmissionError("INVALID_TASK_REQUEST", "task_id must be text.", {"field": field_name})
    task_id = _normalize_text(value)
    if not task_id:
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST", "task_id must not be empty.", {"field": field_name}
        )
    if len(task_id) > MAX_TASK_ID_CHARS:
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST",
            f"task_id must not exceed {MAX_TASK_ID_CHARS} characters.",
            {"field": field_name, "maximum": MAX_TASK_ID_CHARS},
        )
    _reject_control_characters(
        task_id,
        field_name=field_name,
        allow_whitespace=False,
        code="INVALID_TASK_REQUEST",
        details={"field": field_name},
    )
    if any(character.isspace() for character in task_id):
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST", "task_id must not contain whitespace.", {"field": field_name}
        )
    forbidden = [token for token in TASK_ID_FORBIDDEN if token in task_id]
    if forbidden:
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST",
            "task_id must not contain path separators or reserved characters.",
            {"field": field_name, "forbidden": sorted(set(forbidden))},
        )
    if ".." in task_id:
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST", "task_id must not contain '..'.", {"field": field_name}
        )
    if task_id[0] in "-.":
        # A leading dash or dot cannot be passed back to `forge task show`, and a leading dot
        # produces a hidden worktree directory.
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST",
            "task_id must not start with '-' or '.'.",
            {"field": field_name},
        )
    if not any(character.isalnum() for character in task_id):
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST",
            "task_id must contain at least one letter or digit.",
            {"field": field_name},
        )
    return task_id


def _normalize_risk(value: Any) -> str:
    if not isinstance(value, str):
        raise TaskSubmissionError("INVALID_TASK_REQUEST", "risk must be text.", {"field": "risk"})
    risk = _normalize_text(value).lower()
    if risk not in RISK_LEVELS:
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST",
            "risk must be one of the supported levels.",
            {"field": "risk", "supported": list(RISK_LEVELS)},
        )
    return risk


def _normalize_bounded_integer(value: Any, *, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskSubmissionError(
            "INVALID_LIMIT", f"{field_name} must be an integer.", {"field": field_name}
        )
    if value < minimum or value > maximum:
        raise TaskSubmissionError(
            "INVALID_LIMIT",
            f"{field_name} must be between {minimum} and {maximum}.",
            {"field": field_name, "minimum": minimum, "maximum": maximum, "value": value},
        )
    return value


def build_task_request(args: argparse.Namespace) -> TaskRequest:
    """Normalize and validate CLI arguments into a database-independent request."""
    defaults: list[str] = []

    title = _require_text(getattr(args, "title", None), field_name="title", maximum=MAX_TITLE_CHARS)
    objective = _require_text(
        getattr(args, "objective", None),
        field_name="objective",
        maximum=MAX_OBJECTIVE_CHARS,
        allow_whitespace=True,
    )
    project = _require_text(getattr(args, "project", None), field_name="project", maximum=MAX_TITLE_CHARS)

    acceptance = _normalize_string_list(
        getattr(args, "acceptance", None),
        field_name="acceptance_criteria",
        max_items=MAX_ACCEPTANCE_ITEMS,
        max_chars=MAX_ACCEPTANCE_CHARS,
    )
    validation_commands = _normalize_validation_commands(getattr(args, "validation", None))
    tags = _normalize_string_list(
        getattr(args, "tag", None), field_name="tags", max_items=MAX_TAG_ITEMS, max_chars=MAX_TAG_CHARS
    )
    # A dependency names another task, so it must satisfy the same identifier rules.
    dependencies = tuple(
        _normalize_task_id(dependency, field_name="dependencies")
        for dependency in _normalize_string_list(
            getattr(args, "depends_on", None),
            field_name="dependencies",
            max_items=MAX_DEPENDENCY_ITEMS,
            max_chars=MAX_TASK_ID_CHARS,
        )
    )

    task_id = _normalize_task_id(getattr(args, "id", None))
    if task_id is not None and task_id in dependencies:
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST", "A task must not depend on itself.", {"field": "dependencies"}
        )

    raw_risk = getattr(args, "risk", None)
    if raw_risk is None:
        risk = DEFAULT_RISK
        defaults.append("risk")
    else:
        risk = _normalize_risk(raw_risk)

    raw_priority = getattr(args, "priority", None)
    if raw_priority is None:
        priority = DEFAULT_PRIORITY
        defaults.append("priority")
    else:
        priority = _normalize_bounded_integer(
            raw_priority, field_name="priority", minimum=MIN_PRIORITY, maximum=MAX_PRIORITY
        )

    raw_max_attempts = getattr(args, "max_attempts", None)
    if raw_max_attempts is None:
        max_attempts = DEFAULT_MAX_ATTEMPTS
        defaults.append("max_attempts")
    else:
        max_attempts = _normalize_bounded_integer(
            raw_max_attempts,
            field_name="max_attempts",
            minimum=MIN_MAX_ATTEMPTS,
            maximum=MAX_MAX_ATTEMPTS,
        )

    if not acceptance:
        defaults.append("acceptance_criteria")
    if not validation_commands:
        defaults.append("validation_commands")
    if not tags:
        defaults.append("tags")
    if not dependencies:
        defaults.append("dependencies")
    if task_id is None:
        defaults.append("task_id")

    return TaskRequest(
        project=project,
        title=title,
        objective=objective,
        acceptance=acceptance,
        validation_commands=validation_commands,
        tags=tags,
        dependencies=dependencies,
        risk=risk,
        priority=priority,
        max_attempts=max_attempts,
        task_id=task_id,
        defaults_applied=tuple(defaults),
    )


def _fingerprint(
    *,
    project_id: str,
    title: str,
    objective: str,
    acceptance: Iterable[str],
    validation_commands: Iterable[str],
    tags: Iterable[str],
    dependencies: Iterable[str],
    risk: str,
    priority: int,
    max_attempts: int,
) -> str:
    """Hash the material content of a task so identical retries are recognizable.

    Both a submitted request and a persisted task are normalized the same way before hashing,
    so no additional database column is required to make submission idempotent.

    Acceptance criteria and validation commands keep their order, because validation commands run
    in the order given and stop at the first failure. Tags and dependencies are unordered sets, so
    they are sorted here to keep a reordered retry from reading as a different request.
    """
    material = {
        "project_id": project_id,
        "title": _normalize_text(title),
        "objective": _normalize_text(objective),
        "acceptance": [_normalize_text(item) for item in acceptance],
        "validation_commands": [_normalize_text(item) for item in validation_commands],
        "tags": sorted(_normalize_text(item) for item in tags),
        "dependencies": sorted(_normalize_text(item) for item in dependencies),
        "risk": _normalize_text(risk).lower(),
        "priority": priority,
        "max_attempts": max_attempts,
    }
    return hashlib.sha256(json_dumps(material).encode("utf-8")).hexdigest()


def request_fingerprint(request: TaskRequest, project: Project) -> str:
    return _fingerprint(
        project_id=project.id,
        title=request.title,
        objective=request.objective,
        acceptance=request.acceptance,
        validation_commands=request.validation_commands,
        tags=request.tags,
        dependencies=request.dependencies,
        risk=request.risk,
        priority=request.priority,
        max_attempts=request.max_attempts,
    )


def task_fingerprint(task: Task) -> str:
    return _fingerprint(
        project_id=task.project_id,
        title=task.title,
        objective=task.objective,
        acceptance=task.acceptance,
        validation_commands=task.validation_commands,
        tags=task.tags,
        dependencies=task.dependencies,
        risk=task.risk,
        priority=task.priority,
        max_attempts=task.max_attempts,
    )


@contextmanager
def _database_scope() -> Iterator[None]:
    """Map SQLite failures to stable codes without leaking database detail to callers."""
    try:
        yield
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            raise TaskSubmissionError(
                "CONFIGURATION_ERROR",
                "The Forge database is not initialized. Run 'forge init' before submitting tasks.",
            ) from exc
        raise TaskSubmissionError(
            "DATABASE_ERROR", "The Forge database could not be accessed."
        ) from exc
    except sqlite3.Error as exc:
        raise TaskSubmissionError(
            "DATABASE_ERROR", "The Forge database rejected the operation."
        ) from exc


def derive_task_id(request: TaskRequest, project: Project) -> str:
    return request.task_id or stable_id(
        "TASK", f"{project.id}:{request.title}:{request.objective}"
    )


def planned_branch(task_id: str, title: str) -> str:
    return f"forge/{slugify(task_id)}-{slugify(title, 36)}"


def planned_worktree(config: ForgeConfig, project: Project, task_id: str) -> str:
    return str(config.workspace_root / project.name / slugify(task_id))


def resolve_state(dependencies: Iterable[str]) -> TaskState:
    return TaskState.BACKLOG if tuple(dependencies) else TaskState.READY


def open_database(config: ForgeConfig) -> ForgeDB:
    """Open the configured database without bringing it into existence.

    `ForgeDB` creates its parent directory and SQLite creates the file on first connect, so an
    unchecked open would leave state behind for a validation call that promised to write nothing.
    """
    if not config.database_path.exists():
        raise TaskSubmissionError(
            "CONFIGURATION_ERROR",
            "The Forge database is not initialized. Run 'forge init' before submitting tasks.",
        )
    try:
        return ForgeDB(config.database_path)
    except OSError as exc:
        raise TaskSubmissionError(
            "CONFIGURATION_ERROR", "The configured Forge database path is not usable."
        ) from exc


def _resolve_project(db: ForgeDB, identifier: str) -> Project:
    with _database_scope():
        project = db.get_project(identifier)
    if project is None:
        raise TaskSubmissionError(
            "PROJECT_NOT_FOUND",
            "The requested project is not registered with Forge.",
            {"project": identifier},
        )
    return project


def _normalized_request_payload(request: TaskRequest, project: Project, task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_id_source": "client" if request.task_id else "derived",
        "project": project.name,
        "project_id": project.id,
        "title": request.title,
        "objective": request.objective,
        "acceptance_criteria": list(request.acceptance),
        "validation_commands": [redact_command(command) for command in request.validation_commands],
        "tags": list(request.tags),
        "dependencies": list(request.dependencies),
        "risk": request.risk,
        "priority": request.priority,
        "max_attempts": request.max_attempts,
    }


def _defaults_payload(
    request: TaskRequest, config: ForgeConfig, task_id: str, state: TaskState
) -> dict[str, Any]:
    resolved: dict[str, Any] = {"state": state.value, "token_limit": config.max_task_tokens}
    values: dict[str, Any] = {
        "risk": request.risk,
        "priority": request.priority,
        "max_attempts": request.max_attempts,
        "acceptance_criteria": list(request.acceptance),
        "validation_commands": list(request.validation_commands),
        "tags": list(request.tags),
        "dependencies": list(request.dependencies),
        "task_id": task_id,
    }
    for name in request.defaults_applied:
        resolved[name] = values[name]
    return resolved


EMPTY_TOKEN_USAGE = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _task_payload(
    task: Task,
    *,
    config: ForgeConfig,
    project: Project,
    token_usage: Mapping[str, int],
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    payload = task_to_public(
        task,
        project_name=project.name,
        token_usage=token_usage,
        token_limit=config.max_task_tokens,
        runtime=runtime,
    )
    payload["acceptance_criteria"] = list(task.acceptance)
    payload["validation_commands"] = [redact_command(command) for command in task.validation_commands]
    payload["tags"] = list(task.tags)
    payload["dependencies"] = list(task.dependencies)
    payload["priority"] = task.priority
    payload["planned_branch"] = planned_branch(task.id, task.title)
    payload["planned_worktree"] = planned_worktree(config, project, task.id)
    return payload


def _preview_task(request: TaskRequest, project: Project, task_id: str, state: TaskState) -> Task:
    return Task(
        id=task_id,
        project_id=project.id,
        title=request.title,
        objective=request.objective,
        acceptance=list(request.acceptance),
        validation_commands=list(request.validation_commands),
        state=state,
        priority=request.priority,
        risk=request.risk,
        tags=list(request.tags),
        dependencies=list(request.dependencies),
        max_attempts=request.max_attempts,
    )


def _conflict_error(request: TaskRequest, task_id: str) -> TaskSubmissionError:
    if request.task_id:
        return TaskSubmissionError(
            "TASK_ID_CONFLICT",
            "The task_id already identifies a different task request. Submit a new task_id or "
            "resubmit the original request unchanged.",
            {"task_id": task_id},
        )
    return TaskSubmissionError(
        "DUPLICATE_TASK_ID",
        "The derived task_id already identifies a different task request. Supply an explicit "
        "task_id for this submission.",
        {"task_id": task_id},
    )


def _persisted_task_payload(
    db: ForgeDB, task: Task, *, config: ForgeConfig, project: Project
) -> dict[str, Any]:
    with _database_scope():
        token_usage = db.task_token_usage(task.id)
        runtime = db.task_runtime(task.id)
    return _task_payload(
        task, config=config, project=project, token_usage=token_usage, runtime=runtime
    )


def _insert_once(
    db: ForgeDB, task: Task, fingerprint: str, request: TaskRequest
) -> bool:
    """Insert the task, returning whether this call created it.

    A primary-key violation means a concurrent writer inserted the same identifier first. An
    identical request adopts that row so a client retry never produces a duplicate task; a
    different request is a conflict.
    """
    try:
        db.add_task(task)
        return True
    except sqlite3.IntegrityError as exc:
        with _database_scope():
            racing = db.get_task(task.id)
        if racing is None:
            # No row holds this identifier, so the violation was not a duplicate id — a foreign
            # key or another constraint failed. Reporting a conflict would be misleading.
            raise TaskSubmissionError(
                "DATABASE_ERROR", "The Forge database rejected the task."
            ) from exc
        if task_fingerprint(racing) != fingerprint:
            raise _conflict_error(request, task.id) from exc
        return False
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            raise TaskSubmissionError(
                "CONFIGURATION_ERROR",
                "The Forge database is not initialized. Run 'forge init' before submitting tasks.",
            ) from exc
        raise TaskSubmissionError(
            "DATABASE_ERROR", "The Forge database could not be accessed."
        ) from exc
    except sqlite3.Error as exc:
        raise TaskSubmissionError(
            "DATABASE_ERROR", "The Forge database rejected the task."
        ) from exc


def submit_task(
    config: ForgeConfig, request: TaskRequest, *, dry_run: bool, db: ForgeDB | None = None
) -> dict[str, Any]:
    """Validate a request and, unless `dry_run`, persist it exactly once.

    Returns the `data` payload of the schema 1.0 envelope. Raises `TaskSubmissionError` for every
    expected failure so the caller emits one stable structured error.
    """
    db = db or open_database(config)
    project = _resolve_project(db, request.project)
    task_id = derive_task_id(request, project)
    state = resolve_state(request.dependencies)
    fingerprint = request_fingerprint(request, project)

    with _database_scope():
        existing = db.get_task(task_id)

    normalized = _normalized_request_payload(request, project, task_id)
    defaults = _defaults_payload(request, config, task_id, state)

    if existing is not None:
        if task_fingerprint(existing) != fingerprint:
            raise _conflict_error(request, task_id)
        return {
            "dry_run": dry_run,
            "created": False,
            "would_create": False,
            "task": _persisted_task_payload(db, existing, config=config, project=project),
            "normalized_request": normalized,
            "defaults_applied": defaults,
        }

    if dry_run:
        preview = _preview_task(request, project, task_id, state)
        return {
            "dry_run": True,
            "created": False,
            "would_create": True,
            "task": _task_payload(
                preview,
                config=config,
                project=project,
                token_usage=EMPTY_TOKEN_USAGE,
                runtime={},
            ),
            "normalized_request": normalized,
            "defaults_applied": defaults,
        }

    created = _insert_once(db, _preview_task(request, project, task_id, state), fingerprint, request)

    with _database_scope():
        stored = db.get_task(task_id)
    if stored is None:
        raise TaskSubmissionError(
            "DATABASE_ERROR", "The task could not be read back after submission.", {"task_id": task_id}
        )

    return {
        "dry_run": False,
        "created": created,
        "would_create": False,
        "task": _persisted_task_payload(db, stored, config=config, project=project),
        "normalized_request": normalized,
        "defaults_applied": defaults,
    }
