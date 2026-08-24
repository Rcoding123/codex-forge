from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class TaskState(StrEnum):
    BACKLOG = "BACKLOG"
    READY = "READY"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    REVIEWING = "REVIEWING"
    REPAIRING = "REPAIRING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    NEEDS_DECISION = "NEEDS_DECISION"


@dataclass(slots=True)
class Project:
    id: str
    name: str
    repo_path: Path
    default_branch: str = "main"
    validation_commands: list[str] = field(default_factory=list)
    auto_fetch: bool = True
    auto_commit: bool = True
    auto_push: bool = False
    # Minted and owned by Forge, never supplied by a caller: `add_project` ignores whatever is
    # here and persists its own value, and a read fills it in from the durable row.
    repository_registration_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class Task:
    id: str
    project_id: str
    title: str
    objective: str
    acceptance: list[str]
    validation_commands: list[str]
    state: TaskState
    priority: int = 100
    risk: str = "medium"
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    architecture_decisions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    scope: str = ""
    out_of_scope: str = ""
    attempts: int = 0
    max_attempts: int = 3
    branch: str | None = None
    worktree_path: Path | None = None
    last_error: str | None = None
    failure_fingerprint: str | None = None
    identical_failures: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class ValidationResult:
    command: str
    exit_code: int
    passed: bool
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    summary: str


@dataclass(slots=True)
class CodexRunResult:
    exit_code: int
    final: dict[str, Any]
    jsonl_path: Path
    final_path: Path
    stderr_path: Path
    thread_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
