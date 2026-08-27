"""Immutable repository-registration identity: minting, refusal, relocation and migration.

`project_id` is `stable_id("project", name)`. It is derived from the project name, so it is
reproduced exactly by deleting a project and registering the name again, and it says nothing about
which repository the registration is bound to. It is therefore not durable repository identity, and
before this slice a second `project add` for a registered name silently replaced that project's
`repo_path`, `default_branch` and policies underneath every task already pointing at it.

`repository_registration_id` is the Forge-owned opaque identity of one repository registration. It
is minted by Forge, never supplied by a caller, never derived from the project name or the
repository path, immutable for the life of a registration, and rotated only by a new registration.
These tests drive the real CLI and the real database seams; none of them makes a model request.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from codex_forge import db as db_module
from codex_forge import task_submission
from codex_forge.cli import build_parser, main
from codex_forge.config import ForgeConfig, load_config
from codex_forge.control_plane import (
    EXPECTED_REPOSITORY_REGISTRATION_SINCE,
    LATEST_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
)
from codex_forge.db import ForgeDB
from codex_forge.evidence import evidence_list
from codex_forge.models import Task, TaskState
from codex_forge.orchestrator import Orchestrator
from codex_forge.task_submission import (
    TaskSubmissionError,
    build_task_request,
    derive_task_id,
    request_fingerprint,
    submit_task,
)
from codex_forge.utils import stable_id


def _configured(tmp_path: Path) -> ForgeConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        f"""
[paths]
workspace_root = '{(tmp_path / "worktrees").as_posix()}'
data_root = '{(tmp_path / "data").as_posix()}'
repository_root = '{(tmp_path / "repositories").as_posix()}'
inbox_root = '{(tmp_path / "inbox").as_posix()}'
database_path = '{(tmp_path / "data" / "forge.db").as_posix()}'
evidence_root = '{(tmp_path / "evidence").as_posix()}'
knowledge_root = '{(tmp_path / "knowledge").as_posix()}'

[codex]
approval_policy = 'never'
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_path)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True
    )


def _repository(path: Path) -> Path:
    """A real Git working tree, because registration and relocation both verify one."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Forge Registration Test")
    _git(path, "config", "user.email", "forge-registration@example.invalid")
    (path / "README.md").write_text("registration\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "base")
    return path


def _run(config: ForgeConfig, *arguments: str) -> int:
    return main(["--config", str(config.config_path), *arguments])


def _add(config: ForgeConfig, name: str, repo: Path, *extra: str) -> int:
    return _run(config, "project", "add", name, "--repo", str(repo), *extra)


def _list_json(config: ForgeConfig, capsys, *extra: str) -> dict:
    _run(config, "project", "list", "--json", *extra)
    return json.loads(capsys.readouterr().out)


def _registration(db: ForgeDB, identifier: str) -> str:
    project = db.get_project(identifier)
    assert project is not None
    return project.repository_registration_id


def _projects_row(config: ForgeConfig, project_id: str) -> dict:
    connection = sqlite3.connect(config.database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    finally:
        connection.close()
    return dict(row)


def _project_columns(config: ForgeConfig) -> set[str]:
    connection = sqlite3.connect(config.database_path)
    try:
        return {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    finally:
        connection.close()


def _connection(config: ForgeConfig) -> sqlite3.Connection:
    return sqlite3.connect(config.database_path)


def _drop_registration_column(config: ForgeConfig) -> None:
    """Reproduce the exact pre-column shape of the projects table, keeping every row."""
    connection = _connection(config)
    try:
        connection.execute("DROP INDEX IF EXISTS idx_projects_repository_registration")
        connection.execute("ALTER TABLE projects DROP COLUMN repository_registration_id")
        connection.commit()
    finally:
        connection.close()


def _empty_every_registration(config: ForgeConfig) -> None:
    """Reproduce interruption point A: the column exists, no row has been backfilled yet."""
    connection = _connection(config)
    try:
        connection.execute("DROP INDEX IF EXISTS idx_projects_repository_registration")
        connection.execute("UPDATE projects SET repository_registration_id=''")
        connection.commit()
    finally:
        connection.close()


# A durable statement of the invariant under test, installed by the test on the real table: any
# statement that writes over an identity a previous writer already established aborts. It is
# blind to how the migration is implemented -- it fires on the write itself -- so it detects a
# rotation no matter which initializer performs it.
_WRITE_ONCE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS registration_identity_is_write_once
BEFORE UPDATE OF repository_registration_id ON projects
WHEN OLD.repository_registration_id IS NOT NULL AND OLD.repository_registration_id <> ''
BEGIN
    SELECT RAISE(ABORT, 'repository registration identity rotated');
END;
"""


def _forbid_rotation(config: ForgeConfig) -> None:
    connection = _connection(config)
    try:
        connection.execute(_WRITE_ONCE_TRIGGER)
        connection.commit()
    finally:
        connection.close()


def _indexes(config: ForgeConfig) -> set[str]:
    connection = _connection(config)
    try:
        return {row[1] for row in connection.execute("PRAGMA index_list(projects)")}
    finally:
        connection.close()


def _initialize_concurrently(config: ForgeConfig, monkeypatch, workers: int = 2) -> list[str]:
    """Run `workers` real initializers over one database, forced through the dangerous timeline.

    The only hook is `new_repository_registration_id`, the real minting seam, which an initializer
    reaches after it has selected the rows it believes are unset and before it writes any of them.
    Holding the first arrival there until another one arrives is what makes a lost update happen
    rather than merely being possible. Nothing in production synchronizes on it: when the migration
    holds the write lock, a second initializer cannot reach the seam at all, the barrier times out,
    and the first one proceeds -- which is exactly the behaviour under test.
    """
    minted: list[str] = []
    lock = threading.Lock()
    paused = threading.Event()
    meet = threading.Barrier(workers, timeout=0.5)
    real_mint = db_module.new_repository_registration_id

    def hooked_mint() -> str:
        if not paused.is_set():
            paused.set()
            try:
                meet.wait()
            except threading.BrokenBarrierError:
                pass
        value = real_mint()
        with lock:
            minted.append(value)
        return value

    monkeypatch.setattr(db_module, "new_repository_registration_id", hooked_mint)

    failures: list[BaseException] = []

    def initialize() -> None:
        try:
            ForgeDB(config.database_path).initialize()
        except BaseException as exc:  # noqa: BLE001 - reported verbatim to the assertion below
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=initialize, name=f"init-{n}") for n in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    assert not any(thread.is_alive() for thread in threads), "an initializer never finished"
    assert failures == [], f"an initializer failed: {failures!r}"
    return minted


def _registered(tmp_path: Path, capsys, name: str = "alpha") -> tuple[ForgeConfig, ForgeDB, Path]:
    config = _configured(tmp_path)
    repo = _repository(tmp_path / f"{name}-repo")
    assert _add(config, name, repo) == 0
    capsys.readouterr()
    return config, ForgeDB(config.database_path), repo


# ---------------------------------------------------------------- minting and persistence


def test_a_new_registration_gets_a_nonempty_opaque_identity(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)

    registration_id = _registration(db, "alpha")

    assert registration_id
    assert registration_id.startswith("repo-reg-")
    assert registration_id != db.get_project("alpha").id
    # Not derived from the project name and not derived from the repository path: neither of those
    # is identity, because one survives a rebinding and the other dies in an ordinary relocation.
    assert "alpha" not in registration_id
    assert registration_id != stable_id("project", "alpha")
    assert registration_id != stable_id("project", str(repo))
    assert repo.name not in registration_id
    minted = registration_id.removeprefix("repo-reg-")
    assert len(minted) == 32 and set(minted) <= set("0123456789abcdef")


def test_two_registrations_never_share_an_identity(tmp_path: Path, capsys):
    config = _configured(tmp_path)
    db = ForgeDB(config.database_path)
    assert _add(config, "alpha", _repository(tmp_path / "a")) == 0
    assert _add(config, "beta", _repository(tmp_path / "b")) == 0
    capsys.readouterr()

    assert _registration(db, "alpha") != _registration(db, "beta")


def test_repeated_reads_and_a_later_process_see_one_unchanged_identity(tmp_path: Path, capsys):
    config, db, _ = _registered(tmp_path, capsys)
    minted = _registration(db, "alpha")

    assert _registration(db, "alpha") == minted
    assert db.list_projects()[0].repository_registration_id == minted
    # A later process constructs its own controller and initializes the database on startup; that
    # startup path must never rotate an identity that already exists.
    for _ in range(3):
        reopened = ForgeDB(config.database_path)
        reopened.initialize()
        assert _registration(reopened, "alpha") == minted
    assert _list_json(config, capsys, "--schema-version", "1.2")["data"]["projects"][0][
        "repository_registration_id"
    ] == minted


