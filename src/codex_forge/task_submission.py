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
from typing import Any, Callable, Iterable, Iterator, Mapping

from .config import ForgeConfig
from .control_plane import (
    ADMISSION_BASE_COMMIT_SINCE,
    DEFAULT_SCHEMA_VERSION,
    _declares,
    redact_command,
    task_to_public,
)
from .db import ForgeDB
from .evidence import EvidenceError, retained_evidence_claim
from .gitops import BaseCommitError, GitError, resolve_admission_base_commit, verify_repository
from .models import Project, Task, TaskState
from .usage import empty_token_usage
from .utils import (
    derived_worktree_path,
    flatten_unique,
    json_dumps,
    slugify,
    stable_id,
    windows_alias_key,
    windows_safe_component,
)


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
MAX_BINDING_ITEMS = 50
MAX_BINDING_CHARS = 1_000
MAX_SCOPE_CHARS = 20_000
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
    # Exactly as the caller wrote it. Resolution needs the project's repository, which a
    # database-independent request must not reach, so the canonical form is produced later.
    base_commit: str | None = None
    architecture_decisions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    scope: str = ""
    out_of_scope: str = ""
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


def _optional_text(value: Any, *, field_name: str, maximum: int) -> str:
    """Normalize a field that may be omitted entirely; supplied text must still be visible."""
    if value is None:
        return ""
    return _require_text(value, field_name=field_name, maximum=maximum, allow_whitespace=True)


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
    if not windows_safe_component(task_id):
        # The shared Windows-safe component validator is the single authority: trailing dots or
        # spaces alias their stripped names, and DOS device names alias devices with any
        # extension.
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST",
            "task_id must be a Windows-safe path component.",
            {"field": field_name},
        )
    return task_id


def _normalize_base_commit(value: Any) -> str | None:
    """Normalize the requested admission base without deciding whether it names a commit.

    Only shape is checked here, because whether a commit exists is a question for the project's
    registered repository and this function is database- and repository-independent. An omitted
    base stays omitted: it is not defaulted, and it never becomes the default branch.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST",
            "base_commit must be a string.",
            {"field": "base_commit"},
        )
    candidate = value.strip()
    if not candidate:
        raise TaskSubmissionError(
            "INVALID_TASK_REQUEST",
            "base_commit must not be empty when it is supplied.",
            {"field": "base_commit"},
        )
    return candidate


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
    architecture_decisions = _normalize_string_list(
        getattr(args, "decision", None),
        field_name="architecture_decisions",
        max_items=MAX_BINDING_ITEMS,
        max_chars=MAX_BINDING_CHARS,
    )
    constraints = _normalize_string_list(
        getattr(args, "constraint", None),
        field_name="constraints",
        max_items=MAX_BINDING_ITEMS,
        max_chars=MAX_BINDING_CHARS,
    )
    scope = _optional_text(getattr(args, "scope", None), field_name="scope", maximum=MAX_SCOPE_CHARS)
    out_of_scope = _optional_text(
        getattr(args, "out_of_scope", None), field_name="out_of_scope", maximum=MAX_SCOPE_CHARS
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

    base_commit = _normalize_base_commit(getattr(args, "base_commit", None))

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
    if not architecture_decisions:
        defaults.append("architecture_decisions")
    if not constraints:
        defaults.append("constraints")
    if not scope:
        defaults.append("scope")
    if not out_of_scope:
        defaults.append("out_of_scope")
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
        base_commit=base_commit,
        architecture_decisions=architecture_decisions,
        constraints=constraints,
        scope=scope,
        out_of_scope=out_of_scope,
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
    architecture_decisions: Iterable[str],
    constraints: Iterable[str],
    scope: str,
    out_of_scope: str,
    admission_base_commit: str | None,
) -> str:
    """Hash the material content of a task so identical retries are recognizable.

    Both a submitted request and a persisted task are normalized the same way before hashing,
    so no additional database column is required to make submission idempotent.

    Acceptance criteria and validation commands keep their order, because validation commands run
    in the order given and stop at the first failure. Architecture decisions and constraints are
    binding requirements and keep their order too, so the same task identity can never represent
    two different binding contracts. Tags and dependencies are unordered sets, so they are sorted
    here to keep a reordered retry from reading as a different request.

    The admission base is material: one task identity may never stand for two execution lineages,
    so a retry that names a different base -- or none -- is a different request and not an
    idempotent repeat. Its canonical resolved form is hashed, so the same commit written in a
    different case is the same request.
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
        "architecture_decisions": [_normalize_text(item) for item in architecture_decisions],
        "constraints": [_normalize_text(item) for item in constraints],
        "scope": _normalize_text(scope),
        "out_of_scope": _normalize_text(out_of_scope),
        "admission_base_commit": admission_base_commit,
    }
    return hashlib.sha256(json_dumps(material).encode("utf-8")).hexdigest()


