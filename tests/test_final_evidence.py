from __future__ import annotations

import json
import subprocess
from pathlib import Path

from codex_forge.config import ForgeConfig
from codex_forge.gitops import changed_files_between, commit_task, current_commit, write_patch
from codex_forge.models import CodexRunResult, Project, Task, TaskState, ValidationResult
from codex_forge.orchestrator import OUTCOME_COMPLETED, OUTCOME_VALIDATION_FAILED, Orchestrator
from codex_forge.runner import SandboxResolution


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout


def initialize_repository(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Forge Evidence Test")
    git(repo, "config", "user.email", "forge-evidence@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    (repo / "revert.txt").write_text("original\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def completed_result(changed_files: list[str], summary: str) -> dict:
    return {
        "status": "completed",
        "summary": summary,
        "changed_files": changed_files,
        "validation_performed": [],
        "assumptions": [],
        "blocking_reason": None,
        "decisions_needed": [],
        "reusable_patterns": [],
    }


class ScriptedRunner:
    def __init__(self):
        self.phases: list[tuple[int, str]] = []

    def resolve_sandbox(self, requested: str, *, cwd: Path) -> SandboxResolution:
        return SandboxResolution(requested=requested, sandbox="danger-full-access", probe=None)

    def run(self, *, cwd, artifact_dir, phase, **_kwargs) -> CodexRunResult:
        attempt = int(Path(artifact_dir).parent.name.removeprefix("attempt-"))
        self.phases.append((attempt, phase))
        worktree = Path(cwd)

        if phase == "implementation" and attempt == 1:
            (worktree / "a.txt").write_text("attempt one\n", encoding="utf-8")
            (worktree / "base.txt").write_text("attempt one\n", encoding="utf-8")
            (worktree / "c.txt").write_text("attempt one\n", encoding="utf-8")
            final = completed_result(["a.txt", "base.txt", "c.txt"], "first attempt")
        elif phase == "implementation":
            (worktree / "base.txt").write_text("attempt two\n", encoding="utf-8")
            final = completed_result(["base.txt"], "second attempt repair")
        elif phase == "review":
            final = {
                "verdict": "changes_required",
                "summary": "One repair remains.",
                "acceptance_criteria": [],
                "binding_requirements": [],
                "findings": [
                    {
                        "severity": "medium",
                        "file": "base.txt",
                        "line": 1,
                        "issue": "Use the final value.",
                        "required_fix": "Replace the line.",
                    }
                ],
            }
        elif phase == "review-repair-1":
            (worktree / "base.txt").write_text("final repair\n", encoding="utf-8")
            final = completed_result(["base.txt"], "review repair")
        else:
            final = {
                "verdict": "pass",
                "summary": "Candidate is complete.",
                "acceptance_criteria": [],
                "binding_requirements": [],
                "findings": [],
            }

        artifact = Path(artifact_dir)
        artifact.mkdir(parents=True, exist_ok=True)
        final_path = artifact / f"{phase}.final.json"
        final_path.write_text(json.dumps(final), encoding="utf-8")
        events_path = artifact / f"{phase}.events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "git diff --check",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stderr_path = artifact / f"{phase}.stderr.log"
        stderr_path.write_text("", encoding="utf-8")
        return CodexRunResult(
            exit_code=0,
            final=final,
            jsonl_path=events_path,
            final_path=final_path,
            stderr_path=stderr_path,
            usage={},
        )


def make_orchestrator(tmp_path: Path, repo: Path) -> tuple[Orchestrator, Task]:
    config = ForgeConfig(
        config_path=tmp_path / "forge.toml",
        workspace_root=tmp_path / "worktrees",
        data_root=tmp_path / "data",
        repository_root=tmp_path / "repositories",
        inbox_root=tmp_path / "inbox",
        database_path=tmp_path / "data" / "forge.db",
        evidence_root=tmp_path / "evidence",
        knowledge_root=tmp_path / "knowledge",
        max_repair_cycles=0,
        max_task_tokens=0,
    )
    orchestrator = Orchestrator(config, Path(__file__).parents[1])
    project = Project(
        id="project-final-evidence",
        name="final-evidence",
        repo_path=repo,
        default_branch="main",
        validation_commands=["controlled-validation"],
        auto_fetch=False,
        auto_commit=True,
        auto_push=False,
    )
    task = Task(
        id="SC-R1-final-evidence",
        project_id=project.id,
        title="Exercise repair evidence",
        objective="Produce a complete final snapshot.",
        acceptance=[],
        validation_commands=[],
        state=TaskState.IMPLEMENTING,
        attempts=1,
        max_attempts=3,
    )
    orchestrator.db.add_project(project)
    orchestrator.db.add_task(task)
    orchestrator.runner = ScriptedRunner()
    return orchestrator, task


def passing_validation(tmp_path: Path, passed: bool) -> list[ValidationResult]:
    return [
        ValidationResult(
            command="controlled-validation",
            exit_code=0 if passed else 1,
            passed=passed,
            duration_seconds=0.01,
            stdout_path=tmp_path / "validation.stdout.log",
            stderr_path=tmp_path / "validation.stderr.log",
            summary="passed" if passed else "failed",
        )
    ]


def test_multi_attempt_terminal_evidence_is_the_complete_base_to_candidate_snapshot(
    tmp_path: Path, monkeypatch
):
    repo, approved_base = initialize_repository(tmp_path)
    orchestrator, first_attempt = make_orchestrator(tmp_path, repo)
    validation_sweeps: list[tuple[int, str]] = []

    def validate(task, *_args, suffix="initial", **_kwargs):
        validation_sweeps.append((task.attempts, suffix))
        return passing_validation(tmp_path, task.attempts > 1)

    monkeypatch.setattr(orchestrator, "_validate", validate)

    assert orchestrator._execute_task_body(first_attempt) == OUTCOME_VALIDATION_FAILED
    second_attempt = orchestrator.db.claim_ready_tasks(1)[0]
    assert second_attempt.id == first_attempt.id
    assert second_attempt.attempts == 2
    orchestrator.config.max_repair_cycles = 1
    assert orchestrator._execute_task_body(second_attempt) == OUTCOME_COMPLETED

    attempt_one = json.loads(
        (
            tmp_path
            / "evidence/final-evidence/SC-R1-final-evidence/attempt-01/implementation/implementation.final.json"
        ).read_text(encoding="utf-8")
    )
    attempt_two = json.loads(
        (
            tmp_path
            / "evidence/final-evidence/SC-R1-final-evidence/attempt-02/implementation/implementation.final.json"
        ).read_text(encoding="utf-8")
    )
    repair = json.loads(
        (
            tmp_path
            / "evidence/final-evidence/SC-R1-final-evidence/attempt-02/review-repair-1/review-repair-1.final.json"
        ).read_text(encoding="utf-8")
    )
    terminal_path = (
        tmp_path / "evidence/final-evidence/SC-R1-final-evidence/attempt-02/task-result.json"
    )
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))

    assert attempt_one["changed_files"] == ["a.txt", "base.txt", "c.txt"]
    assert attempt_two["changed_files"] == ["base.txt"]
    assert repair["changed_files"] == ["base.txt"]
    assert terminal["changed_files"] == ["a.txt", "base.txt", "c.txt"]
    assert orchestrator.runner.phases == [
        (1, "implementation"),
        (2, "implementation"),
        (2, "review"),
        (2, "review-repair-1"),
        (2, "review-2"),
    ]
    assert validation_sweeps == [
        (1, "initial"),
        (2, "initial"),
        (2, "review-repair-1"),
        (2, "final"),
    ]
    assert terminal["commit"] == git(repo, "rev-parse", "forge/sc-r1-final-evidence-exercise-repair-evidence").strip()
    assert terminal["changed_files"] == sorted(
        git(repo, "diff", "--name-only", f"{approved_base}..{terminal['commit']}", "--").splitlines()
    )
    expected_patch = git(repo, "diff", "--binary", f"{approved_base}..{terminal['commit']}", "--")
    assert Path(terminal["patch"]).read_text(encoding="utf-8") == expected_patch
    assert orchestrator.db.get_task(first_attempt.id).result == terminal


