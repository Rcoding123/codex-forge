from pathlib import Path

from codex_forge.models import Project, Task, TaskState
from codex_forge.prompts import BASE_POLICY, review_prompt


def test_follow_up_review_is_scoped_to_prior_findings(tmp_path: Path):
    project = Project(id="project-1", name="demo", repo_path=tmp_path)
    task = Task(
        id="TASK-1",
        project_id=project.id,
        title="Task",
        objective="work",
        acceptance=["works"],
        validation_commands=[],
        state=TaskState.REVIEWING,
    )
    prompt = review_prompt(
        project,
        task,
        tmp_path,
        previous_findings=[{"severity": "high", "message": "remove blocker"}],
    )
    assert "FOLLOW-UP REVIEW" in prompt
    assert "do not repeat unrelated repository exploration" in prompt
    assert "remove blocker" in prompt


def test_worker_policy_rejects_unnecessary_repetition():
    assert "Do not repeat successful commands" in BASE_POLICY
    assert "unchanged file reads" in BASE_POLICY
    assert "identical failed commands" in BASE_POLICY