def request_fingerprint(
    request: TaskRequest, project: Project, admission_base_commit: str | None
) -> str:
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
        architecture_decisions=request.architecture_decisions,
        constraints=request.constraints,
        scope=request.scope,
        out_of_scope=request.out_of_scope,
        admission_base_commit=admission_base_commit,
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
        architecture_decisions=task.architecture_decisions,
        constraints=task.constraints,
        scope=task.scope,
        out_of_scope=task.out_of_scope,
        admission_base_commit=task.admission_base_commit,
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


def resolve_base_commit(project: Project, base_commit: str | None) -> str | None:
    """Resolve a requested admission base against the project's registered repository.

    Returns None when no base was requested, which preserves Forge's existing base selection for
    that task exactly. A requested base is proven to be a commit object that this repository
    actually has, and the canonical object id Git returns is what everything downstream persists,
    fingerprints and reports.

    Containment in `origin/<default_branch>` is deliberately not required. The case this exists
    for is a predecessor merged locally and not yet pushed, so demanding the remote contain it
    would refuse exactly the admission this primitive was built to make possible. Whether the base
    contains the right predecessors is the controller's law, not Forge's; Forge's law is to use
    exactly the commit it was given.
    """
    if base_commit is None:
        return None
    try:
        verify_repository(project.repo_path)
    except GitError as exc:
        raise TaskSubmissionError(
            "REPOSITORY_UNAVAILABLE",
            "The project's registered repository could not be read, so the requested admission "
            "base commit could not be verified.",
            {"project_id": project.id},
        ) from exc
    try:
        return resolve_admission_base_commit(project.repo_path, base_commit)
    except BaseCommitError as exc:
        raise TaskSubmissionError(
            "INVALID_BASE_COMMIT", str(exc), {"project_id": project.id}
        ) from exc
    except GitError as exc:
        raise TaskSubmissionError(
            "REPOSITORY_UNAVAILABLE",
            "The project's registered repository could not be read, so the requested admission "
            "base commit could not be verified.",
            {"project_id": project.id},
        ) from exc


def planned_branch(task_id: str, title: str) -> str:
    return f"forge/{slugify(task_id)}-{slugify(title, 36)}"


def planned_worktree(config: ForgeConfig, project: Project, task_id: str) -> str:
    return str(derived_worktree_path(config.workspace_root, project.name, task_id))


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


def _normalized_request_payload(
    request: TaskRequest,
    project: Project,
    task_id: str,
    admission_base_commit: str | None,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "task_id_source": "client" if request.task_id else "derived",
        "project": project.name,
        "project_id": project.id,
        "title": request.title,
        "objective": request.objective,
        "acceptance_criteria": list(request.acceptance),
        "architecture_decisions": list(request.architecture_decisions),
        "constraints": list(request.constraints),
        "scope": request.scope,
        "out_of_scope": request.out_of_scope,
        "validation_commands": [redact_command(command) for command in request.validation_commands],
        "tags": list(request.tags),
        "dependencies": list(request.dependencies),
        "risk": request.risk,
        "priority": request.priority,
        "max_attempts": request.max_attempts,
    }
    if _declares(schema_version, ADMISSION_BASE_COMMIT_SINCE):
        # The canonical resolved form, so a caller can compare what it asked for against what
        # Forge made durable without opening the database.
        payload["admission_base_commit"] = admission_base_commit
    return payload


def _defaults_payload(
    request: TaskRequest, config: ForgeConfig, task_id: str, state: TaskState
) -> dict[str, Any]:
    resolved: dict[str, Any] = {"state": state.value, "token_limit": config.max_task_tokens}
    values: dict[str, Any] = {
        "risk": request.risk,
        "priority": request.priority,
        "max_attempts": request.max_attempts,
        "acceptance_criteria": list(request.acceptance),
        "architecture_decisions": list(request.architecture_decisions),
        "constraints": list(request.constraints),
        "scope": request.scope,
        "out_of_scope": request.out_of_scope,
        "validation_commands": list(request.validation_commands),
        "tags": list(request.tags),
        "dependencies": list(request.dependencies),
        "task_id": task_id,
    }
    for name in request.defaults_applied:
        resolved[name] = values[name]
    return resolved


EMPTY_TOKEN_USAGE = empty_token_usage()