def test_one_attempt_final_file_set_equals_the_git_snapshot(tmp_path: Path):
    repo, approved_base = initialize_repository(tmp_path)
    (repo / "base.txt").write_text("changed\n", encoding="utf-8")
    task = Task(
        id="SC-R1-one-attempt",
        project_id="project",
        title="One attempt",
        objective="Change one file.",
        acceptance=[],
        validation_commands=[],
        state=TaskState.IMPLEMENTING,
    )
    candidate = commit_task(repo, task)
    assert candidate is not None
    assert changed_files_between(repo, approved_base, candidate) == ["base.txt"]


def test_repair_adds_a_new_file_to_the_complete_snapshot(tmp_path: Path):
    repo, approved_base = initialize_repository(tmp_path)
    (repo / "base.txt").write_text("implementation\n", encoding="utf-8")
    (repo / "repair.txt").write_text("repair\n", encoding="utf-8")
    task = Task(
        id="SC-R1-new-file",
        project_id="project",
        title="Repair adds file",
        objective="Add a repair file.",
        acceptance=[],
        validation_commands=[],
        state=TaskState.IMPLEMENTING,
    )
    candidate = commit_task(repo, task)
    assert candidate is not None
    assert changed_files_between(repo, approved_base, candidate) == ["base.txt", "repair.txt"]


def test_reverted_implementation_work_is_absent_from_final_evidence(tmp_path: Path):
    repo, approved_base = initialize_repository(tmp_path)
    (repo / "revert.txt").write_text("implementation\n", encoding="utf-8")
    (repo / "base.txt").write_text("kept\n", encoding="utf-8")
    (repo / "revert.txt").write_text("original\n", encoding="utf-8")
    task = Task(
        id="SC-R1-revert",
        project_id="project",
        title="Revert prior work",
        objective="Keep only the final diff.",
        acceptance=[],
        validation_commands=[],
        state=TaskState.IMPLEMENTING,
    )
    candidate = commit_task(repo, task)
    assert candidate is not None
    assert changed_files_between(repo, approved_base, candidate) == ["base.txt"]


def test_patch_artifact_is_the_exact_complete_committed_diff(tmp_path: Path):
    repo, approved_base = initialize_repository(tmp_path)
    (repo / "base.txt").unlink()
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    task = Task(
        id="SC-R1-patch",
        project_id="project",
        title="Complete patch",
        objective="Exercise add and delete.",
        acceptance=[],
        validation_commands=[],
        state=TaskState.IMPLEMENTING,
    )
    candidate = commit_task(repo, task)
    assert candidate is not None
    patch = tmp_path / "changes.patch"
    write_patch(repo, patch, approved_base, candidate)
    assert patch.read_text(encoding="utf-8") == git(
        repo, "diff", "--binary", f"{approved_base}..{candidate}", "--"
    )
    assert changed_files_between(repo, approved_base, candidate) == ["base.txt", "new.txt"]
    assert current_commit(repo) == candidate