def test_the_registration_identity_is_unique_in_the_database(tmp_path: Path, capsys):
    config, db, _ = _registered(tmp_path, capsys)
    assert _add(config, "beta", _repository(tmp_path / "b")) == 0
    capsys.readouterr()

    connection = sqlite3.connect(config.database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE projects SET repository_registration_id=? WHERE name='beta'",
                (_registration(db, "alpha"),),
            )
    finally:
        connection.close()


# ---------------------------------------------------------------- project add is create-only


def test_project_add_cannot_silently_replace_the_repository_path(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)
    before = db.get_project("alpha")
    other = _repository(tmp_path / "elsewhere")

    assert _add(config, "alpha", other) == 2

    assert "PROJECT_EXISTS" in capsys.readouterr().err
    after = db.get_project("alpha")
    assert after.repo_path == before.repo_path == repo.resolve()
    assert after.repository_registration_id == before.repository_registration_id
    assert after.updated_at == before.updated_at


def test_project_add_cannot_silently_replace_the_default_branch(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)
    assert db.get_project("alpha").default_branch == "main"

    assert _add(config, "alpha", repo, "--default-branch", "release") == 2

    assert "PROJECT_EXISTS" in capsys.readouterr().err
    # `default_branch` is execution-significant; an incidental registration never changes it, and
    # this slice has no explicit branch-change operation, so a mismatch is refused rather than
    # applied.
    assert db.get_project("alpha").default_branch == "main"


def test_project_add_cannot_silently_replace_validation_or_policies(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)
    before = db.get_project("alpha")

    assert _add(config, "alpha", repo, "--validation", "python -m pytest", "--push") == 2

    capsys.readouterr()
    after = db.get_project("alpha")
    assert after.validation_commands == before.validation_commands == []
    assert (after.auto_fetch, after.auto_commit, after.auto_push) == (
        before.auto_fetch,
        before.auto_commit,
        before.auto_push,
    )


def test_one_repository_path_is_one_registration(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)

    assert _add(config, "beta", repo) == 2

    assert "REPOSITORY_ALREADY_REGISTERED" in capsys.readouterr().err
    assert [project.name for project in db.list_projects()] == ["alpha"]


def test_a_refused_registration_never_clones_a_repository(tmp_path: Path, capsys):
    """The refusal is decided before the clone, so a refused `project add` leaves nothing behind."""
    config, db, _ = _registered(tmp_path, capsys)

    assert _run(config, "project", "add", "alpha", "--url", "https://example.invalid/x.git") == 2

    assert "PROJECT_EXISTS" in capsys.readouterr().err
    assert not (config.repository_root / "alpha").exists()


# ---------------------------------------------------------------- explicit relocation


def test_relocation_preserves_the_registration_identity(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)
    before = db.get_project("alpha")
    moved = _repository(tmp_path / "moved")

    assert _run(config, "project", "relocate", "alpha", "--repo", str(moved)) == 0

    assert "Relocated project alpha" in capsys.readouterr().out
    after = db.get_project("alpha")
    assert after.repo_path == moved.resolve() != repo.resolve()
    assert after.repository_registration_id == before.repository_registration_id
    assert (after.id, after.name, after.default_branch) == (
        before.id,
        before.name,
        before.default_branch,
    )
    assert after.validation_commands == before.validation_commands
    assert after.created_at == before.created_at


