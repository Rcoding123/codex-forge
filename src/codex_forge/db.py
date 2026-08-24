from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from .models import Project, Task, TaskState
from .usage import aggregate_token_usage
from .utils import json_dumps, json_loads, new_repository_registration_id, utc_now


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
    repository_registration_id TEXT NOT NULL DEFAULT '',
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
    architecture_decisions_json TEXT NOT NULL DEFAULT '[]',
    constraints_json TEXT NOT NULL DEFAULT '[]',
    scope TEXT NOT NULL DEFAULT '',
    out_of_scope TEXT NOT NULL DEFAULT '',
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


# The states in which a live or interrupted worker owns the task. Deletion and restart recovery
# must agree on this set: a task in one of these states is never the CLI's to remove.
ACTIVE_TASK_STATES: tuple[TaskState, ...] = (
    TaskState.IMPLEMENTING,
    TaskState.VALIDATING,
    TaskState.REVIEWING,
    TaskState.REPAIRING,
)


class ProjectAlreadyRegistered(RuntimeError):
    """A project id or name is already registered.

    Registration creates; it never replaces a registration. Re-registering an existing project is
    refused so that its repository path, default branch and policies cannot be silently replaced.
    """

    def __init__(self, existing: Project):
        super().__init__(f"Project is already registered: {existing.id}")
        self.existing = existing


class RepositoryAlreadyRegistered(RuntimeError):
    """The requested repository path is already claimed by a different registration."""

    def __init__(self, existing: Project):
        super().__init__(f"Repository path is already registered to {existing.id}")
        self.existing = existing


# A project row whose registration identity has not been established yet: either it predates the
# column, or it was added by a migration that has not reached that row. `NOT NULL DEFAULT ''` is why
# both forms have to be named -- `ALTER TABLE ... ADD COLUMN` gives existing rows the default.
_REGISTRATION_UNSET = (
    "(repository_registration_id IS NULL OR repository_registration_id='')"
)
_UNSETTLED_REGISTRATION_COUNT = f"SELECT COUNT(*) FROM projects WHERE {_REGISTRATION_UNSET}"