def _task_payload(
    task: Task,
    *,
    config: ForgeConfig,
    project: Project,
    token_usage: Mapping[str, Any],
    runtime: Mapping[str, Any],
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> dict[str, Any]:
    payload = task_to_public(
        task,
        project_name=project.name,
        token_usage=token_usage,
        token_limit=config.max_task_tokens,
        runtime=runtime,
        schema_version=schema_version,
    )
    payload["acceptance_criteria"] = list(task.acceptance)
    payload["architecture_decisions"] = list(task.architecture_decisions)
    payload["constraints"] = list(task.constraints)
    payload["scope"] = task.scope
    payload["out_of_scope"] = task.out_of_scope
    payload["validation_commands"] = [redact_command(command) for command in task.validation_commands]
    payload["tags"] = list(task.tags)
    payload["dependencies"] = list(task.dependencies)
    payload["priority"] = task.priority
    payload["planned_branch"] = planned_branch(task.id, task.title)
    payload["planned_worktree"] = planned_worktree(config, project, task.id)
    return payload


def _preview_task(
    request: TaskRequest,
    project: Project,
    task_id: str,
    state: TaskState,
    admission_base_commit: str | None,
) -> Task:
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
        architecture_decisions=list(request.architecture_decisions),
        constraints=list(request.constraints),
        scope=request.scope,
        out_of_scope=request.out_of_scope,
        max_attempts=request.max_attempts,
        admission_base_commit=admission_base_commit,
    )


def task_conflict_error(task_id: str, *, client_supplied: bool) -> TaskSubmissionError:
    """The refusal for an id that already names a different task contract.

    Shared by every task-add path so one durable identity never reports two different failures
    depending on which entry point reached it.
    """
    if client_supplied:
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


def _conflict_error(request: TaskRequest, task_id: str) -> TaskSubmissionError:
    return task_conflict_error(task_id, client_supplied=bool(request.task_id))


def _base_mismatch_error(
    task_id: str, requested: str | None, existing: str | None
) -> TaskSubmissionError:
    return TaskSubmissionError(
        "TASK_BASE_MISMATCH",
        "The task id already identifies a task admitted from a different base commit. Admission "
        "provenance is immutable; submit a new specification revision under a new task id rather "
        "than rebinding this one.",
        {
            "task_id": task_id,
            "requested_admission_base_commit": requested,
            "admission_base_commit": existing,
        },
    )


def _require_same_admission_base(
    task_id: str, requested: str | None, existing: Task
) -> None:
    """Refuse a resubmission whose admission base differs from the durable one.

    Checked before the content fingerprint so the refusal names the actual disagreement rather
    than reporting a generic contract conflict. Both directions are refused, and so is the legacy
    case: a task persisted before exact admission bases existed has none, and a later request
    carrying one would be binding it to provenance it never had, which would be a fabrication
    rather than an idempotent repeat. The stored value is never rewritten here or anywhere else.
    """
    if (requested or None) != (existing.admission_base_commit or None):
        raise _base_mismatch_error(task_id, requested, existing.admission_base_commit)


def reconcile_existing_task(
    existing: Task,
    *,
    task_id: str,
    admission_base_commit: str | None,
    fingerprint: str,
    conflict: Callable[[], TaskSubmissionError],
) -> None:
    """The durable idempotency law for a task id that is already taken.

    One law, one place, every entry point. Same id with the same admission base and the same
    contract is an idempotent repeat and adopts the durable task; the same id with a different
    admission base -- in either direction, including a legacy task that has none -- is a base
    mismatch and the stored provenance is left exactly as it is; the same id and base with
    different contract material is the caller's conflict. The base is checked first so a
    disagreement about provenance is never reported as a generic contract conflict.

    The submitted side is passed as captured intent rather than as a live object. What must be
    compared is what this caller asked to insert; re-deriving it from a mutable task after a
    losing race would compare the winner against itself.
    """
    _require_same_admission_base(task_id, admission_base_commit, existing)
    if task_fingerprint(existing) != fingerprint:
        raise conflict()


@dataclass(frozen=True, slots=True)
class TaskAdmission:
    """The outcome of admitting one task through the shared law.

    `task` is the durable row, except on a dry run that would create one, where it is the preview
    the caller supplied and `would_create` says so.
    """

    created: bool
    would_create: bool
    task: Task


