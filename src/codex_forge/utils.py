from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify(value: str, max_length: int = 64) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    return (slug or "task")[:max_length]


def stable_id(prefix: str, value: str, length: int = 10) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}-{digest}"


def new_repository_registration_id() -> str:
    """Mint one Forge-owned opaque repository-registration identity.

    Deliberately minted rather than derived. A value derived from the project name would be
    reproduced exactly by delete-then-recreate, which is a new registration of a repository
    that may not be the old one; a value derived from the repository path would change when
    the same registration is relocated. Neither is an identity. This is minted once, persisted,
    and never recomputed -- unlike `stable_id`, which exists to be recomputed.
    """
    return f"repo-reg-{secrets.token_hex(16)}"


WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in "123456789"}
    | {f"LPT{digit}" for digit in "123456789"}
)

_UNSAFE_COMPONENT_CHARACTERS = frozenset('\x00:/\\<>"|?*')


def windows_safe_component(value: str) -> bool:
    r"""The one shared Windows-safe path-component validator for project names and task ids.

    Rejects: empty, `.` and `..`; control characters; `/ \ : < > " | ? *`; leading or trailing
    whitespace and trailing dots (Windows strips them, so `NAME.` and `NAME ` alias `NAME`); and
    DOS device names with or without an extension (`CON.txt` still opens the console device).
    """
    if not value or value in {".", ".."}:
        return False
    if value != value.strip() or value.endswith("."):
        return False
    if any(ch in _UNSAFE_COMPONENT_CHARACTERS or ord(ch) < 32 for ch in value):
        return False
    return value.split(".", 1)[0].upper() not in WINDOWS_RESERVED_DEVICE_NAMES


def windows_alias_key(value: str) -> str:
    """The Windows physical-name equivalence class: case folded, trailing dots and spaces
    stripped — two names with one key occupy the same directory on Windows."""
    return value.rstrip(". ").casefold()


def worktree_directory_name(task_id: str) -> str:
    """Collision-resistant checkout directory name: lossy slug plus a hash of the exact id.

    Slugify alone is lossy — `TASK-A@B` and `TASK-A-B` collapse to one slug — so a hash of the
    exact id is folded into the name to keep every task's checkout distinct.
    """
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:10]
    return f"{slugify(task_id, 48)}-{digest}"


def derived_worktree_path(workspace_root: Path, project_name: str, task_id: str) -> Path:
    """The one shared derivation of a task's checkout path when none is persisted."""
    return workspace_root / project_name / worktree_directory_name(task_id)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_process(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=merged_env,
        check=check,
    )


def tail_text(text: str, max_chars: int = 12_000) -> str:
    if len(text) <= max_chars:
        return text
    return "[...truncated...]\n" + text[-max_chars:]


def flatten_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class ControllerAlreadyRunning(RuntimeError):
    pass


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def controller_lock(path: Path) -> Iterator[None]:
    """Hold a process-scoped, crash-released singleton lock for a controller session."""
    ensure_dir(path.parent)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        try:
            _lock_file(handle)
        except OSError as exc:
            raise ControllerAlreadyRunning(f"Another Codex Forge controller holds {path}") from exc
        try:
            yield
        finally:
            _unlock_file(handle)
