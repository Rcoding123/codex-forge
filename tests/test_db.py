from pathlib import Path

from codex_forge.db import ForgeDB
from codex_forge.models import Project, Task, TaskState


def test_dependency_transitions_to_ready(tmp_path: Path):
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    project = Project(id="project-1", name="demo", repo_path=tmp_path / "repo")
    db.add_project(project)
    first = Task(
        id="TASK-1",
        project_id=project.id,
        title="First",
        objective="Complete first",
        acceptance=[],
        validation_commands=[],
        state=TaskState.READY,
    )
    second = Task(
        id="TASK-2",
        project_id=project.id,
        title="Second",
        objective="Complete second",
        acceptance=[],
        validation_commands=[],
        state=TaskState.BACKLOG,
        dependencies=["TASK-1"],
    )
    db.add_task(first)
    db.add_task(second)
    assert db.get_task("TASK-2").state == TaskState.BACKLOG
    db.update_task("TASK-1", state=TaskState.COMPLETED)
    db.refresh_ready_states()
    assert db.get_task("TASK-2").state == TaskState.READY


def test_claim_increments_attempts(tmp_path: Path):
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    project = Project(id="project-1", name="demo", repo_path=tmp_path / "repo")
    db.add_project(project)
    task = Task(
        id="TASK-1",
        project_id=project.id,
        title="Task",
        objective="Work",
        acceptance=[],
        validation_commands=[],
        state=TaskState.READY,
    )
    db.add_task(task)
    claimed = db.claim_ready_tasks(1)
    assert len(claimed) == 1
    assert claimed[0].attempts == 1
    assert claimed[0].state == TaskState.IMPLEMENTING


def test_recover_interrupted_tasks_preserves_worktree_and_honors_attempt_limit(tmp_path: Path):
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    project = Project(id="project-1", name="demo", repo_path=tmp_path / "repo")
    db.add_project(project)
    retryable = Task(
        id="TASK-retry",
        project_id=project.id,
        title="Retry",
        objective="Recover",
        acceptance=[],
        validation_commands=[],
        state=TaskState.VALIDATING,
        attempts=1,
        max_attempts=3,
        branch="forge/task-retry",
        worktree_path=tmp_path / "worktree-retry",
    )
    exhausted = Task(
        id="TASK-exhausted",
        project_id=project.id,
        title="Exhausted",
        objective="Fail safely",
        acceptance=[],
        validation_commands=[],
        state=TaskState.REPAIRING,
        attempts=2,
        max_attempts=2,
        branch="forge/task-exhausted",
        worktree_path=tmp_path / "worktree-exhausted",
    )
    db.add_task(retryable)
    db.add_task(exhausted)

    recovered = db.recover_interrupted_tasks()

    assert len(recovered) == 2
    retryable_after = db.get_task(retryable.id)
    exhausted_after = db.get_task(exhausted.id)
    assert retryable_after.state == TaskState.READY
    assert retryable_after.branch == retryable.branch
    assert retryable_after.worktree_path == retryable.worktree_path
    assert exhausted_after.state == TaskState.FAILED


def test_blocked_task_does_not_prevent_ready_task_claim(tmp_path: Path):
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    project = Project(id="project-1", name="demo", repo_path=tmp_path / "repo")
    db.add_project(project)
    for task_id, state in (("TASK-blocked", TaskState.BLOCKED), ("TASK-ready", TaskState.READY)):
        db.add_task(
            Task(
                id=task_id,
                project_id=project.id,
                title=task_id,
                objective="test",
                acceptance=[],
                validation_commands=[],
                state=state,
            )
        )
    claimed = db.claim_ready_tasks(2)
    assert [task.id for task in claimed] == ["TASK-ready"]


def test_runs_record_effort_and_sum_usage(tmp_path: Path):
    db = ForgeDB(tmp_path / "forge.db")
    db.initialize()
    project = Project(id="project-1", name="demo", repo_path=tmp_path / "repo")
    db.add_project(project)
    task = Task(
        id="TASK-usage",
        project_id=project.id,
        title="Usage",
        objective="record",
        acceptance=[],
        validation_commands=[],
        state=TaskState.READY,
    )
    db.add_task(task)
    run_id = db.record_run_start(task.id, "implementation", "balanced", "medium", tmp_path / "run")
    db.record_run_end(
        run_id,
        thread_id="thread-1",
        exit_code=0,
        usage={"input_tokens": 12, "output_tokens": 3},
    )
    with db.connect() as connection:
        effort = connection.execute("SELECT effort FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    assert effort == "medium"
    assert db.task_token_usage(task.id) == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
    }