def test_relocation_reports_the_preserved_identity_in_json(tmp_path: Path, capsys):
    config, db, _ = _registered(tmp_path, capsys)
    minted = _registration(db, "alpha")
    moved = _repository(tmp_path / "moved")

    exit_code = _run(
        config, "project", "relocate", "alpha", "--repo", str(moved), "--json",
        "--schema-version", "1.2",
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["command"] == "project.relocate"
    assert payload["schema_version"] == "1.2"
    assert payload["success"] is True
    assert payload["data"]["relocated"] is True
    assert payload["data"]["project"]["repository_registration_id"] == minted
    assert payload["data"]["project"]["repository_path"] == str(moved.resolve())


def test_relocating_to_the_registered_path_mutates_nothing(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)
    before = _projects_row(config, db.get_project("alpha").id)

    assert _run(config, "project", "relocate", "alpha", "--repo", str(repo), "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["relocated"] is False
    assert _projects_row(config, db.get_project("alpha").id) == before


def test_relocation_refuses_a_target_that_is_not_a_git_working_tree(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)
    plain = tmp_path / "not-a-repository"
    plain.mkdir()

    assert _run(config, "project", "relocate", "alpha", "--repo", str(plain), "--json") == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert payload["data"] is None
    assert db.get_project("alpha").repo_path == repo.resolve()


def test_relocation_refuses_a_path_another_registration_claims(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)
    beta_repo = _repository(tmp_path / "beta-repo")
    assert _add(config, "beta", beta_repo) == 0
    capsys.readouterr()

    assert _run(config, "project", "relocate", "alpha", "--repo", str(beta_repo), "--json") == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "REPOSITORY_ALREADY_REGISTERED"
    assert payload["error"]["details"]["registered_to"] == "beta"
    assert db.get_project("alpha").repo_path == repo.resolve()


@pytest.mark.parametrize(
    "state", [TaskState.IMPLEMENTING, TaskState.VALIDATING, TaskState.REVIEWING, TaskState.REPAIRING]
)
def test_relocation_is_refused_while_a_worker_owns_a_task(
    tmp_path: Path, capsys, state: TaskState
):
    config, db, repo = _registered(tmp_path, capsys)
    db.add_task(
        Task(
            id="TASK-live",
            project_id=db.get_project("alpha").id,
            title="Live",
            objective="run",
            acceptance=[],
            validation_commands=[],
            state=state,
        )
    )
    moved = _repository(tmp_path / "moved")

    assert _run(config, "project", "relocate", "alpha", "--repo", str(moved), "--json") == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "TASK_ACTIVE"
    assert payload["error"]["details"]["active_tasks"] == 1
    assert db.get_project("alpha").repo_path == repo.resolve()


def test_relocation_of_an_unregistered_project_is_refused(tmp_path: Path, capsys):
    config, _, _ = _registered(tmp_path, capsys)
    moved = _repository(tmp_path / "moved")

    assert _run(config, "project", "relocate", "ghost", "--repo", str(moved), "--json") == 2

    assert json.loads(capsys.readouterr().out)["error"]["code"] == "PROJECT_NOT_FOUND"


def test_relocation_refuses_an_ambiguous_identifier(tmp_path: Path, capsys):
    """One project's id naming a different project's name is never resolved by guessing."""
    config, db, _ = _registered(tmp_path, capsys)
    alpha = db.get_project("alpha")
    trap = _repository(tmp_path / "trap")
    assert _add(config, alpha.id, trap) == 0
    capsys.readouterr()
    moved = _repository(tmp_path / "moved")

    assert _run(config, "project", "relocate", alpha.id, "--repo", str(moved), "--json") == 2

    assert json.loads(capsys.readouterr().out)["error"]["code"] == "PROJECT_AMBIGUOUS"
    assert db.get_project(alpha.id).repo_path == alpha.repo_path


def test_relocation_keeps_every_task_bound_to_the_same_registration(tmp_path: Path, capsys):
    config, db, _ = _registered(tmp_path, capsys)
    project = db.get_project("alpha")
    db.add_task(
        Task(
            id="TASK-1",
            project_id=project.id,
            title="Bound",
            objective="stay bound",
            acceptance=[],
            validation_commands=[],
            state=TaskState.READY,
        )
    )
    moved = _repository(tmp_path / "moved")

    assert _run(config, "project", "relocate", "alpha", "--repo", str(moved)) == 0

    capsys.readouterr()
    assert db.get_task("TASK-1").project_id == project.id
    assert _registration(db, "alpha") == project.repository_registration_id


# ---------------------------------------------------------------- delete and re-register


def test_delete_then_recreate_rotates_the_registration_identity(tmp_path: Path, capsys):
    config, db, repo = _registered(tmp_path, capsys)
    before = db.get_project("alpha")

    assert _run(config, "project", "delete", "alpha") == 0
    assert _add(config, "alpha", repo) == 0
    capsys.readouterr()

    after = db.get_project("alpha")
    # The name-derived project id is reproduced exactly -- which is precisely why it cannot carry
    # repository identity. The registration identity rotates, because this is a new registration.
    assert after.id == before.id == stable_id("project", "alpha")
    assert after.repository_registration_id != before.repository_registration_id
    assert after.repository_registration_id


def test_delete_then_recreate_leaves_retained_evidence_identifying_its_own_task(
    tmp_path: Path, capsys
):
    config, db, repo = _registered(tmp_path, capsys)
    project = db.get_project("alpha")
    db.add_task(
        Task(
            id="TASK-1",
            project_id=project.id,
            title="Done",
            objective="finish",
            acceptance=[],
            validation_commands=[],
            state=TaskState.FAILED,
        )
    )
    artifact = config.evidence_root / "alpha" / "TASK-1" / "attempt-01" / "task-result.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"status":"failed"}', encoding="utf-8")
    before = evidence_list(config, "TASK-1", schema_version=None)

    assert _run(config, "task", "delete", "TASK-1") == 0
    assert _run(config, "project", "delete", "alpha") == 0
    assert _add(config, "alpha", repo) == 0
    capsys.readouterr()

    after = evidence_list(config, "TASK-1", schema_version=None)
    assert artifact.read_text(encoding="utf-8") == '{"status":"failed"}'
    assert after["artifacts"] == before["artifacts"]
    assert after["project"] == before["project"] == "alpha"


# ---------------------------------------------------------------- additive migration


def test_additive_migration_mints_one_stable_value_and_changes_nothing_else(
    tmp_path: Path, capsys
):
    config = _configured(tmp_path)
    db = ForgeDB(config.database_path)
    assert _add(config, "alpha", _repository(tmp_path / "a")) == 0
    assert _add(config, "beta", _repository(tmp_path / "b")) == 0
    capsys.readouterr()
    alpha, beta = db.get_project("alpha"), db.get_project("beta")
    db.add_task(
        Task(
            id="TASK-1",
            project_id=alpha.id,
            title="Existing",
            objective="predates the column",
            acceptance=[],
            validation_commands=[],
            state=TaskState.READY,
        )
    )
    task_before = db.get_task("TASK-1")

    # Reproduce the exact pre-column shape of the projects table, keeping every existing row and
    # every task relation, so the additive path is proven against a real historical database.
    _drop_registration_column(config)
    assert "repository_registration_id" not in _project_columns(config)
    rows_before = {name: _projects_row(config, name) for name in (alpha.id, beta.id)}

    ForgeDB(config.database_path).initialize()

    migrated = {name: _registration(db, name) for name in ("alpha", "beta")}
    assert all(migrated.values())
    assert migrated["alpha"] != migrated["beta"]
    for project_id, before in rows_before.items():
        after = _projects_row(config, project_id)
        assert {key: value for key, value in after.items() if key != "repository_registration_id"} == before
    # Running the migration again finds nothing to fill, so it cannot rotate an identity.
    for _ in range(2):
        ForgeDB(config.database_path).initialize()
        assert {name: _registration(db, name) for name in ("alpha", "beta")} == migrated
    assert db.get_task("TASK-1") == task_before
    assert db.get_task("TASK-1").project_id == alpha.id
    assert db.get_project("alpha").id == alpha.id
    assert db.get_project("alpha").repo_path == alpha.repo_path


def test_migration_restores_the_uniqueness_invariant(tmp_path: Path, capsys):
    config, db, _ = _registered(tmp_path, capsys)
    _drop_registration_column(config)

    ForgeDB(config.database_path).initialize()

    assert "idx_projects_repository_registration" in _indexes(config)


# ---------------------------------------------------------------- JSON contract


def test_the_identity_is_served_only_to_a_caller_that_asked_for_a_version_declaring_it(
    tmp_path: Path, capsys
):
    config, db, _ = _registered(tmp_path, capsys)
    minted = _registration(db, "alpha")

    for requested in ((), ("--schema-version", "1.0"), ("--schema-version", "1.1")):
        payload = _list_json(config, capsys, *requested)
        assert "repository_registration_id" not in payload["data"]["projects"][0]

    payload = _list_json(config, capsys, "--schema-version", "1.2")
    project = payload["data"]["projects"][0]

    assert payload["schema_version"] == "1.2"
    assert project["repository_registration_id"] == minted
    # 1.2 adds the field and changes nothing else about the project object.
    baseline = _list_json(config, capsys)["data"]["projects"][0]
    assert {
        key: value for key, value in project.items() if key != "repository_registration_id"
    } == baseline


def test_the_identity_reaches_a_consumer_without_reading_the_forge_database(
    tmp_path: Path, capsys
):
    """Shared Chat binds to `forge_project_id` plus `repository_registration_id`, from JSON alone."""
    config, db, _ = _registered(tmp_path, capsys)

    project = _list_json(config, capsys, "--schema-version", "1.2")["data"]["projects"][0]

    assert project["project_id"] == db.get_project("alpha").id
    assert project["repository_registration_id"] == _registration(db, "alpha")
    assert isinstance(project["repository_registration_id"], str)


def test_1_2_keeps_the_1_1_token_usage_projection(tmp_path: Path, capsys):
    """The new version is additive: it does not fall back to the 1.0 projection of another field."""
    config, db, _ = _registered(tmp_path, capsys)
    db.add_task(
        Task(
            id="TASK-1",
            project_id=db.get_project("alpha").id,
            title="Usage",
            objective="usage",
            acceptance=[],
            validation_commands=[],
            state=TaskState.READY,
        )
    )

    def usage(*extra: str) -> set[str]:
        _run(config, "task", "list", "--json", *extra)
        return set(json.loads(capsys.readouterr().out)["data"]["tasks"][0]["token_usage"])

    legacy = usage()
    assert usage("--schema-version", "1.2") == usage("--schema-version", "1.1") != legacy


# ---------------------------------------------------------------- concurrent initialization


def test_two_concurrent_initializers_settle_one_identity_that_never_rotates(
    tmp_path: Path, capsys, monkeypatch
):
    """The first durable assignment wins permanently; the loser accepts it, it never overwrites it.

    Two Forge processes opening one legacy database both run the real initializer. Without an
    exactly-once backfill both select the same unset row, both mint, and the second write replaces
    an identity the first one had already made durable and a consumer may already hold.
    """
    config, db, _ = _registered(tmp_path, capsys)
    _empty_every_registration(config)
    _forbid_rotation(config)

    _initialize_concurrently(config, monkeypatch)

    settled = _registration(db, "alpha")
    assert settled
    # Every reader, in this process and in a later one, observes exactly that value.
    assert _registration(ForgeDB(config.database_path), "alpha") == settled
    assert _list_json(config, capsys, "--schema-version", "1.2")["data"]["projects"][0][
        "repository_registration_id"
    ] == settled
    for _ in range(3):
        reopened = ForgeDB(config.database_path)
        reopened.initialize()
        assert _registration(reopened, "alpha") == settled
    assert "idx_projects_repository_registration" in _indexes(config)


def test_concurrent_initialization_of_a_multi_project_legacy_database(
    tmp_path: Path, capsys, monkeypatch
):
    """Uniqueness has to hold across the whole backfill, not just across one row."""
    config = _configured(tmp_path)
    names = ("alpha", "beta", "gamma")
    for name in names:
        assert _add(config, name, _repository(tmp_path / f"{name}-repo")) == 0
    capsys.readouterr()
    db = ForgeDB(config.database_path)
    _empty_every_registration(config)
    _forbid_rotation(config)

    _initialize_concurrently(config, monkeypatch, workers=3)

    settled = {name: _registration(db, name) for name in names}
    assert all(settled.values())
    assert len(set(settled.values())) == len(names)
    assert "idx_projects_repository_registration" in _indexes(config)
    for _ in range(2):
        reopened = ForgeDB(config.database_path)
        reopened.initialize()
        assert {name: _registration(reopened, name) for name in names} == settled


def test_concurrent_initialization_of_a_database_without_the_column(
    tmp_path: Path, capsys, monkeypatch
):
    """The oldest case: two initializers race to add the column as well as to fill it."""
    config = _configured(tmp_path)
    for name in ("alpha", "beta"):
        assert _add(config, name, _repository(tmp_path / f"{name}-repo")) == 0
    capsys.readouterr()
    db = ForgeDB(config.database_path)
    _drop_registration_column(config)

    _initialize_concurrently(config, monkeypatch)

    settled = {name: _registration(db, name) for name in ("alpha", "beta")}
    assert all(settled.values())
    assert len(set(settled.values())) == 2
    assert "idx_projects_repository_registration" in _indexes(config)
    reopened = ForgeDB(config.database_path)
    reopened.initialize()
    assert {name: _registration(reopened, name) for name in ("alpha", "beta")} == settled


# ---------------------------------------------------------------- interrupted migration


def test_a_half_backfilled_database_fills_only_what_is_unset(tmp_path: Path, capsys):
    """Interruption point B: some rows were backfilled before the process died."""
    config = _configured(tmp_path)
    for name in ("alpha", "beta"):
        assert _add(config, name, _repository(tmp_path / f"{name}-repo")) == 0
    capsys.readouterr()
    db = ForgeDB(config.database_path)
    keeps = _registration(db, "alpha")
    connection = _connection(config)
    try:
        connection.execute("DROP INDEX IF EXISTS idx_projects_repository_registration")
        connection.execute("UPDATE projects SET repository_registration_id='' WHERE name='beta'")
        connection.commit()
    finally:
        connection.close()
    _forbid_rotation(config)

    ForgeDB(config.database_path).initialize()

    assert _registration(db, "alpha") == keeps
    assert _registration(db, "beta")
    assert _registration(db, "beta") != keeps
    assert "idx_projects_repository_registration" in _indexes(config)


def test_a_fully_backfilled_database_without_the_index_only_gains_the_index(
    tmp_path: Path, capsys
):
    """Interruption point C: every row settled, the unique index not yet established."""
    config, db, _ = _registered(tmp_path, capsys)
    settled = _registration(db, "alpha")
    connection = _connection(config)
    try:
        connection.execute("DROP INDEX IF EXISTS idx_projects_repository_registration")
        connection.commit()
    finally:
        connection.close()
    _forbid_rotation(config)
    assert "idx_projects_repository_registration" not in _indexes(config)

    ForgeDB(config.database_path).initialize()

    assert _registration(db, "alpha") == settled
    assert "idx_projects_repository_registration" in _indexes(config)


def test_a_completed_migration_is_a_no_op_however_often_it_is_repeated(tmp_path: Path, capsys):
    """Interruption point D: the index already exists and every row is settled."""
    config, db, _ = _registered(tmp_path, capsys)
    settled = _registration(db, "alpha")
    _forbid_rotation(config)
    before = _projects_row(config, db.get_project("alpha").id)

    for _ in range(5):
        ForgeDB(config.database_path).initialize()

    assert _projects_row(config, db.get_project("alpha").id) == before
    assert _registration(db, "alpha") == settled
    assert "idx_projects_repository_registration" in _indexes(config)


# ---------------------------------------------------------------- admission against a rotated generation


def test_admission_refuses_a_registration_rotated_before_the_locked_guard(
    tmp_path: Path, capsys, monkeypatch
):
    """A delete-and-recreate that lands between project resolution and the locked insert.

    `submit_task` resolves the project on an unlocked read and only later takes the write lock in
    `ForgeDB.add_task_guarded`, where `_locked_project_guard` re-reads the project row. A task-less
    project can be deleted and re-registered under the same name inside that window: the
    name-derived project id is reproduced exactly, so the guard's lookup still succeeds, while the
    repository registration identity has rotated. The task admitted against the R1 snapshot must
    not be committed under the row that now carries R2.
    """
    config, db, repo = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    real_add_task_guarded = ForgeDB.add_task_guarded
    rotation: dict[str, str] = {}

    def rotate_then_admit(self: ForgeDB, task: Task, guard) -> None:
        # Runs after `_resolve_project` captured the R1 snapshot and before this call takes
        # `BEGIN IMMEDIATE`, so each command below commits on its own connection unblocked.
        assert _run(config, "project", "delete", "alpha") == 0
        assert _add(config, "alpha", repo) == 0
        capsys.readouterr()
        rotation["registration"] = _registration(db, "alpha")
        return real_add_task_guarded(self, task, guard)

    monkeypatch.setattr(ForgeDB, "add_task_guarded", rotate_then_admit)

    title = "Admit against one generation"
    objective = "Prove the registration the insert commits under is the one that was resolved."
    exit_code = _run(
        config,
        "task",
        "add",
        "--project",
        "alpha",
        "--title",
        title,
        "--objective",
        objective,
        "--json",
    )
    document = json.loads(capsys.readouterr().out)

    # The race really happened: same project id, different registration.
    live = db.get_project("alpha")
    assert live is not None
    assert live.id == snapshot.id == stable_id("project", "alpha")
    assert rotation["registration"] == live.repository_registration_id
    assert live.repository_registration_id != snapshot.repository_registration_id

    connection = _connection(config)
    try:
        committed = connection.execute("SELECT id, project_id FROM tasks").fetchall()
    finally:
        connection.close()

    assert committed == [], (
        "admission committed "
        f"{[task_id for task_id, _ in committed]} under project row {live.id!r} whose registration "
        f"is now {live.repository_registration_id!r}, although the request was resolved against "
        f"{snapshot.repository_registration_id!r} (exit {exit_code}, success={document['success']})"
    )


def test_the_rotated_generation_refusal_is_named_and_precedes_any_insert(
    tmp_path: Path, capsys, monkeypatch
):
    """The same race, pinned to its stable code and to the order refusal-before-insert."""
    config, db, repo = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    real_add_task_guarded = ForgeDB.add_task_guarded
    inserts: list[str] = []

    def rotate_then_admit(self: ForgeDB, task: Task, guard) -> None:
        assert _run(config, "project", "delete", "alpha") == 0
        assert _add(config, "alpha", repo) == 0
        capsys.readouterr()
        return real_add_task_guarded(self, task, guard)

    def record_insert(connection, task: Task) -> None:
        inserts.append(task.id)

    monkeypatch.setattr(ForgeDB, "add_task_guarded", rotate_then_admit)
    monkeypatch.setattr(ForgeDB, "_insert_task", staticmethod(record_insert))

    exit_code = _run(
        config, "task", "add", "--project", "alpha", "--title", "Named refusal",
        "--objective", "The refusal names the rotated generation.", "--json",
    )
    document = json.loads(capsys.readouterr().out)
    live = db.get_project("alpha")
    assert live is not None

    assert exit_code == 2
    assert document["success"] is False
    assert document["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    details = document["error"]["details"]
    assert details["project_id"] == snapshot.id
    assert details["expected_repository_registration_id"] == snapshot.repository_registration_id
    assert details["current_repository_registration_id"] == live.repository_registration_id
    # The guard refused inside the locked transaction before the insert statement ever ran.
    assert inserts == []
    assert db.list_tasks() == []


def test_admission_survives_a_relocation_inside_the_locked_window(
    tmp_path: Path, capsys, monkeypatch
):
    """Relocation preserves the registration identity, so it is not a generation mismatch.

    The same window as the rotation test, but the project is legitimately moved instead of
    deleted and re-registered: same project id, same registration id, different `repo_path`.
    The locked guard compares identity, never the relocatable path, so admission continues.
    """
    config, db, _ = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    moved = _repository(tmp_path / "moved")
    real_add_task_guarded = ForgeDB.add_task_guarded

    def relocate_then_admit(self: ForgeDB, task: Task, guard) -> None:
        assert _run(config, "project", "relocate", "alpha", "--repo", str(moved)) == 0
        capsys.readouterr()
        return real_add_task_guarded(self, task, guard)

    monkeypatch.setattr(ForgeDB, "add_task_guarded", relocate_then_admit)

    exit_code = _run(
        config, "task", "add", "--project", "alpha", "--title", "Admit across a move",
        "--objective", "A relocation is not a new generation.", "--json",
    )
    document = json.loads(capsys.readouterr().out)
    live = db.get_project("alpha")
    assert live is not None

    # The relocation really happened inside the window, and identity was preserved.
    assert live.repo_path == moved
    assert live.repo_path != snapshot.repo_path
    assert live.id == snapshot.id
    assert live.repository_registration_id == snapshot.repository_registration_id

    assert exit_code == 0, document
    assert document["success"] is True
    tasks = db.list_tasks()
    assert [task.project_id for task in tasks] == [snapshot.id]
    assert tasks[0].id == document["data"]["task"]["task_id"]


def test_admission_against_an_unchanged_registration_still_succeeds(tmp_path: Path, capsys):
    """The ordinary path: the R1 snapshot meets the R1 locked row and the task is inserted."""
    config, db, _ = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None

    exit_code = _run(
        config, "task", "add", "--project", "alpha", "--title", "Ordinary admission",
        "--objective", "Nothing changed under the request.", "--json",
    )
    document = json.loads(capsys.readouterr().out)

    assert exit_code == 0, document
    assert document["success"] is True
    assert _registration(db, "alpha") == snapshot.repository_registration_id
    assert [task.project_id for task in db.list_tasks()] == [snapshot.id]


def test_a_project_that_owns_tasks_refuses_deletion(tmp_path: Path, capsys):
    """The supporting invariant: a registration with tasks cannot be deleted while they exist.

    This protects an existing task's generation from project deletion. It is not, by itself, what
    makes adopting an existing task safe: tasks can be deleted, so a project observed non-empty on
    an unlocked read may be taskless, deleted and re-registered before adoption. Adoption proves
    the generation under the guard (`read_task_guarded`); this invariant only narrows the window.
    """
    config, db, _ = _registered(tmp_path, capsys)
    before = db.get_project("alpha")
    assert before is not None
    assert _run(
        config, "task", "add", "--project", "alpha", "--title", "Anchor",
        "--objective", "Pin the registration in place.",
    ) == 0
    capsys.readouterr()

    assert _run(config, "project", "delete", "alpha", "--json") == 2
    document = json.loads(capsys.readouterr().out)

    assert document["success"] is False
    assert document["error"]["code"] == "PROJECT_NOT_EMPTY"
    assert document["error"]["details"]["task_count"] == 1
    after = db.get_project("alpha")
    assert after is not None
    assert after.repository_registration_id == before.repository_registration_id
    assert [task.project_id for task in db.list_tasks()] == [before.id]


# ---------------------------------------------------------- the caller's expected generation (1.4)
#
# `expected_repository_registration_id` is a caller's statement of which repository generation its
# task is bound to. It is a precondition on the project, never task material, and once supplied it
# is never discarded. Contract 1.4 requires it; older versions still enforce it when it is given.

OLDER_VERSIONS = ("1.0", "1.1", "1.2", "1.3")
NEVER_MINTED = "repo-reg-00000000000000000000000000000000"


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _tasks(config: ForgeConfig) -> list[tuple[str, str]]:
    connection = _connection(config)
    try:
        return connection.execute("SELECT id, project_id FROM tasks ORDER BY id").fetchall()
    finally:
        connection.close()


def _submit(
    config: ForgeConfig,
    capsys,
    *extra: str,
    title: str = "Bound task",
    objective: str = "Admit only against the expected generation.",
    schema_version: str | None = None,
) -> tuple[int, dict]:
    arguments = ["task", "add", "--project", "alpha", "--title", title, "--objective", objective]
    if schema_version is not None:
        arguments += ["--schema-version", schema_version]
    exit_code = _run(config, *arguments, "--json", *extra)
    return exit_code, json.loads(capsys.readouterr().out)


def _rotate(config: ForgeConfig, db: ForgeDB, repo: Path, capsys) -> tuple[str, str]:
    """Delete and re-register `alpha`: same name, same project id, a new registration."""
    before = _registration(db, "alpha")
    assert _run(config, "project", "delete", "alpha") == 0
    assert _add(config, "alpha", repo) == 0
    capsys.readouterr()
    after = _registration(db, "alpha")
    assert after != before
    return before, after


def _request(*extra: str) -> task_submission.TaskRequest:
    return build_task_request(
        build_parser().parse_args(
            [
                "task", "add", "--project", "alpha", "--title", "Bound task",
                "--objective", "Admit only against the expected generation.", *extra,
            ]
        )
    )


def test_contract_1_4_is_advertised_and_served(tmp_path: Path, capsys):
    """1.4 is a supported, requestable version, and it is the one that first requires the field."""
    assert SUPPORTED_SCHEMA_VERSIONS == ("1.0", "1.1", "1.2", "1.3", "1.4")
    assert LATEST_SCHEMA_VERSION == "1.4"
    assert EXPECTED_REPOSITORY_REGISTRATION_SINCE == "1.4"
    config, db, _ = _registered(tmp_path, capsys)

    assert _run(config, "status", "--schema-version", "1.4", "--json") == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == "1.4"

    assert _run(config, "status", "--schema-version", "9.9", "--json") == 2
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["error"]["code"] == "UNSUPPORTED_SCHEMA"
    assert refusal["error"]["details"]["supported"] == list(SUPPORTED_SCHEMA_VERSIONS)

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", _registration(db, "alpha"),
        schema_version="1.4",
    )
    assert exit_code == 0, document
    assert document["schema_version"] == "1.4"
    assert document["command"] == "task.add"


def test_a_1_4_submission_without_an_expected_registration_is_refused(tmp_path: Path, capsys):
    """Omission at 1.4 is a named contract refusal, decided before any database is opened."""
    config = _configured(tmp_path)
    assert not config.database_path.exists()

    exit_code, document = _submit(config, capsys, schema_version="1.4")

    assert exit_code == 2
    assert document["success"] is False
    assert document["schema_version"] == "1.4"
    assert document["error"]["code"] == "EXPECTED_REGISTRATION_REQUIRED"
    assert document["error"]["details"] == {
        "field": "expected_repository_registration_id",
        "schema_version": "1.4",
        "required_since": "1.4",
    }
    # A pure contract refusal: it never reached the database, which still does not exist.
    assert not config.database_path.exists()
    with pytest.raises(TaskSubmissionError) as excinfo:
        submit_task(config, _request(), dry_run=False, schema_version="1.4")
    assert excinfo.value.code == "EXPECTED_REGISTRATION_REQUIRED"


@pytest.mark.parametrize("schema_version", (None, *OLDER_VERSIONS))
def test_older_versions_still_admit_without_an_expected_registration(
    tmp_path: Path, capsys, schema_version: str | None
):
    """An installed caller on 1.0-1.3 that never heard of the field keeps working unchanged."""
    config, db, _ = _registered(tmp_path, capsys)

    exit_code, document = _submit(config, capsys, schema_version=schema_version)

    assert exit_code == 0, document
    assert document["success"] is True
    assert document["schema_version"] == (schema_version or "1.0")
    assert document["data"]["created"] is True
    assert "expected_repository_registration_id" not in document["data"]["normalized_request"]
    assert "expected_repository_registration_id" not in document["data"]["task"]
    assert [project_id for _, project_id in _tasks(config)] == [db.get_project("alpha").id]


def test_the_expected_generation_admits_when_it_is_the_live_one(tmp_path: Path, capsys):
    """Expected R1, project R1: admitted, and the 1.4 normalized request echoes the expectation."""
    config, db, _ = _registered(tmp_path, capsys)
    live = _registration(db, "alpha")

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", live, schema_version="1.4"
    )

    assert exit_code == 0, document
    assert document["data"]["created"] is True
    normalized = document["data"]["normalized_request"]
    assert normalized["expected_repository_registration_id"] == live
    assert normalized["admission_base_commit"] is None
    assert normalized["project_id"] == db.get_project("alpha").id
    # It qualifies the project the task was admitted into; it is not a property of the task.
    assert "expected_repository_registration_id" not in document["data"]["task"]
    assert _tasks(config) == [(document["data"]["task"]["task_id"], normalized["project_id"])]


def test_a_stale_expected_generation_is_refused_before_any_admission_work(
    tmp_path: Path, capsys, monkeypatch
):
    """Expected R1, project now R2: refused on the resolved snapshot, before Git, lock or insert."""
    config, db, repo = _registered(tmp_path, capsys)
    stale, live = _rotate(config, db, repo, capsys)
    project_id = db.get_project("alpha").id

    def never(*_args, **_kwargs):
        raise AssertionError("admission work ran after the caller's expectation failed")

    monkeypatch.setattr(task_submission, "resolve_base_commit", never)
    monkeypatch.setattr(task_submission, "derive_task_id", never)
    monkeypatch.setattr(ForgeDB, "add_task_guarded", never)

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", stale, "--base-commit", _head(repo),
        schema_version="1.4",
    )

    assert exit_code == 2
    assert document["success"] is False
    assert document["data"] is None
    assert document["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert document["error"]["details"] == {
        "project_id": project_id,
        "expected_repository_registration_id": stale,
        "current_repository_registration_id": live,
    }
    # Opaque identities only: no repository or workspace path is disclosed.
    assert str(repo) not in json.dumps(document)
    assert str(tmp_path) not in json.dumps(document)
    assert _tasks(config) == []
    assert _registration(db, "alpha") == live