@dataclass(frozen=True, slots=True)
class ProjectRelocation:
    """The durable outcome of one explicit relocation request.

    `outcome` is one of `relocated`, `unchanged`, `not_found`, `ambiguous`, `path_claimed` or
    `active_tasks`. Only `relocated` and `unchanged` carry a registration the caller may report.
    """

    outcome: str
    project: Project | None = None
    active_tasks: int = 0
    conflict: Project | None = None


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
            self._add_column_if_missing(
                connection, "tasks", "architecture_decisions_json", "TEXT NOT NULL DEFAULT '[]'"
            )
            self._add_column_if_missing(
                connection, "tasks", "constraints_json", "TEXT NOT NULL DEFAULT '[]'"
            )
            self._add_column_if_missing(connection, "tasks", "scope", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing(
                connection, "tasks", "out_of_scope", "TEXT NOT NULL DEFAULT ''"
            )
            self._add_column_if_missing(connection, "runs", "effort", "TEXT NOT NULL DEFAULT 'medium'")
            self._establish_repository_registration(connection)

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _establish_repository_registration(connection: sqlite3.Connection) -> None:
        """Add, backfill and index the registration column exactly once, under one write lock.

        `BEGIN IMMEDIATE` takes the database's write lock before the column is even inspected and
        holds it through the column addition, the backfill, the verification and the unique index,
        so this is one atomic unit for every concurrent initializer. Without it two Forge processes
        opening one legacy database each read the same unset row before either writes, both mint,
        and the second write replaces an identity the first had already made durable and a consumer
        may already hold -- a lost update that rotates an identity that is required to be immutable
        for the life of the registration. Serializing the writes alone would not have fixed it: the
        losing initializer had already decided to write, so it would still overwrite the winner
        once it acquired the lock. The read and the write have to be inside the same write
        transaction, which is what this takes. The same lock removes the second race in this
        migration, where two initializers both passed the `PRAGMA table_info` check and the loser's
        `ALTER TABLE` failed with `duplicate column name`.

        A second initializer blocks here until the first commits, and then finds the column
        present, no row unset and the index built, so it does nothing.
        """
        connection.execute("BEGIN IMMEDIATE")
        ForgeDB._add_column_if_missing(
            connection, "projects", "repository_registration_id", "TEXT NOT NULL DEFAULT ''"
        )
        ForgeDB._mint_missing_registration_ids(connection)
        unsettled = connection.execute(_UNSETTLED_REGISTRATION_COUNT).fetchone()[0]
        if unsettled:
            # The index is the durable statement that one registration has one identity. Building
            # it over rows that still share the column default would either fail or, worse, appear
            # to succeed on a single row; refuse loudly instead of leaving a half-migrated database.
            raise RuntimeError(
                f"{unsettled} project rows still have no repository registration identity"
            )
        # Created after the backfill, never in SCHEMA: on an upgraded database every pre-existing
        # row carries the column default until it is minted, and a unique index over those
        # identical values could not be built.
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_repository_registration "
            "ON projects(repository_registration_id)"
        )

    @staticmethod
    def _mint_missing_registration_ids(connection: sqlite3.Connection) -> None:
        """Give every project row that predates the column one persisted registration identity.

        Additive backfill, not derivation: each row receives its own freshly minted value, so a
        second run finds nothing to fill and cannot rotate an identity that already exists. Nothing
        else on the row is touched -- `id`, `name`, `repo_path`, `default_branch` and `updated_at`
        all stay exactly as they were, and every task, run and evidence record keeps naming the
        same project.

        Every write is a compare-and-set that applies only while the row is still unset, so the
        first durable assignment wins permanently: a writer that finds the identity already
        established writes nothing and thereby accepts the durable value. The caller holds the
        write lock, which makes that the whole guarantee rather than half of one; the predicate
        states the law in the statement itself so no future caller can reach this and rotate an
        identity by reading it a moment too early.
        """
        rows = connection.execute(
            f"SELECT id FROM projects WHERE {_REGISTRATION_UNSET}"
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE projects SET repository_registration_id=? "
                f"WHERE id=? AND {_REGISTRATION_UNSET}",
                (new_repository_registration_id(), row["id"]),
            )

    def add_project(self, project: Project) -> Project:
        """Register one new project and return it carrying its minted registration identity."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            registered = self._insert_project(connection, project)
        return registered

    def add_project_guarded(self, project: Project, guard) -> Project:
        """Register under one immediate transaction after the caller's guard approves."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            guard(connection)
            registered = self._insert_project(connection, project)
        return registered

    @staticmethod
    def _insert_project(connection: sqlite3.Connection, project: Project) -> Project:
        """Create one registration. It never updates an existing one.

        The repository path, the default branch and the policy settings of a registration that
        already exists are never written here: a second registration of the same project is a
        refusal, not a silent replacement. `repository_registration_id` is minted here, by Forge,
        and whatever the caller's `Project` carried in that field is ignored.
        """
        existing = connection.execute(
            "SELECT * FROM projects WHERE id=? OR name=?", (project.id, project.name)
        ).fetchone()
        if existing is not None:
            raise ProjectAlreadyRegistered(ForgeDB._project_from_row(existing))
        claimed = connection.execute(
            "SELECT * FROM projects WHERE repo_path=?", (str(project.repo_path),)
        ).fetchone()
        if claimed is not None:
            raise RepositoryAlreadyRegistered(ForgeDB._project_from_row(claimed))
        now = utc_now()
        registration_id = new_repository_registration_id()
        connection.execute(
            """
            INSERT INTO projects(id,name,repo_path,default_branch,validation_json,auto_fetch,auto_commit,auto_push,repository_registration_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
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
                registration_id,
                now,
                now,
            ),
        )
        return replace(
            project,
            repository_registration_id=registration_id,
            created_at=now,
            updated_at=now,
        )

    def get_project(self, identifier: str) -> Project | None:
        # Deterministic resolution: an exact id match always wins over a name match, so an
        # identifier that collides across projects can never select an arbitrary row.
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id=?", (identifier,)
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM projects WHERE name=?", (identifier,)
                ).fetchone()
        return self._project_from_row(row) if row else None

    def list_projects(self) -> list[Project]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [self._project_from_row(row) for row in rows]

    def delete_project(self, identifier: str) -> tuple[str, Project | None, int]:
        """Atomically delete one project that owns no tasks.

        Returns ``("deleted", project, 0)``, ``("not_found", None, 0)``,
        ``("has_tasks", project, task_count)``, or ``("ambiguous", None, 0)`` when the identifier
        names one project's id and a different project's name — a destructive command never
        guesses. The emptiness check and the deletion share one immediate transaction, and the
        tasks->projects foreign key backstops the refusal. Nothing on disk is touched here.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            by_id = connection.execute(
                "SELECT * FROM projects WHERE id=?", (identifier,)
            ).fetchone()
            by_name = connection.execute(
                "SELECT * FROM projects WHERE name=?", (identifier,)
            ).fetchone()
            if by_id is not None and by_name is not None and by_id["id"] != by_name["id"]:
                connection.rollback()
                return ("ambiguous", None, 0)
            row = by_id if by_id is not None else by_name
            if row is None:
                connection.rollback()
                return ("not_found", None, 0)
            project = self._project_from_row(row)
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=?", (project.id,)
                ).fetchone()[0]
            )
            if count:
                connection.rollback()
                return ("has_tasks", project, count)
            connection.execute("DELETE FROM projects WHERE id=?", (project.id,))
        return ("deleted", project, 0)

    def relocate_project(self, identifier: str, repo_path: Path) -> ProjectRelocation:
        """Point one existing registration at a different local path, preserving its identity.

        Relocation is the only operation that ever changes `repo_path` after registration, and it
        changes nothing else. `id`, `name`, `default_branch`, the validation commands, the policy
        settings and above all `repository_registration_id` are carried through unchanged, so every
        task, run and evidence record keeps naming the same registration.

        Relocation is the operator's explicit assertion that this is the same repository in a new
        place; Forge does not infer repository sameness. It is refused while a worker owns a task
        of this project, because the repository underneath a live execution is not the caller's to
        move; it is refused when another registration already claims the path; and it mutates
        nothing when the path is already the registered one. Resolution, refusal and update share
        one immediate transaction.
        """
        target = str(repo_path)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            by_id = connection.execute(
                "SELECT * FROM projects WHERE id=?", (identifier,)
            ).fetchone()
            by_name = connection.execute(
                "SELECT * FROM projects WHERE name=?", (identifier,)
            ).fetchone()
            if by_id is not None and by_name is not None and by_id["id"] != by_name["id"]:
                connection.rollback()
                return ProjectRelocation("ambiguous")
            row = by_id if by_id is not None else by_name
            if row is None:
                connection.rollback()
                return ProjectRelocation("not_found")
            project = self._project_from_row(row)
            if str(project.repo_path) == target:
                connection.rollback()
                return ProjectRelocation("unchanged", project)
            claimed = connection.execute(
                "SELECT * FROM projects WHERE repo_path=? AND id<>?", (target, project.id)
            ).fetchone()
            if claimed is not None:
                connection.rollback()
                return ProjectRelocation(
                    "path_claimed", project, conflict=self._project_from_row(claimed)
                )
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=? AND state IN (?,?,?,?)",
                    (project.id, *(state.value for state in ACTIVE_TASK_STATES)),
                ).fetchone()[0]
            )
            if active:
                connection.rollback()
                return ProjectRelocation("active_tasks", project, active_tasks=active)
            connection.execute(
                "UPDATE projects SET repo_path=?, updated_at=? WHERE id=?",
                (target, utc_now(), project.id),
            )
            relocated = self._project_from_row(
                connection.execute("SELECT * FROM projects WHERE id=?", (project.id,)).fetchone()
            )
        return ProjectRelocation("relocated", relocated)

    def add_task(self, task: Task) -> None:
        with self.connect() as connection:
            self._insert_task(connection, task)

    def add_task_guarded(self, task: Task, guard) -> None:
        """Insert under one immediate transaction after the caller's guard approves.

        The guard runs with the write lock held and may raise to refuse the insert; a refusal
        rolls the transaction back. This is what makes an alias check atomic with its insert.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            guard(connection)
            self._insert_task(connection, task)

    @staticmethod
    def _insert_task(connection: sqlite3.Connection, task: Task) -> None:
        now = utc_now()
        connection.execute(
                """
                INSERT INTO tasks(
                    id,project_id,title,objective,acceptance_json,validation_json,state,priority,risk,
                    tags_json,dependencies_json,architecture_decisions_json,constraints_json,scope,
                    out_of_scope,attempts,max_attempts,branch,worktree_path,last_error,
                    failure_fingerprint,identical_failures,result_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    json_dumps(task.architecture_decisions),
                    json_dumps(task.constraints),
                    task.scope,
                    task.out_of_scope,
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

    def delete_task(self, task_id: str) -> tuple[str, Task | None]:
        """Atomically delete one non-running task and every row it owns.

        Returns ``("deleted", task)`` with the pre-deletion snapshot, ``("not_found", None)`` when
        no such row exists, or ``("active", task)`` when a worker owns the task. The state check
        and the child-first deletion run inside one immediate transaction, so the daemon's claim
        (``claim_ready_tasks``, also immediate) can never interleave: whichever transaction wins
        the write lock decides, and the loser observes the durable outcome. Evidence on disk is
        never touched here.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return ("not_found", None)
            task = self._task_from_row(row)
            if task.state in ACTIVE_TASK_STATES:
                connection.rollback()
                return ("active", task)
            connection.execute("DELETE FROM decisions WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
            placeholders = ",".join("?" for _ in ACTIVE_TASK_STATES)
            cursor = connection.execute(
                f"DELETE FROM tasks WHERE id=? AND state NOT IN ({placeholders})",
                (task_id, *(state.value for state in ACTIVE_TASK_STATES)),
            )
            if cursor.rowcount != 1:
                # Unreachable while the immediate transaction holds the write lock; failing loudly
                # beats committing a child-row deletion for a task that was not deleted.
                raise sqlite3.OperationalError(f"task {task_id} changed state during deletion")
        return ("deleted", task)

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

    def task_token_usage(self, task_id: str) -> dict[str, Any]:
        """Aggregate the usage every completed run of a task reported.

        The categories are derived from the persisted `usage_json` rather than from normalized
        columns, so runs recorded before the categories were exposed report the same detail as runs
        recorded today. `total_tokens` keeps its previous meaning and value — the raw
        `input_tokens + output_tokens` traffic — while the token budget is enforced against
        `usage.budget_used_tokens` of this same aggregate.
        """
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT usage_json FROM runs WHERE task_id=? AND completed_at IS NOT NULL", (task_id,)
            ).fetchall()
        return aggregate_token_usage(row["usage_json"] for row in rows)

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
        active_states = tuple(state.value for state in ACTIVE_TASK_STATES)
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
            repository_registration_id=row["repository_registration_id"],
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
            # `.get` with a default: rows persisted before these columns existed, and databases not
            # yet migrated by `initialize`, must stay readable as tasks with no binding contract.
            architecture_decisions=json_loads(row.get("architecture_decisions_json"), []),
            constraints=json_loads(row.get("constraints_json"), []),
            scope=row.get("scope") or "",
            out_of_scope=row.get("out_of_scope") or "",
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
