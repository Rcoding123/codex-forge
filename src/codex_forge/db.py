from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import Project, Task, TaskState
from .utils import json_dumps, json_loads, utc_now


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    repo_path TEXT NOT NULL UNIQUE,
    default_branch TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    auto_fetch INTEGER NOT NULL DEFAULT 1,
    auto_commit INTEGER NOT NULL DEFAULT 1,
    auto_push INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    risk TEXT NOT NULL DEFAULT 'medium',
    tags_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    branch TEXT,
    worktree_path TEXT,
    last_error TEXT,
    failure_fingerprint TEXT,
    identical_failures INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_state_priority ON tasks(state, priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    recommendation TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN',
    answer TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    phase TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL DEFAULT 'medium',
    thread_id TEXT,
    exit_code INTEGER,
    usage_json TEXT NOT NULL DEFAULT '{}',
    artifact_dir TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);
"""


class ForgeDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._add_column_if_missing(connection, "tasks", "failure_fingerprint", "TEXT")
            self._add_column_if_missing(
                connection, "tasks", "identical_failures", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(connection, "runs", "effort", "TEXT NOT NULL DEFAULT 'medium'")

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def add_project(self, project: Project) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO projects(id,name,repo_path,default_branch,validation_json,auto_fetch,auto_commit,auto_push,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    repo_path=excluded.repo_path,
                    default_branch=excluded.default_branch,
                    validation_json=excluded.validation_json,
                    auto_fetch=excluded.auto_fetch,
                    auto_commit=excluded.auto_commit,
                    auto_push=excluded.auto_push,
                    updated_at=excluded.updated_at
                """,
                (
                    project.id,
                    project.name,
                    str(project.repo_path),
                    project.default_branch,
                    json_dumps(project.validation_commands),
                    int(project.auto_fetch),
                    int(project.auto_commit),
                    int(project.auto_push),
                    now,
                    now,
                ),
            )

    def get_project(self, identifier: str) -> Project | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id=? OR name=?", (identifier, identifier)
            ).fetchone()
        return self._project_from_row(row) if row else None

    def list_projects(self) -> list[Project]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [self._project_from_row(row) for row in rows]

    def add_task(self, task: Task) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id,project_id,title,objective,acceptance_json,validation_json,state,priority,risk,
                    tags_json,dependencies_json,attempts,max_attempts,branch,worktree_path,last_error,
                    failure_fingerprint,identical_failures,result_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task.id,
                    task.project_id,
                    task.title,
                    task.objective,
                    json_dumps(task.acceptance),
                    json_dumps(task.validation_commands),
                    task.state.value,
                    task.priority,
                    task.risk,
                    json_dumps(task.tags),
                    json_dumps(task.dependencies),
                    task.attempts,
                    task.max_attempts,
                    task.branch,
                    str(task.worktree_path) if task.worktree_path else None,
                    task.last_error,
                    task.failure_fingerprint,
                    task.identical_failures,
                    json_dumps(task.result),
                    now,
                    now,
                ),
            )

    def get_task(self, task_id: str) -> Task | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def list_tasks(self, state: TaskState | None = None) -> list[Task]:
        query = "SELECT * FROM tasks"
        params: tuple[str, ...] = ()
        if state:
            query += " WHERE state=?"
            params = (state.value,)
        query += " ORDER BY priority DESC, created_at ASC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._task_from_row(row) for row in rows]

    def refresh_ready_states(self) -> None:
        with self.connect() as connection:
            rows = connection.execute("SELECT id,dependencies_json,state FROM tasks").fetchall()
            completed = {
                row["id"]
                for row in connection.execute("SELECT id FROM tasks WHERE state=?", (TaskState.COMPLETED.value,))
            }
            now = utc_now()
            for row in rows:
                if row["state"] not in (TaskState.BACKLOG.value, TaskState.READY.value):
                    continue
                dependencies = json_loads(row["dependencies_json"], [])
                new_state = TaskState.READY.value if all(dep in completed for dep in dependencies) else TaskState.BACKLOG.value
                if new_state != row["state"]:
                    connection.execute(
                        "UPDATE tasks SET state=?,updated_at=? WHERE id=?",
                        (new_state, now, row["id"]),
                    )

    def claim_ready_tasks(self, limit: int) -> list[Task]:
        self.refresh_ready_states()
        claimed: list[Task] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM tasks WHERE state=? ORDER BY priority DESC, created_at ASC LIMIT ?",
                (TaskState.READY.value, limit),
            ).fetchall()
            now = utc_now()
            for row in rows:
                connection.execute(
                    "UPDATE tasks SET state=?,attempts=attempts+1,updated_at=? WHERE id=? AND state=?",
                    (TaskState.IMPLEMENTING.value, now, row["id"], TaskState.READY.value),
                )
                updated = dict(row)
                updated["state"] = TaskState.IMPLEMENTING.value
                updated["attempts"] = row["attempts"] + 1
                claimed.append(self._task_from_mapping(updated))
        return claimed

    def update_task(
        self,
        task_id: str,
        *,
        state: TaskState | None = None,
        branch: str | None = None,
        worktree_path: Path | None = None,
        last_error: str | None = None,
        failure_fingerprint: str | None = None,
        identical_failures: int | None = None,
        result: dict | None = None,
    ) -> None:
        fields: list[str] = ["updated_at=?"]
        values: list[object] = [utc_now()]
        if state is not None:
            fields.append("state=?")
            values.append(state.value)
        if branch is not None:
            fields.append("branch=?")
            values.append(branch)
        if worktree_path is not None:
            fields.append("worktree_path=?")
            values.append(str(worktree_path))
        if last_error is not None:
            fields.append("last_error=?")
            values.append(last_error)
        if failure_fingerprint is not None:
            fields.append("failure_fingerprint=?")
            values.append(failure_fingerprint)
        if identical_failures is not None:
            fields.append("identical_failures=?")
            values.append(identical_failures)
        if result is not None:
            fields.append("result_json=?")
            values.append(json_dumps(result))
        values.append(task_id)
        with self.connect() as connection:
            connection.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", values)

    def add_decision(
        self,
        task_id: str,
        question: str,
        options: list[str],
        recommendation: str | None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO decisions(task_id,question,options_json,recommendation,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, question, json_dumps(options), recommendation, "OPEN", now, now),
            )

    def list_open_decisions(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM decisions WHERE status='OPEN' ORDER BY created_at"
            ).fetchall()

    def record_run_start(
        self, task_id: str, phase: str, model: str, effort: str, artifact_dir: Path
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO runs(task_id,phase,model,effort,artifact_dir,started_at) VALUES(?,?,?,?,?,?)",
                (task_id, phase, model, effort, str(artifact_dir), utc_now()),
            )
            return int(cursor.lastrowid)

    def record_run_end(
        self,
        run_id: int,
        *,
        thread_id: str | None,
        exit_code: int,
        usage: dict,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET thread_id=?,exit_code=?,usage_json=?,completed_at=? WHERE id=?",
                (thread_id, exit_code, json_dumps(usage), utc_now(), run_id),
            )

    def task_token_usage(self, task_id: str) -> dict[str, int]:
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT usage_json FROM runs WHERE task_id=? AND completed_at IS NOT NULL", (task_id,)
            ).fetchall()
        for row in rows:
            usage = json_loads(row["usage_json"], {})
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            totals["input_tokens"] += input_tokens
            totals["output_tokens"] += output_tokens
        totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
        return totals

    def task_runtime(self, task_id: str) -> dict[str, str | None]:
        with self.connect() as connection:
            first = connection.execute(
                "SELECT started_at FROM runs WHERE task_id=? ORDER BY id ASC LIMIT 1", (task_id,)
            ).fetchone()
            latest = connection.execute(
                "SELECT phase,artifact_dir FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
            ).fetchone()
        return {
            "started_at": first["started_at"] if first else None,
            "current_phase": latest["phase"] if latest else None,
            "evidence_path": latest["artifact_dir"] if latest else None,
        }

    def recover_interrupted_tasks(self) -> list[dict[str, str]]:
        active_states = tuple(
            state.value
            for state in (
                TaskState.IMPLEMENTING,
                TaskState.VALIDATING,
                TaskState.REVIEWING,
                TaskState.REPAIRING,
            )
        )
        placeholders = ",".join("?" for _ in active_states)
        recovered: list[dict[str, str]] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT id,state,attempts,max_attempts FROM tasks WHERE state IN ({placeholders})",
                active_states,
            ).fetchall()
            for row in rows:
                new_state = (
                    TaskState.READY if int(row["attempts"]) < int(row["max_attempts"]) else TaskState.FAILED
                )
                reason = (
                    f"RECOVERED_AFTER_RESTART: task was {row['state']}; retained its branch and worktree."
                )
                connection.execute(
                    "UPDATE tasks SET state=?,last_error=?,updated_at=? WHERE id=?",
                    (new_state.value, reason, utc_now(), row["id"]),
                )
                recovered.append(
                    {"task_id": row["id"], "previous_state": row["state"], "state": new_state.value}
                )
        return recovered

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            repo_path=Path(row["repo_path"]),
            default_branch=row["default_branch"],
            validation_commands=json_loads(row["validation_json"], []),
            auto_fetch=bool(row["auto_fetch"]),
            auto_commit=bool(row["auto_commit"]),
            auto_push=bool(row["auto_push"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        return ForgeDB._task_from_mapping(dict(row))

    @staticmethod
    def _task_from_mapping(row: dict) -> Task:
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            objective=row["objective"],
            acceptance=json_loads(row["acceptance_json"], []),
            validation_commands=json_loads(row["validation_json"], []),
            state=TaskState(row["state"]),
            priority=int(row["priority"]),
            risk=row["risk"],
            tags=json_loads(row["tags_json"], []),
            dependencies=json_loads(row["dependencies_json"], []),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            branch=row.get("branch"),
            worktree_path=Path(row["worktree_path"]) if row.get("worktree_path") else None,
            last_error=row.get("last_error"),
            failure_fingerprint=row.get("failure_fingerprint"),
            identical_failures=int(row.get("identical_failures") or 0),
            result=json_loads(row.get("result_json"), {}),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