def test_a_dry_run_verifies_the_expected_generation_honestly(tmp_path: Path, capsys):
    """Dry run refuses a stale expectation, requires one at 1.4, and never writes either way."""
    config, db, repo = _registered(tmp_path, capsys)
    stale, live = _rotate(config, db, repo, capsys)

    exit_code, document = _submit(
        config, capsys, "--dry-run", "--expected-repository-registration", stale,
        schema_version="1.4",
    )
    assert exit_code == 2
    assert document["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert document["error"]["details"]["expected_repository_registration_id"] == stale
    assert document["error"]["details"]["current_repository_registration_id"] == live

    exit_code, document = _submit(config, capsys, "--dry-run", schema_version="1.4")
    assert exit_code == 2
    assert document["error"]["code"] == "EXPECTED_REGISTRATION_REQUIRED"

    exit_code, document = _submit(
        config, capsys, "--dry-run", "--expected-repository-registration", live,
        schema_version="1.4",
    )
    assert exit_code == 0, document
    assert document["data"]["dry_run"] is True
    assert document["data"]["would_create"] is True
    assert document["data"]["created"] is False
    assert document["data"]["normalized_request"]["expected_repository_registration_id"] == live
    assert _tasks(config) == []


def test_the_human_path_honors_the_flag_and_the_required_law(tmp_path: Path, capsys):
    """An option argparse accepts is checked on every entry path, never silently dropped."""
    config, db, repo = _registered(tmp_path, capsys)
    stale, live = _rotate(config, db, repo, capsys)
    human = ("task", "add", "--project", "alpha", "--title", "Human", "--objective", "By hand.")

    assert _run(config, *human, "--expected-repository-registration", stale) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "REPOSITORY_REGISTRATION_MISMATCH" in captured.err
    assert str(repo) not in captured.err
    assert _tasks(config) == []

    assert _run(config, *human, "--schema-version", "1.4") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "EXPECTED_REGISTRATION_REQUIRED" in captured.err
    assert _tasks(config) == []

    assert _run(config, *human, "--expected-repository-registration", live) == 0
    assert capsys.readouterr().out.startswith("Added ")
    assert [project_id for _, project_id in _tasks(config)] == [db.get_project("alpha").id]

    # The same admission again, declared at 1.4 by hand, is the idempotent repeat.
    assert (
        _run(config, *human, "--schema-version", "1.4", "--expected-repository-registration", live)
        == 0
    )
    assert capsys.readouterr().out.startswith("Already present:")
    assert len(_tasks(config)) == 1


def test_an_empty_expectation_is_refused_rather_than_treated_as_absent(tmp_path: Path, capsys):
    config, _db, _ = _registered(tmp_path, capsys)
    arguments = ("task", "add", "--project", "alpha", "--title", "Empty", "--objective", "obj")

    assert _run(config, *arguments, "--expected-repository-registration", "  ", "--json") == 2
    document = json.loads(capsys.readouterr().out)
    assert document["error"]["code"] == "INVALID_TASK_REQUEST"
    assert document["error"]["details"] == {"field": "expected_repository_registration_id"}

    assert _run(config, *arguments, "--expected-repository-registration", "") == 2
    assert "INVALID_TASK_REQUEST" in capsys.readouterr().err
    assert _tasks(config) == []


@pytest.mark.parametrize("schema_version", (None, *OLDER_VERSIONS))
def test_an_expectation_supplied_at_an_older_version_is_still_enforced(
    tmp_path: Path, capsys, schema_version: str | None
):
    """A supplied safety expectation is never ignored, whatever version the caller declared."""
    config, db, repo = _registered(tmp_path, capsys)
    stale, live = _rotate(config, db, repo, capsys)

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", stale, schema_version=schema_version
    )
    assert exit_code == 2
    assert document["schema_version"] == (schema_version or "1.0")
    assert document["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert _tasks(config) == []

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", live, schema_version=schema_version
    )
    assert exit_code == 0, document
    assert document["data"]["created"] is True
    # Enforced, but the older version's shape is preserved: the field is not echoed there.
    assert "expected_repository_registration_id" not in document["data"]["normalized_request"]