def admit_task(
    db: ForgeDB,
    config: ForgeConfig,
    project: Project,
    task: Task,
    *,
    conflict: Callable[[], TaskSubmissionError],
    dry_run: bool = False,
) -> TaskAdmission:
    """Admit one task, or converge on the durable task that already holds its id.

    This is the only durable task-add seam. Both CLI paths reach it, so an id that is already
    taken is reconciled by `reconcile_existing_task` rather than surfacing as a raw database
    integrity error from one path and a structured refusal from the other. A concurrent writer
    that wins the primary key is reconciled by exactly the same law, so two simultaneous adds of
    the same task and base converge on one row, and two adds that disagree about the base leave
    one winner and one structured mismatch with the stored base unrotated.
    """
    # Captured before anything is read or written, so every reconciliation below compares the
    # durable row against what this call asked for.
    admission_base_commit = task.admission_base_commit
    fingerprint = task_fingerprint(task)

    with _database_scope():
        existing = db.get_task(task.id)
    if existing is not None:
        reconcile_existing_task(
            existing,
            task_id=task.id,
            admission_base_commit=admission_base_commit,
            fingerprint=fingerprint,
            conflict=conflict,
        )
        return TaskAdmission(created=False, would_create=False, task=existing)

    collision = task_id_collision(db, config, project, task.id)
    if collision is not None:
        raise _collision_error(task.id, collision)

    if dry_run:
        return TaskAdmission(created=False, would_create=True, task=task)

    created = _insert_once(
        db,
        task,
        task_id=task.id,
        admission_base_commit=admission_base_commit,
        fingerprint=fingerprint,
        config=config,
        project=project,
        conflict=conflict,
    )
    with _database_scope():
        stored = db.get_task(task.id)
    if stored is None:
        raise TaskSubmissionError(
            "DATABASE_ERROR",
            "The task could not be read back after submission.",
            {"task_id": task.id},
        )
    return TaskAdmission(created=created, would_create=False, task=stored)


def _locked_project_guard(connection, project: Project) -> None:
    """Refuse submission into a project whose evidence namespace cannot be proven safe.

    Runs on the caller's connection — under the insert's own immediate transaction on the
    insertion path — so the registration state it approves is the state the insert commits
    against. A legacy stored name that is not a safe path component cannot address a workspace
    or evidence directory at all, and a name aliasing another registered project's Windows
    physical name shares one on-disk namespace with it; either makes single-generation evidence
    unprovable, so submission is refused before any task is inserted. Details carry only the
    backend-minted ids, never a possibly path-shaped stored name.
    """
    row = connection.execute("SELECT name FROM projects WHERE id=?", (project.id,)).fetchone()
    if row is None:
        raise TaskSubmissionError(
            "PROJECT_NOT_FOUND",
            "The project is no longer registered.",
            {"project_id": project.id},
        )
    name = row["name"]
    if not windows_safe_component(name):
        raise TaskSubmissionError(
            "PROJECT_UNSAFE",
            "The project's stored name is not a safe path component, so its workspace and "
            "evidence namespaces cannot be proven safe. Re-register the project under a safe "
            "name before submitting tasks.",
            {"project_id": project.id},
        )
    key = windows_alias_key(name)
    for other in connection.execute(
        "SELECT id, name FROM projects WHERE id != ?", (project.id,)
    ):
        if windows_alias_key(other["name"]) == key:
            raise TaskSubmissionError(
                "PROJECT_UNSAFE",
                "Another registered project aliases this project's Windows physical name, so "
                "their workspace and evidence namespaces collide. Resolve the aliased "
                "registration before submitting tasks.",
                {"project_id": project.id, "collides_with_project_id": other["id"]},
            )


def _locked_task_collision(connection, config: ForgeConfig, project: Project, task_id: str) -> str | None:
    """Find a generation or case alias for the task id, using the caller's connection.

    The project registration is guarded first: an unsafe or aliased project makes every
    task-level namespace answer meaningless, so it refuses before any task check runs.
    """
    _locked_project_guard(connection, project)
    key = windows_alias_key(task_id)
    for row in connection.execute("SELECT id FROM tasks WHERE project_id=?", (project.id,)):
        if row["id"] != task_id and windows_alias_key(row["id"]) == key:
            return row["id"]
    return retained_evidence_claim(config, project.name, task_id)


def task_id_collision(
    db: ForgeDB, config: ForgeConfig, project: Project, task_id: str
) -> str | None:
    """Find the generation or case alias that makes a new task id unavailable.

    A live task in the same project whose id differs only by case would share the new task's
    worktree slug and evidence directory on a case-insensitive filesystem. Retained evidence of a
    deleted task — exact or case variant — must never be adopted by a new generation.
    """
    try:
        with _database_scope():
            with db.connect() as connection:
                return _locked_task_collision(connection, config, project, task_id)
    except EvidenceError as exc:
        raise TaskSubmissionError(
            "FILESYSTEM_ERROR",
            "The evidence tree could not be checked for a retained task id.",
            {"task_id": task_id},
        ) from exc


