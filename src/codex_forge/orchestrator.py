from __future__ import annotations

import json
import hashlib
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import ForgeConfig
from .db import ForgeDB
from .gitops import commit_task, prepare_worktree, write_patch
from .knowledge import KnowledgeStore
from .models import Project, Task, TaskState, ValidationResult
from .prompts import implementation_prompt, review_prompt, validation_failure_summary
from .runner import CodexRunner
from .utils import ControllerAlreadyRunning, controller_lock, ensure_dir, flatten_unique, tail_text, utc_now
from .validation import run_validations


class Orchestrator:
    def __init__(self, config: ForgeConfig, package_root: Path):
        self.config = config
        self.package_root = package_root
        self.db = ForgeDB(config.database_path)
        self.db.initialize()
        self.runner = CodexRunner(config, package_root / "schemas")
        self.knowledge = KnowledgeStore(config.knowledge_root)
        for path in (
            config.workspace_root,
            config.data_root,
            config.repository_root,
            config.inbox_root,
            config.evidence_root,
            config.knowledge_root,
        ):
            ensure_dir(path)

    def run_once(self) -> int:
        try:
            with controller_lock(self.config.data_root / "controller.lock"):
                self._recover_interrupted_tasks()
                return self._run_once_unlocked()
        except ControllerAlreadyRunning as exc:
            self._write_controller_error(str(exc))
            return 0

    def _run_once_unlocked(self) -> int:
        self.import_inbox()
        tasks = self.db.claim_ready_tasks(self.config.max_parallel)
        if not tasks:
            return 0
        with ThreadPoolExecutor(max_workers=self.config.max_parallel) as pool:
            futures = {pool.submit(self._execute_task, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    self._handle_unexpected_failure(task, exc)
        return len(tasks)

    def run_daemon(self) -> None:
        try:
            with controller_lock(self.config.data_root / "controller.lock"):
                self._recover_interrupted_tasks()
                while True:
                    try:
                        processed = self._run_once_unlocked()
                    except KeyboardInterrupt:
                        return
                    except Exception:
                        self._write_controller_error(traceback.format_exc())
                        processed = 0
                    if processed == 0:
                        time.sleep(self.config.poll_seconds)
        except ControllerAlreadyRunning as exc:
            self._write_controller_error(str(exc))

    def import_inbox(self) -> int:
        try:
            import yaml
        except ImportError:
            return 0
        imported = 0
        processed_dir = ensure_dir(self.config.inbox_root / "processed")
        failed_dir = ensure_dir(self.config.inbox_root / "failed")
        for path in sorted(self.config.inbox_root.glob("*.y*ml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                self._task_from_yaml(data)
                path.replace(processed_dir / path.name)
                imported += 1
            except Exception as exc:
                (failed_dir / f"{path.stem}.error.txt").write_text(str(exc), encoding="utf-8")
                path.replace(failed_dir / path.name)
        return imported

    def _task_from_yaml(self, data: dict) -> None:
        from .models import Task
        from .utils import stable_id

        project_name = str(data["project"])
        project = self.db.get_project(project_name)
        if not project:
            raise ValueError(f"Unknown project: {project_name}")
        title = str(data["title"])
        objective = str(data["objective"])
        task_id = str(data.get("id") or stable_id("TASK", f"{project.id}:{title}:{objective}"))
        dependencies = list(data.get("dependencies") or [])
        state = TaskState.BACKLOG if dependencies else TaskState.READY
        task = Task(
            id=task_id,
            project_id=project.id,
            title=title,
            objective=objective,
            acceptance=list(data.get("acceptance") or []),
            validation_commands=list(data.get("validation") or []),
            state=state,
            priority=int(data.get("priority", 100)),
            risk=str(data.get("risk", "medium")),
            tags=list(data.get("tags") or []),
            dependencies=dependencies,
            max_attempts=int(data.get("max_attempts", 3)),
        )
        self.db.add_task(task)

    def _execute_task(self, task: Task) -> None:
        project = self.db.get_project(task.project_id)
        if not project:
            raise RuntimeError(f"Project missing for task {task.id}: {task.project_id}")
        artifact_dir = ensure_dir(self.config.evidence_root / project.name / task.id / f"attempt-{task.attempts:02d}")
        branch, worktree = prepare_worktree(project, task, self.config.workspace_root)
        self.db.update_task(task.id, branch=branch, worktree_path=worktree)
        task.branch = branch
        task.worktree_path = worktree

        patterns = self.knowledge.relevant(project.id, f"{task.title} {task.objective} {' '.join(task.tags)}")
        result = self._run_codex_phase(
            task,
            project,
            artifact_dir,
            phase="implementation",
            model=self._implementation_model(task),
            effort=self._implementation_effort(task),
            prompt=implementation_prompt(
                project,
                task,
                worktree,
                patterns,
                previous_failure=task.last_error if task.attempts > 1 else None,
            ),
            schema="task-result.schema.json",
        )
        if self._handle_noncompletion(task, result.final):
            return

        commands = flatten_unique([*project.validation_commands, *task.validation_commands])
        validation_results = self._validate(task, worktree, artifact_dir, commands)
        repair_cycle = 0
        while not self._all_passed(validation_results) and repair_cycle < self.config.max_repair_cycles:
            repair_cycle += 1
            self.db.update_task(task.id, state=TaskState.REPAIRING)
            failure = validation_failure_summary(validation_results)
            result = self._run_codex_phase(
                task,
                project,
                artifact_dir,
                phase=f"validation-repair-{repair_cycle}",
                model=self.config.implementation_model,
                effort="high",
                prompt=implementation_prompt(project, task, worktree, patterns, previous_failure=failure),
                schema="task-result.schema.json",
            )
            if self._handle_noncompletion(task, result.final):
                return
            validation_results = self._validate(task, worktree, artifact_dir, commands, suffix=f"repair-{repair_cycle}")

        if not self._all_passed(validation_results):
            self._retry_or_fail(task, validation_failure_summary(validation_results))
            return

        self.db.update_task(task.id, state=TaskState.REVIEWING)
        review = self._run_codex_phase(
            task,
            project,
            artifact_dir,
            phase="review",
            model=self._review_model(task),
            effort=self._review_effort(task),
            prompt=review_prompt(project, task, worktree),
            schema="review-result.schema.json",
            sandbox="read-only",
        )
        review_final = review.final

        review_cycle = 0
        while review_final.get("verdict") == "changes_required" and review_cycle < self.config.max_repair_cycles:
            review_cycle += 1
            findings = review_final.get("findings") or []
            self.db.update_task(task.id, state=TaskState.REPAIRING)
            repair = self._run_codex_phase(
                task,
                project,
                artifact_dir,
                phase=f"review-repair-{review_cycle}",
                model=self.config.implementation_model,
                effort="high",
                prompt=implementation_prompt(project, task, worktree, patterns, review_findings=findings),
                schema="task-result.schema.json",
            )
            if self._handle_noncompletion(task, repair.final):
                return
            result = repair
            validation_results = self._validate(task, worktree, artifact_dir, commands, suffix=f"review-repair-{review_cycle}")
            if not self._all_passed(validation_results):
                continue
            review = self._run_codex_phase(
                task,
                project,
                artifact_dir,
                phase=f"review-{review_cycle + 1}",
                model=self._review_model(task),
                effort=self._review_effort(task),
                prompt=review_prompt(project, task, worktree, previous_findings=findings),
                schema="review-result.schema.json",
                sandbox="read-only",
            )
            review_final = review.final

        if review_final.get("verdict") != "pass":
            self._retry_or_fail(task, json.dumps(review_final, ensure_ascii=False))
            return

        validation_results = self._validate(task, worktree, artifact_dir, commands, suffix="final")
        if not self._all_passed(validation_results):
            self._retry_or_fail(task, validation_failure_summary(validation_results))
            return

        patch_path = artifact_dir / "changes.patch"
        write_patch(worktree, patch_path)
        commit_hash = commit_task(worktree, task) if project.auto_commit else None
        if project.auto_push and commit_hash:
            from .gitops import _git
            _git(worktree, "push", "-u", "origin", branch, timeout=1200)

        final_result = {
            **result.final,
            "status": "completed",
            "controller_validation": [self._validation_to_dict(item) for item in validation_results],
            "review": review_final,
            "branch": branch,
            "commit": commit_hash,
            "worktree": str(worktree),
            "patch": str(patch_path),
            "completed_at": utc_now(),
        }
        (artifact_dir / "task-result.json").write_text(
            json.dumps(final_result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.knowledge.add_patterns(task.id, project.id, result.final.get("reusable_patterns") or [])
        self.db.update_task(task.id, state=TaskState.COMPLETED, result=final_result, last_error="")

    def _run_codex_phase(
        self,
        task: Task,
        project: Project,
        artifact_dir: Path,
        *,
        phase: str,
        model: str,
        effort: str,
        prompt: str,
        schema: str,
        sandbox: str | None = None,
    ):
        phase_dir = ensure_dir(artifact_dir / phase)
        self._enforce_usage_budget(task)
        run_id = self.db.record_run_start(task.id, phase, model, effort, phase_dir)
        result = self.runner.run(
            prompt=prompt,
            cwd=task.worktree_path or project.repo_path,
            artifact_dir=phase_dir,
            phase=phase,
            model=model,
            effort=effort,
            schema_name=schema,
            sandbox=sandbox,
        )
        self.db.record_run_end(
            run_id,
            thread_id=result.thread_id,
            exit_code=result.exit_code,
            usage=result.usage,
        )
        self._enforce_usage_budget(task)
        if result.exit_code != 0:
            raise RuntimeError(
                f"Codex phase {phase} failed with exit {result.exit_code}: "
                f"{tail_text(result.stderr_path.read_text(encoding='utf-8', errors='replace'))}"
            )
        return result

    def _validate(
        self,
        task: Task,
        worktree: Path,
        artifact_dir: Path,
        commands: list[str],
        suffix: str = "initial",
    ) -> list[ValidationResult]:
        self.db.update_task(task.id, state=TaskState.VALIDATING)
        if not commands:
            return []
        return run_validations(
            commands,
            cwd=worktree,
            artifact_dir=artifact_dir / f"validation-{suffix}",
            timeout_seconds=self.config.task_timeout_minutes * 60,
            max_summary_chars=self.config.max_output_chars,
        )

    def _handle_noncompletion(self, task: Task, final: dict) -> bool:
        status = final.get("status")
        if status == "completed":
            return False
        if status == "needs_decision":
            for decision in final.get("decisions_needed") or []:
                self.db.add_decision(
                    task.id,
                    decision.get("question", "Decision required"),
                    decision.get("options") or [],
                    decision.get("recommendation"),
                )
            self.db.update_task(task.id, state=TaskState.NEEDS_DECISION, result=final)
            return True
        reason = final.get("blocking_reason") or final.get("summary") or "Codex reported blocked."
        self._retry_or_fail(task, str(reason))
        return True

    def _retry_or_fail(self, task: Task, error: str) -> None:
        bounded_error = tail_text(error, self.config.max_output_chars)
        normalized = " ".join(bounded_error.split())
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        identical_failures = task.identical_failures + 1 if task.failure_fingerprint == fingerprint else 1
        loop_detected = identical_failures >= self.config.max_identical_failures
        state = (
            TaskState.FAILED
            if loop_detected or task.attempts >= task.max_attempts
            else TaskState.READY
        )
        if loop_detected:
            bounded_error = f"LOOP_DETECTED after {identical_failures} identical failures.\n{bounded_error}"
        self.db.update_task(
            task.id,
            state=state,
            last_error=bounded_error,
            failure_fingerprint=fingerprint,
            identical_failures=identical_failures,
        )

    def _handle_unexpected_failure(self, task: Task, exc: Exception) -> None:
        if isinstance(exc, TaskUsageBudgetExceeded):
            self.db.update_task(task.id, state=TaskState.FAILED, last_error=str(exc))
            return
        self._retry_or_fail(task, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

    def _write_controller_error(self, content: str) -> None:
        root = ensure_dir(self.config.data_root / "controller-errors")
        path = root / f"{int(time.time())}.log"
        path.write_text(content, encoding="utf-8")

    def _implementation_model(self, task: Task) -> str:
        return self.config.escalation_model if task.risk.lower() in {"critical", "high"} else self.config.implementation_model

    def _implementation_effort(self, task: Task) -> str:
        return self.config.escalation_effort if task.risk.lower() in {"critical", "high"} else self.config.implementation_effort

    def _review_model(self, task: Task) -> str:
        return self.config.escalation_model if task.risk.lower() in {"critical", "high"} else self.config.review_model

    def _review_effort(self, task: Task) -> str:
        return self.config.escalation_effort if task.risk.lower() in {"critical", "high"} else self.config.review_effort

    def _enforce_usage_budget(self, task: Task) -> None:
        if self.config.max_task_tokens <= 0:
            return
        usage = self.db.task_token_usage(task.id)
        if usage["total_tokens"] >= self.config.max_task_tokens:
            raise TaskUsageBudgetExceeded(
                f"TOKEN_BUDGET_EXCEEDED: {usage['total_tokens']} used of "
                f"{self.config.max_task_tokens} configured tokens for {task.id}."
            )

    def _recover_interrupted_tasks(self) -> None:
        recovered = self.db.recover_interrupted_tasks()
        if not recovered:
            return
        path = self.config.data_root / "recovery.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for item in recovered:
                handle.write(json.dumps({"timestamp": utc_now(), **item}) + "\n")

    @staticmethod
    def _all_passed(results: list[ValidationResult]) -> bool:
        return all(item.passed for item in results)

    @staticmethod
    def _validation_to_dict(result: ValidationResult) -> dict:
        return {
            "command": result.command,
            "exit_code": result.exit_code,
            "passed": result.passed,
            "duration_seconds": round(result.duration_seconds, 3),
            "stdout": str(result.stdout_path),
            "stderr": str(result.stderr_path),
        }


class TaskUsageBudgetExceeded(RuntimeError):
    pass