@pytest.mark.parametrize("schema_version", OLDER_VERSIONS)
def test_older_normalized_request_shapes_are_pinned(tmp_path: Path, capsys, schema_version: str):
    """The 1.0-1.3 normalized request has exactly the keys it had, with or without the option."""
    config, db, _ = _registered(tmp_path, capsys)
    live = _registration(db, "alpha")
    base_keys = [
        "task_id", "task_id_source", "project", "project_id", "title", "objective",
        "acceptance_criteria", "architecture_decisions", "constraints", "scope", "out_of_scope",
        "validation_commands", "tags", "dependencies", "risk", "priority", "max_attempts",
    ]
    expected_keys = base_keys + (["admission_base_commit"] if schema_version == "1.3" else [])

    _, plain = _submit(config, capsys, title="Plain", schema_version=schema_version)
    _, bound = _submit(
        config, capsys, "--expected-repository-registration", live, title="Bound",
        schema_version=schema_version,
    )

    assert list(plain["data"]["normalized_request"]) == expected_keys
    assert list(bound["data"]["normalized_request"]) == expected_keys

    _, latest = _submit(
        config, capsys, "--expected-repository-registration", live, title="Latest",
        schema_version="1.4",
    )
    assert list(latest["data"]["normalized_request"]) == base_keys + [
        "admission_base_commit", "expected_repository_registration_id",
    ]