def _collision_error(task_id: str, collision: str) -> TaskSubmissionError:
    return TaskSubmissionError(
        "TASK_ID_COLLISION",
        "The task id collides with retained evidence of a deleted task or with a case "
        "variant of a live task in this project; evidence roots are single-generation. "
        "Submit a new task id.",
        {"task_id": task_id, "collides_with": collision},
    )


def insert_task_guarded(db: ForgeDB, config: ForgeConfig, project: Project, task: Task) -> None:
    """Insert a task atomically with the alias check.

    The collision check and the insert share one immediate transaction, so two concurrent
    submissions with case-variant ids cannot both pass the check and both insert.
    """

    def guard(connection) -> None:
        try:
            collision = _locked_task_collision(connection, config, project, task.id)
        except EvidenceError as exc:
            raise TaskSubmissionError(
                "FILESYSTEM_ERROR",
                "The evidence tree could not be checked for a retained task id.",
                {"task_id": task.id},
            ) from exc
        if collision is not None:
            raise _collision_error(task.id, collision)

    db.add_task_guarded(task, guard)


def _persisted_task_payload(
    db: ForgeDB,
    task: Task,
    *,
    config: ForgeConfig,
    project: Project,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> dict[str, Any]:
    with _database_scope():
        token_usage = db.task_token_usage(task.id)
        runtime = db.task_runtime(task.id)
    return _task_payload(
        task,
        config=config,
        project=project,
        token_usage=token_usage,
        runtime=runtime,
        schema_version=schema_version,
    )


def _insert_once(
    db: ForgeDB,
    task: Task,
    *,
    task_id: str,
    admission_base_commit: str | None,
    fingerprint: str,
    config: ForgeConfig,
    project: Project,
    conflict: Callable[[], TaskSubmissionError],
) -> bool:
    """Insert the task, returning whether this call created it.

    A primary-key violation means a concurrent writer inserted the same identifier first. The
    racing row is then reconciled by the same law an already-present row is: an identical request
    adopts it so a client retry never produces a duplicate task, a different admission base is a
    base mismatch, and different contract material is a conflict.
    """
    try:
        insert_task_guarded(db, config, project, task)
        return True
    except sqlite3.IntegrityError as exc:
        with _database_scope():
            racing = db.get_task(task_id)
        if racing is None:
            # No row holds this identifier, so the violation was not a duplicate id — a foreign
            # key or another constraint failed. Reporting a conflict would be misleading.
            raise TaskSubmissionError(
                "DATABASE_ERROR", "The Forge database rejected the task."
            ) from exc
        reconcile_existing_task(
            racing,
            task_id=task_id,
            admission_base_commit=admission_base_commit,
            fingerprint=fingerprint,
            conflict=conflict,
        )
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
    config: ForgeConfig,
    request: TaskRequest,
    *,
    dry_run: bool,
    db: ForgeDB | None = None,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Validate a request and, unless `dry_run`, persist it exactly once.

    Returns the `data` payload of the envelope for `schema_version`. Raises `TaskSubmissionError`
    for every expected failure so the caller emits one stable structured error.
    """
    db = db or open_database(config)
    project = _resolve_project(db, request.project)
    # Resolved before anything is read or written: a base that does not name a commit in this
    # repository is refused on its own terms, never adopted and never silently replaced.
    admission_base_commit = resolve_base_commit(project, request.base_commit)
    task_id = derive_task_id(request, project)
    state = resolve_state(request.dependencies)

    normalized = _normalized_request_payload(
        request, project, task_id, admission_base_commit, schema_version
    )
    defaults = _defaults_payload(request, config, task_id, state)

    admission = admit_task(
        db,
        config,
        project,
        _preview_task(request, project, task_id, state, admission_base_commit),
        conflict=lambda: _conflict_error(request, task_id),
        dry_run=dry_run,
    )

    if admission.would_create:
        task_payload = _task_payload(
            admission.task,
            config=config,
            project=project,
            token_usage=EMPTY_TOKEN_USAGE,
            runtime={},
            schema_version=schema_version,
        )
    else:
        task_payload = _persisted_task_payload(
            db, admission.task, config=config, project=project, schema_version=schema_version
        )

    return {
        "dry_run": dry_run,
        "created": admission.created,
        "would_create": admission.would_create,
        "task": task_payload,
        "normalized_request": normalized,
        "defaults_applied": defaults,
    }
