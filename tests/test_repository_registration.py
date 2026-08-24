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
from pathlib import Path

import pytest

from codex_forge import db as db_module
from codex_forge.cli import main
from codex_forge.config import ForgeConfig, load_config
from codex_forge.db import ForgeDB
from codex_forge.evidence import evidence_list
from codex_forge.models import Task, TaskState
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