def test_a_generation_rotated_after_the_caller_check_is_still_refused(
    tmp_path: Path, capsys, monkeypatch
):
    """The cross-race composition: expected R1, resolved R1, rotated to R2 before the lock.

    The caller's check passes on the R1 snapshot. The locked self-consistency guard then observes
    R2 under `BEGIN IMMEDIATE`, refuses, and rolls back: no task exists under either generation.
    """
    config, db, repo = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    real_add_task_guarded = ForgeDB.add_task_guarded
    real_verify = task_submission.verify_expected_registration
    rotation: dict[str, str] = {}
    checks: list[str] = []

    def record_check(project, expected):
        checks.append(project.repository_registration_id)
        return real_verify(project, expected)

    def rotate_then_admit(self: ForgeDB, task: Task, guard) -> None:
        assert checks == [snapshot.repository_registration_id], "the caller check ran first"
        rotation["before"], rotation["after"] = _rotate(config, db, repo, capsys)
        return real_add_task_guarded(self, task, guard)

    monkeypatch.setattr(task_submission, "verify_expected_registration", record_check)
    monkeypatch.setattr(ForgeDB, "add_task_guarded", rotate_then_admit)

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", snapshot.repository_registration_id,
        schema_version="1.4",
    )

    assert rotation["before"] == snapshot.repository_registration_id
    assert exit_code == 2
    assert document["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    details = document["error"]["details"]
    assert details["project_id"] == snapshot.id
    assert details["expected_repository_registration_id"] == snapshot.repository_registration_id
    assert details["current_repository_registration_id"] == rotation["after"]
    assert _tasks(config) == []
    assert _registration(db, "alpha") == rotation["after"]


def test_relocation_preserves_the_expected_generation(tmp_path: Path, capsys):
    """Same registration, new path: the expectation still matches; the path is never compared."""
    config, db, _ = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    moved = _repository(tmp_path / "moved")
    assert _run(config, "project", "relocate", "alpha", "--repo", str(moved)) == 0
    capsys.readouterr()
    live = db.get_project("alpha")
    assert live is not None
    assert live.repo_path == moved != snapshot.repo_path
    assert live.repository_registration_id == snapshot.repository_registration_id

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", snapshot.repository_registration_id,
        schema_version="1.4",
    )

    assert exit_code == 0, document
    assert document["data"]["created"] is True
    assert _tasks(config) == [(document["data"]["task"]["task_id"], snapshot.id)]


def test_the_expectation_is_not_task_identity_or_fingerprint_material(tmp_path: Path, capsys):
    config, db, _ = _registered(tmp_path, capsys)
    project = db.get_project("alpha")
    assert project is not None
    plain = _request()
    bound = _request("--expected-repository-registration", project.repository_registration_id)
    other = replace(plain, expected_repository_registration_id=NEVER_MINTED)

    assert plain.expected_repository_registration_id is None
    assert bound.expected_repository_registration_id == project.repository_registration_id
    assert plain.defaults_applied == bound.defaults_applied
    assert "expected_repository_registration_id" not in bound.defaults_applied
    assert (
        derive_task_id(plain, project)
        == derive_task_id(bound, project)
        == derive_task_id(other, project)
    )
    assert (
        request_fingerprint(plain, project, None)
        == request_fingerprint(bound, project, None)
        == request_fingerprint(other, project, None)
    )
    # The durable row is the same row a plain submission would have produced.
    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", project.repository_registration_id,
        schema_version="1.4",
    )
    assert exit_code == 0, document
    assert document["data"]["task"]["task_id"] == derive_task_id(plain, project)
    assert document["data"]["normalized_request"]["task_id_source"] == "derived"


def test_a_retry_with_the_expectation_converges_on_the_same_task(tmp_path: Path, capsys):
    config, db, _ = _registered(tmp_path, capsys)
    live = _registration(db, "alpha")

    first_code, first = _submit(
        config, capsys, "--expected-repository-registration", live, schema_version="1.4"
    )
    second_code, second = _submit(
        config, capsys, "--expected-repository-registration", live, schema_version="1.4"
    )

    assert (first_code, second_code) == (0, 0), (first, second)
    assert first["data"]["created"] is True
    assert second["data"]["created"] is False
    assert second["data"]["would_create"] is False
    assert second["data"]["task"]["task_id"] == first["data"]["task"]["task_id"]
    assert len(_tasks(config)) == 1


