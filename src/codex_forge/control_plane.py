from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Mapping, TextIO

from .models import Project, Task
from .utils import json_loads


SCHEMA_VERSION = "1.0"
_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<prefix>(?:^|[\s;&])(?:\$env:)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s;&]+)"
)
_SECRET_ARGUMENT_PATTERN = re.compile(
    r"(?P<prefix>--?(?:api[-_]?key|auth|cookie|password|secret|token)(?:=|\s+))"
    r"(?P<value>\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_URL_CREDENTIAL_PATTERN = re.compile(r"(?P<scheme>://)[^/@\s]+:[^/@\s]+@")


# Stable structured error codes and the process exit code each one reports. Exit code 1 marks an
# operational failure; exit code 2 marks an invocation, configuration, or requested-resource
# failure. Consumers must branch on the code, not on the message text.
ERROR_EXIT_CODES: dict[str, int] = {
    "TASK_NOT_FOUND": 2,
    "INVALID_ARGUMENT": 2,
    "CONFIG_ERROR": 2,
    "COMMAND_FAILED": 1,
    "INVALID_TASK_REQUEST": 2,
    "PROJECT_NOT_FOUND": 2,
    "DUPLICATE_TASK_ID": 2,
    "TASK_ID_CONFLICT": 2,
    "INVALID_VALIDATION_COMMAND": 2,
    "INVALID_LIMIT": 2,
    "CONFIGURATION_ERROR": 2,
    "DATABASE_ERROR": 1,
    "EVIDENCE_NOT_FOUND": 2,
    "EVIDENCE_UNAVAILABLE": 2,
    "ARTIFACT_NOT_FOUND": 2,
    "ARTIFACT_NOT_PUBLIC": 2,
    "ARTIFACT_OUTSIDE_EVIDENCE_ROOT": 2,
    "ARTIFACT_UNSAFE_TYPE": 2,
    "ARTIFACT_TOO_LARGE": 2,
    "ARTIFACT_INVALID_UTF8": 2,
    "UNSUPPORTED_SCHEMA": 2,
    # Retryable: the artifact was being written while it was read. The caller may repeat the call.
    "ARTIFACT_CHANGED_DURING_READ": 1,
    "FILESYSTEM_ERROR": 1,
}


def error_exit_code(code: str) -> int:
    """Return the documented process exit code for a structured error code."""
    return ERROR_EXIT_CODES.get(code, 1)


@dataclass(frozen=True, slots=True)
class StructuredError:
    code: str
    message: str
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


def response_envelope(
    command: str,
    *,
    data: Mapping[str, Any] | None = None,
    error: StructuredError | None = None,
    legacy_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "success": error is None,
        "data": dict(data) if data is not None else None,
        "error": error.to_dict() if error else None,
    }
    if legacy_fields:
        payload.update(legacy_fields)
    return payload


def _utf8_stream(stream: TextIO) -> TextIO:
    """Ensure the contract's promised UTF-8 encoding on a legacy console.

    A Windows console defaulting to a legacy code page cannot encode Unicode task titles, project
    names, or paths. Without this the response would raise instead of being emitted.
    """
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("_", "-")
    if encoding in {"utf-8", "utf8"}:
        return stream
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    return stream


def write_response(
    command: str,
    *,
    data: Mapping[str, Any] | None = None,
    error: StructuredError | None = None,
    legacy_fields: Mapping[str, Any] | None = None,
    stream: TextIO | None = None,
) -> None:
    output = _utf8_stream(stream or sys.stdout)
    document = response_envelope(
        command,
        data=data,
        error=error,
        legacy_fields=legacy_fields,
    )
    output.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def project_to_public(project: Project) -> dict[str, Any]:
    return {
        "project_id": project.id,
        "name": project.name,
        "repository_path": str(project.repo_path),
        "default_branch": project.default_branch,
        "validation_commands": [redact_command(command) for command in project.validation_commands],
        "fetch_policy": "automatic" if project.auto_fetch else "disabled",
        "commit_policy": "automatic" if project.auto_commit else "disabled",
        "push_policy": "automatic" if project.auto_push else "disabled",
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def redact_secret_arguments(text: str) -> str:
    """Redact credential-shaped CLI arguments and URL credentials.

    This is the subset of command redaction that is safe to apply to free-form captured output. It
    deliberately excludes the blanket `name=value` rule, which would destroy ordinary log lines such
    as `exit_code=0`.
    """
    redacted = _SECRET_ARGUMENT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", text
    )
    return _URL_CREDENTIAL_PATTERN.sub(r"\g<scheme>[REDACTED]@", redacted)


def redact_command(command: str) -> str:
    """Replace credential-shaped values in a command with `[REDACTED]`."""
    redacted = _ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group('prefix')}[REDACTED]", command)
    return redact_secret_arguments(redacted)


def _safe_task_error(last_error: str | None) -> tuple[str | None, str | None]:
    if not last_error:
        return None, None
    known_errors = {
        "TOKEN_BUDGET_EXCEEDED": "The task exceeded its configured token limit.",
        "LOOP_DETECTED": "The task stopped after repeated identical failures.",
        "RECOVERED_AFTER_RESTART": "The task was recovered after a controller restart.",
    }
    for code, message in known_errors.items():
        if last_error.startswith(code):
            return code, message
    return "TASK_ERROR", "The task has a recorded error. Inspect retained evidence locally."


def task_to_public(
    task: Task,
    *,
    project_name: str | None,
    token_usage: Mapping[str, int],
    token_limit: int,
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    error_code, error_message = _safe_task_error(task.last_error)
    completed_at = task.result.get("completed_at")
    return {
        "task_id": task.id,
        "project": project_name,
        "project_id": task.project_id,
        "title": task.title,
        "objective": task.objective,
        "state": task.state.value,
        "risk": task.risk,
        "attempt_count": task.attempts,
        "max_attempts": task.max_attempts,
        "token_usage": dict(token_usage),
        "token_limit": token_limit,
        "branch": task.branch,
        "worktree": str(task.worktree_path) if task.worktree_path else None,
        "current_phase": runtime.get("current_phase"),
        "error_code": error_code,
        "error_message": error_message,
        "evidence_path": runtime.get("evidence_path"),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": runtime.get("started_at"),
        "completed_at": completed_at if isinstance(completed_at, str) else None,
    }


def decision_to_public(row: Mapping[str, Any]) -> dict[str, Any]:
    state = str(row["status"])
    return {
        "decision_id": int(row["id"]),
        "task_id": row["task_id"],
        "state": state,
        "question": row["question"],
        "reason": None,
        "options": json_loads(row["options_json"], []),
        "recommendation": row["recommendation"],
        "created_at": row["created_at"],
        "resolved_at": row["updated_at"] if state != "OPEN" else None,
        "resolution": row["answer"],
    }