def test_a_legacy_task_reconciles_with_a_later_correct_expectation(tmp_path: Path, capsys):
    """A task admitted at <=1.3 without an expectation is the same task when retried at 1.4."""
    config, db, _ = _registered(tmp_path, capsys)
    live = _registration(db, "alpha")
    project_id = db.get_project("alpha").id

    legacy_code, legacy = _submit(config, capsys, "--id", "SC-LEGACY-1")
    assert legacy_code == 0, legacy
    assert legacy["data"]["created"] is True
    assert legacy["schema_version"] == "1.0"

    retry_code, retry = _submit(
        config, capsys, "--id", "SC-LEGACY-1", "--expected-repository-registration", live,
        schema_version="1.4",
    )
    assert retry_code == 0, retry
    assert retry["data"]["created"] is False
    assert retry["data"]["task"]["task_id"] == "SC-LEGACY-1"
    assert retry["data"]["normalized_request"]["expected_repository_registration_id"] == live
    assert _tasks(config) == [("SC-LEGACY-1", project_id)]

    # A wrong expectation is a generation mismatch, never a task conflict and never a rotation.
    wrong_code, wrong = _submit(
        config, capsys, "--id", "SC-LEGACY-1", "--expected-repository-registration", NEVER_MINTED,
        schema_version="1.4",
    )
    assert wrong_code == 2
    assert wrong["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert _tasks(config) == [("SC-LEGACY-1", project_id)]
    assert db.get_task("SC-LEGACY-1").title == "Bound task"
    # The registration cannot have rotated underneath the task: the project owns it.
    assert _run(config, "project", "delete", "alpha") == 2
    capsys.readouterr()
    assert _registration(db, "alpha") == live


def test_the_inbox_honors_a_supplied_expectation_and_keeps_legacy_omission(
    tmp_path: Path, capsys
):
    """The inbox constructs its task directly; it applies the same law when a file binds itself."""
    config, db, _ = _registered(tmp_path, capsys)
    project = db.get_project("alpha")
    assert project is not None
    orchestrator = object.__new__(Orchestrator)
    orchestrator.db = db
    orchestrator.config = config

    def payload(task_id: str, expected: str | None) -> dict:
        data = {"project": "alpha", "title": "Inbox", "objective": "obj", "id": task_id}
        if expected is not None:
            data["expected_repository_registration_id"] = expected
        return data

    orchestrator._task_from_yaml(payload("INBOX-LEGACY", None))
    assert db.get_task("INBOX-LEGACY") is not None

    with pytest.raises(TaskSubmissionError) as excinfo:
        orchestrator._task_from_yaml(payload("INBOX-STALE", NEVER_MINTED))
    assert excinfo.value.code == "REPOSITORY_REGISTRATION_MISMATCH"
    assert excinfo.value.details["project_id"] == project.id
    assert excinfo.value.details["expected_repository_registration_id"] == NEVER_MINTED
    assert db.get_task("INBOX-STALE") is None

    orchestrator._task_from_yaml(payload("INBOX-BOUND", project.repository_registration_id))
    assert db.get_task("INBOX-BOUND") is not None
    assert sorted(task_id for task_id, _ in _tasks(config)) == ["INBOX-BOUND", "INBOX-LEGACY"]


# ------------------------------------------------------------- reconciliation is admission
#
# `admit_task` succeeds two ways: it inserts a new row, or it adopts the durable task that already
# holds the deterministic id. Both are admissions, so both must prove that the generation the
# caller resolved is the generation currently governing the task. The insert proves it under the
# write lock; these tests hold adoption to the same proof.


def _admit_race(config: ForgeConfig, repo: Path, capsys, monkeypatch, *before: "Callable"):
    """Run `before` once inside caller A's window: after A resolved the project and the caller
    expectation passed, before A reaches `admit_task`. Implementation-agnostic: the seam is the
    base-commit resolution `submit_task` always performs between those two points."""
    real_resolve = task_submission.resolve_base_commit
    fired: list[bool] = []

    def resolve_after_race(project, base_commit):
        if not fired:
            fired.append(True)
            for step in before:
                step()
            capsys.readouterr()
        return real_resolve(project, base_commit)

    monkeypatch.setattr(task_submission, "resolve_base_commit", resolve_after_race)
    return fired


def _b_submits(config: ForgeConfig, capsys, *extra: str) -> "Callable[[], None]":
    def submit() -> None:
        exit_code = _run(
            config, "task", "add", "--project", "alpha", "--title", "Bound task",
            "--objective", "Admit only against the expected generation.", "--json", *extra,
        )
        document = json.loads(capsys.readouterr().out)
        assert exit_code == 0, document
        assert document["data"]["created"] is True

    return submit


def test_reconciliation_refuses_a_deterministic_task_created_under_a_rotated_generation(
    tmp_path: Path, capsys, monkeypatch
):
    """The reconciliation race: A resolves P/R1; B rotates the taskless project to R2 and admits
    the identical deterministic task T under R2; A then finds T already present.

    T's contents are identical, so fingerprint reconciliation alone would adopt it and report a
    successful admission of a task governed by a generation A never resolved. Adoption must carry
    the same generation proof the insert does.
    """
    config, db, repo = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    rotation: dict[str, str] = {}

    def rotate() -> None:
        rotation["before"], rotation["after"] = _rotate(config, db, repo, capsys)

    fired = _admit_race(config, repo, capsys, monkeypatch, rotate, _b_submits(config, capsys))

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", snapshot.repository_registration_id,
        schema_version="1.4",
    )

    # The race really happened, in A's window: same project id, new generation, T under it.
    assert fired == [True]
    assert rotation["before"] == snapshot.repository_registration_id
    live = db.get_project("alpha")
    assert live is not None
    assert live.id == snapshot.id
    assert live.repository_registration_id == rotation["after"]
    winner = db.get_task(derive_task_id(_request(), snapshot))
    assert winner is not None and winner.project_id == live.id

    assert exit_code == 2, document
    assert document["success"] is False
    assert document["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert document["error"]["details"] == {
        "project_id": snapshot.id,
        "expected_repository_registration_id": snapshot.repository_registration_id,
        "current_repository_registration_id": rotation["after"],
    }
    assert str(repo) not in json.dumps(document)
    # B's valid task is untouched, and A created nothing.
    assert _tasks(config) == [(winner.id, live.id)]
    assert db.get_task(winner.id) == winner


def test_reconciliation_refuses_a_task_deleted_and_recreated_under_a_rotated_generation(
    tmp_path: Path, capsys, monkeypatch
):
    """Deletion variant: A resolves P/R1 while T already exists under it, so the project is not
    taskless. B deletes T, deletes P, re-registers it as R2 and admits T again. An earlier
    unlocked observation of T -- and the fact that a non-empty project cannot be deleted -- proves
    nothing about the generation at adoption time; only the guarded read does.
    """
    config, db, repo = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    _b_submits(config, capsys)()
    task_id = derive_task_id(_request(), snapshot)
    assert db.get_task(task_id) is not None
    rotation: dict[str, str] = {}

    def delete_task_then_rotate() -> None:
        assert _run(config, "task", "delete", task_id) == 0
        capsys.readouterr()
        rotation["before"], rotation["after"] = _rotate(config, db, repo, capsys)

    _admit_race(
        config, repo, capsys, monkeypatch, delete_task_then_rotate, _b_submits(config, capsys)
    )

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", snapshot.repository_registration_id,
        schema_version="1.4",
    )

    assert rotation["before"] == snapshot.repository_registration_id
    assert exit_code == 2, document
    assert document["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert document["error"]["details"]["expected_repository_registration_id"] == (
        snapshot.repository_registration_id
    )
    assert document["error"]["details"]["current_repository_registration_id"] == rotation["after"]
    recreated = db.get_task(task_id)
    assert recreated is not None
    assert _tasks(config) == [(task_id, snapshot.id)]
    assert _registration(db, "alpha") == rotation["after"]


def test_reconciliation_adopts_an_existing_task_under_the_same_generation(
    tmp_path: Path, capsys
):
    """P/R1 owns T; a caller resolving and expecting R1 adopts T: no rotation, no conflict."""
    config, db, _ = _registered(tmp_path, capsys)
    live = _registration(db, "alpha")
    _b_submits(config, capsys, "--schema-version", "1.4", "--expected-repository-registration", live)()
    before = _tasks(config)

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", live, schema_version="1.4"
    )

    assert exit_code == 0, document
    assert document["data"]["created"] is False
    assert document["data"]["would_create"] is False
    assert document["data"]["task"]["task_id"] == before[0][0]
    assert _tasks(config) == before


def test_reconciliation_cannot_adopt_across_a_rotation_inside_the_guard_window(
    tmp_path: Path, capsys, monkeypatch
):
    """The guarded read is the decision: a rotation that lands after every unlocked step and
    immediately before the lock is still refused, and the winner's task is left alone."""
    config, db, repo = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    _b_submits(config, capsys)()
    task_id = derive_task_id(_request(), snapshot)
    real_read = ForgeDB.read_task_guarded
    rotation: dict[str, str] = {}

    def rotate_then_read(self: ForgeDB, wanted: str, guard):
        if not rotation:
            assert _run(config, "task", "delete", task_id) == 0
            capsys.readouterr()
            rotation["before"], rotation["after"] = _rotate(config, db, repo, capsys)
            _b_submits(config, capsys)()
        return real_read(self, wanted, guard)

    monkeypatch.setattr(ForgeDB, "read_task_guarded", rotate_then_read)

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", snapshot.repository_registration_id,
        schema_version="1.4",
    )

    assert rotation["before"] == snapshot.repository_registration_id
    assert exit_code == 2, document
    assert document["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert document["error"]["details"]["current_repository_registration_id"] == rotation["after"]
    assert _tasks(config) == [(task_id, snapshot.id)]


# ------------------------------------------------------------- the handoff after the guarded proof
#
# The guarded transaction proves the generation of the exact row it inserts or adopts. The task the
# admission caller receives must be that row's snapshot. A live reread after the lock is released
# can observe a replacement admitted under a later generation -- the same name-derived project id,
# the same deterministic task id, a different task -- and report success describing it. Once the
# proven snapshot has been returned, a later deletion or rotation is an ordinary later event; the
# law is that the success response describes the task proven at admission, not that the row lives
# forever.

REPLACED_TAG = "recreated-under-r2"


def _replace_under_a_new_generation(
    config: ForgeConfig, db: ForgeDB, repo: Path, capsys, task_id: str
) -> tuple[str, str]:
    """B's steps, after A's proof: delete T, delete P/R1, register P again as R2, and admit the
    deterministic T again with different contract material, so the replacement is
    distinguishable from the row A proved by more than its id."""
    assert _run(config, "task", "delete", task_id) == 0
    capsys.readouterr()
    before, after = _rotate(config, db, repo, capsys)
    _b_submits(config, capsys, "--tag", REPLACED_TAG)()
    return before, after


def _assert_describes(payload: dict, proven: Task) -> None:
    """The response is the proven snapshot: every field the replacement could differ in agrees
    with the row read under the guard, and the replacement's material is absent."""
    assert payload["task_id"] == proven.id
    assert payload["project_id"] == proven.project_id
    assert payload["tags"] == list(proven.tags)
    assert payload["created_at"] == proven.created_at
    assert payload["updated_at"] == proven.updated_at
    assert (payload["title"], payload["objective"], payload["state"]) == (
        proven.title, proven.objective, proven.state.value
    )
    assert REPLACED_TAG not in json.dumps(payload)


def test_a_created_task_is_answered_with_the_guarded_snapshot_not_a_later_replacement(
    tmp_path: Path, capsys, monkeypatch
):
    """created:true. A's guarded insert proves P/R1 and commits T. After that proof and before A
    answers, B deletes T, rotates P to R2 and admits a different T under the same deterministic
    id. A's answer is the row it proved and committed under R1, never R2's replacement."""
    config, db, repo = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    task_id = derive_task_id(_request(), snapshot)
    real_add_task_guarded = ForgeDB.add_task_guarded
    guarded: dict = {}

    def replace_after_commit(self: ForgeDB, task: Task, guard):
        result = real_add_task_guarded(self, task, guard)
        if "committed" in guarded:
            return result
        # The row A's transaction committed, observed before anything else touches it; then the
        # window the old unlocked reread sat in.
        guarded["committed"] = db.get_task(task_id)
        guarded["result"] = result
        guarded["before"], guarded["after"] = _replace_under_a_new_generation(
            config, db, repo, capsys, task_id
        )
        return result

    monkeypatch.setattr(ForgeDB, "add_task_guarded", replace_after_commit)

    exit_code, document = _submit(
        config, capsys, "--tag", "bound-to-r1",
        "--expected-repository-registration", snapshot.repository_registration_id,
        schema_version="1.4",
    )

    # The race really happened after A's proof: R1 rotated to R2 and a different T sits there.
    assert guarded["before"] == snapshot.repository_registration_id
    assert _registration(db, "alpha") == guarded["after"]
    replacement = db.get_task(task_id)
    assert replacement is not None and replacement.tags == [REPLACED_TAG]
    committed = guarded["committed"]
    assert committed is not None and committed.tags == ["bound-to-r1"]

    assert exit_code == 0, document
    assert document["data"]["created"] is True
    assert document["data"]["would_create"] is False
    _assert_describes(document["data"]["task"], committed)
    # The snapshot came out of the guarded transaction itself, not from any later read.
    assert guarded["result"] == committed


def test_a_racing_winner_is_answered_with_the_guarded_snapshot_not_a_later_replacement(
    tmp_path: Path, capsys, monkeypatch
):
    """created:false. A observes no task; B admits the deterministic T under R1 first, so A's
    insert loses the primary key and adopts the winner under the guarded generation proof. After
    that proof and before A answers, B deletes T, rotates P to R2 and admits a different T. A's
    answer is the exact winner it proved under R1."""
    config, db, repo = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    task_id = derive_task_id(_request(), snapshot)
    real_add_task_guarded = ForgeDB.add_task_guarded
    real_read_task_guarded = ForgeDB.read_task_guarded
    guarded: dict = {}

    def winner_then_insert(self: ForgeDB, task: Task, guard):
        if "won" not in guarded:
            # B wins the id between A's guarded absence read and A's insert; A's own insert then
            # fails on the real primary key and reconciles the winner.
            guarded["won"] = True
            _b_submits(config, capsys, "--tag", "won-under-r1")()
        return real_add_task_guarded(self, task, guard)

    def replace_after_winner_proof(self: ForgeDB, wanted: str, guard):
        winner = real_read_task_guarded(self, wanted, guard)
        if winner is None or "winner" in guarded:
            return winner
        guarded["winner"] = winner
        guarded["before"], guarded["after"] = _replace_under_a_new_generation(
            config, db, repo, capsys, task_id
        )
        return winner

    monkeypatch.setattr(ForgeDB, "add_task_guarded", winner_then_insert)
    monkeypatch.setattr(ForgeDB, "read_task_guarded", replace_after_winner_proof)

    exit_code, document = _submit(
        config, capsys, "--tag", "won-under-r1",
        "--expected-repository-registration", snapshot.repository_registration_id,
        schema_version="1.4",
    )

    assert guarded["won"] is True
    assert guarded["before"] == snapshot.repository_registration_id
    assert _registration(db, "alpha") == guarded["after"]
    replacement = db.get_task(task_id)
    assert replacement is not None and replacement.tags == [REPLACED_TAG]
    winner = guarded["winner"]
    assert winner.tags == ["won-under-r1"]

    assert exit_code == 0, document
    assert document["data"]["created"] is False
    assert document["data"]["would_create"] is False
    _assert_describes(document["data"]["task"], winner)


def test_an_opaque_expectation_is_compared_never_parsed(tmp_path: Path, capsys):
    """Forge owns the identity's shape and never restates it: a hygienic value that does not look
    like `repo-reg-*` is a generation mismatch, not a format refusal."""
    config, db, _ = _registered(tmp_path, capsys)
    live = _registration(db, "alpha")
    assert live.startswith("repo-reg-")

    exit_code, document = _submit(
        config, capsys, "--expected-repository-registration", "opaque-generation-token",
        schema_version="1.4",
    )

    assert exit_code == 2
    assert document["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert document["error"]["details"] == {
        "project_id": db.get_project("alpha").id,
        "expected_repository_registration_id": "opaque-generation-token",
        "current_repository_registration_id": live,
    }
    assert _tasks(config) == []


def test_mismatch_details_use_one_key_set_on_both_proofs(tmp_path: Path, capsys, monkeypatch):
    """Pre-lock and locked refusals report exactly project_id, expected_repository_registration_id
    and current_repository_registration_id; no other key, and never a path."""
    config, db, repo = _registered(tmp_path, capsys)
    snapshot = db.get_project("alpha")
    assert snapshot is not None
    keys = {"project_id", "expected_repository_registration_id", "current_repository_registration_id"}

    stale, live = _rotate(config, db, repo, capsys)
    _, pre_lock = _submit(
        config, capsys, "--expected-repository-registration", stale, schema_version="1.4"
    )
    assert pre_lock["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert set(pre_lock["error"]["details"]) == keys
    assert pre_lock["error"]["details"]["expected_repository_registration_id"] == stale
    assert pre_lock["error"]["details"]["current_repository_registration_id"] == live

    resolved = db.get_project("alpha")
    assert resolved is not None
    real_add_task_guarded = ForgeDB.add_task_guarded
    rotation: dict[str, str] = {}

    def rotate_then_admit(self: ForgeDB, task: Task, guard) -> None:
        rotation["before"], rotation["after"] = _rotate(config, db, repo, capsys)
        return real_add_task_guarded(self, task, guard)

    monkeypatch.setattr(ForgeDB, "add_task_guarded", rotate_then_admit)
    _, locked = _submit(
        config, capsys, "--expected-repository-registration", live, schema_version="1.4"
    )
    assert locked["error"]["code"] == "REPOSITORY_REGISTRATION_MISMATCH"
    assert set(locked["error"]["details"]) == keys
    assert locked["error"]["details"]["expected_repository_registration_id"] == live
    assert locked["error"]["details"]["current_repository_registration_id"] == rotation["after"]
    for document in (pre_lock, locked):
        assert "resolved_repository_registration_id" not in json.dumps(document)
        assert str(repo) not in json.dumps(document)
    assert _tasks(config) == []
