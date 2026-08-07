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
from .runner import (
    CodexRunner,
    ReviewInfrastructureBlock,
    detect_review_infrastructure_block,
    sandbox_infrastructure_block,
)
from .telemetry import (
    ATTR_ATTEMPT_TERMINATED_BY,
    ERROR_CODEX_EXIT_NONZERO,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_NEEDS_DECISION,
    OUTCOME_NONCOMPLETION,
    OUTCOME_OK,
    OUTCOME_REVIEW_CHANGES_REQUIRED,
    OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED,
    OUTCOME_SKIPPED,
    OUTCOME_VALIDATION_FAILED,
    PHASE_COMPLETION,
    PHASE_EVIDENCE,
    ROLE_CONTROLLER,
    ROLE_IMPLEMENTER,
    ROLE_REVIEWER,
    ROLE_VALIDATOR,
    TERMINATED_BY_EXCEPTION,
    TERMINATED_BY_OUTCOME,
    SpanHandle,
    build_telemetry,
    normalize_phase,
    phase_usage_attributes,
    usage_attributes,
)
from .usage import BUDGET_BASIS, budget_used_tokens
from .utils import ControllerAlreadyRunning, controller_lock, ensure_dir, flatten_unique, tail_text, utc_now
from .validation import run_validations


# The reviewer is asked for a read-only sandbox. Whether it gets one is decided by whether that
# sandbox can launch a command on this host, not assumed.
REVIEW_SANDBOX = "read-only"

# Written next to the review evidence, and the prefix `control_plane._safe_task_error` matches so
# Shared Chat reports an infrastructure fault rather than a generic task error.
REVIEW_INFRASTRUCTURE_BLOCK_FILENAME = "review-infrastructure-block.json"
REVIEW_INFRASTRUCTURE_BLOCKED = "REVIEW_INFRASTRUCTURE_BLOCKED"


class Orchestrator:
    def __init__(self, config: ForgeConfig, package_root: Path):
        self.config = config
        self.package_root = package_root
        self.db = ForgeDB(config.database_path)
        self.db.initialize()
        self.runner = CodexRunner(config, package_root / "schemas")
        self.knowledge = KnowledgeStore(config.knowledge_root)
        # Inert unless `[telemetry] enabled = true`. Built before any worker thread starts.
        self.telemetry = build_telemetry(config.telemetry)
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
        """Execute one attempt of a task, traced as forge.task -> forge.attempt -> forge.phase.

        The spans wrap the unchanged body: the outcome the body reports is recorded as an
        attribute, and an exception is re-raised to `_handle_unexpected_failure` exactly as before
        after the span helpers have marked and closed both spans.
        """
        with self.telemetry.task_span(
            task_id=task.id,
            project_id=task.project_id,
            risk=task.risk,
            max_attempts=task.max_attempts,
        ) as task_span, self.telemetry.attempt_span(attempt_number=task.attempts) as attempt_span:
            outcome = self._execute_task_body(task)
            task_span.set_outcome(outcome)
            attempt_span.set_outcome(outcome)
            # `.get` rather than indexing: a lookup miss must never raise out of instrumentation
            # and be handled as a task failure.
            attempt_span.set(
                ATTR_ATTEMPT_TERMINATED_BY, TERMINATED_BY_OUTCOME.get(outcome, TERMINATED_BY_EXCEPTION)
            )
            self._record_task_usage(task_span, task)

    def _record_task_usage(self, span: SpanHandle, task: Task) -> None:
        """Attach the task's categorized token totals to its span.

        Only read when a span is actually recording, so a disabled telemetry configuration issues
        no additional query. The read is read-only and guarded, so it cannot change a task outcome.
        """
        if not span.recording:
            return
        try:
            span.set_many(usage_attributes(self.db.task_token_usage(task.id)))
        except Exception:
            return

    def _execute_task_body(self, task: Task) -> str:
        """Run the task and report the bounded outcome that ended it.

        The returned value is observability only. Every caller of `_execute_task` ignored the
        return value before this slice and still does; task state is written to the database by the
        same calls as before.
        """
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
            role=ROLE_IMPLEMENTER,
        )
        if self._handle_noncompletion(task, result.final):
            return _noncompletion_outcome(result.final)

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
                role=ROLE_IMPLEMENTER,
            )
            if self._handle_noncompletion(task, result.final):
                return _noncompletion_outcome(result.final)
            validation_results = self._validate(task, worktree, artifact_dir, commands, suffix=f"repair-{repair_cycle}")

        if not self._all_passed(validation_results):
            self._retry_or_fail(task, validation_failure_summary(validation_results))
            return OUTCOME_VALIDATION_FAILED

        self.db.update_task(task.id, state=TaskState.REVIEWING)
        # Resolved before the reviewer is paid for: a sandbox that cannot launch a command produces
        # a review that inspects nothing, and the model call that produces it is pure waste.
        resolution = self.runner.resolve_sandbox(REVIEW_SANDBOX, cwd=worktree)
        if resolution.sandbox is None:
            return self._fail_review_infrastructure(
                task, artifact_dir, sandbox_infrastructure_block(resolution)
            )
        review = self._run_codex_phase(
            task,
            project,
            artifact_dir,
            phase="review",
            model=self._review_model(task),
            effort=self._review_effort(task),
            prompt=review_prompt(project, task, worktree),
            schema="review-result.schema.json",
            sandbox=resolution.sandbox,
            role=ROLE_REVIEWER,
        )
        blocked = detect_review_infrastructure_block(
            review, requested_sandbox=resolution.requested, sandbox=resolution.sandbox
        )
        if blocked is not None:
            return self._fail_review_infrastructure(task, artifact_dir, blocked)
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
                role=ROLE_IMPLEMENTER,
            )
            if self._handle_noncompletion(task, repair.final):
                return _noncompletion_outcome(repair.final)
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
                sandbox=resolution.sandbox,
                role=ROLE_REVIEWER,
            )
            blocked = detect_review_infrastructure_block(
                review, requested_sandbox=resolution.requested, sandbox=resolution.sandbox
            )
            if blocked is not None:
                return self._fail_review_infrastructure(task, artifact_dir, blocked)
            review_final = review.final

        if review_final.get("verdict") != "pass":
            self._retry_or_fail(task, json.dumps(review_final, ensure_ascii=False))
            return OUTCOME_REVIEW_CHANGES_REQUIRED

        validation_results = self._validate(task, worktree, artifact_dir, commands, suffix="final")
        if not self._all_passed(validation_results):
            self._retry_or_fail(task, validation_failure_summary(validation_results))
            return OUTCOME_VALIDATION_FAILED

        with self.telemetry.phase_span(phase_name=PHASE_EVIDENCE, role=ROLE_CONTROLLER) as span:
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
                # Retained so a completed task records which sandbox the independent review actually
                # ran under, including when the requested one could not launch a command here.
                "review_sandbox": resolution.sandbox,
                "review_sandbox_requested": resolution.requested,
                "branch": branch,
                "commit": commit_hash,
                "worktree": str(worktree),
                "patch": str(patch_path),
                "completed_at": utc_now(),
            }
            (artifact_dir / "task-result.json").write_text(
                json.dumps(final_result, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            span.set_outcome(OUTCOME_OK)

        with self.telemetry.phase_span(phase_name=PHASE_COMPLETION, role=ROLE_CONTROLLER) as span:
            self.knowledge.add_patterns(task.id, project.id, result.final.get("reusable_patterns") or [])
            self.db.update_task(task.id, state=TaskState.COMPLETED, result=final_result, last_error="")
            span.set_outcome(OUTCOME_OK)
        return OUTCOME_COMPLETED

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
        role: str = ROLE_IMPLEMENTER,
    ):
        phase_name, cycle = normalize_phase(phase)
        with self.telemetry.phase_span(
            phase_name=phase_name, cycle=cycle, role=role, model=model, effort=effort
        ) as span:
            phase_dir = ensure_dir(artifact_dir / phase)
            # Both boundaries enforce the same new-token basis: this one refuses to start a phase
            # the budget cannot cover, the one below stops the task as soon as a completed phase
            # has spent it.
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
            if span.recording:
                span.set_many(phase_usage_attributes(result.usage))
            self._enforce_usage_budget(task)
            if result.exit_code != 0:
                # Recorded before raising so the span carries the specific cause; the raised text
                # itself is never attached, because it embeds a stderr tail.
                span.set_error_code(ERROR_CODEX_EXIT_NONZERO)
                raise RuntimeError(
                    f"Codex phase {phase} failed with exit {result.exit_code}: "
                    f"{tail_text(result.stderr_path.read_text(encoding='utf-8', errors='replace'))}"
                )
            span.set_outcome(OUTCOME_OK)
            return result

    def _validate(
        self,
        task: Task,
        worktree: Path,
        artifact_dir: Path,
        commands: list[str],
        suffix: str = "initial",
    ) -> list[ValidationResult]:
        phase_name, cycle = normalize_phase(suffix, kind="validation")
        with self.telemetry.phase_span(
            phase_name=phase_name, cycle=cycle, role=ROLE_VALIDATOR
        ) as span:
            self.db.update_task(task.id, state=TaskState.VALIDATING)
            if not commands:
                span.set_outcome(OUTCOME_SKIPPED)
                return []
            results = run_validations(
                commands,
                cwd=worktree,
                artifact_dir=artifact_dir / f"validation-{suffix}",
                timeout_seconds=self.config.task_timeout_minutes * 60,
                max_summary_chars=self.config.max_output_chars,
            )
            span.set_outcome(OUTCOME_OK if self._all_passed(results) else OUTCOME_FAILED)
            return results

    def _fail_review_infrastructure(
        self, task: Task, artifact_dir: Path, block: ReviewInfrastructureBlock
    ) -> str:
        """End the task on a review that never ran, without treating it as a defect in the code.

        A reviewer whose command runner is unavailable has said nothing about the candidate, so this
        starts no repair phase and makes no further model call — the two repair cycles the live
        canary spent could not have repaired an environment fault. It also refuses to complete the
        task: passing validation is not a substitute for the independent review that did not happen.
        The task fails closed rather than retrying, because every attempt on this host would re-run
        implementation into the same unusable reviewer.
        """
        record = block.to_dict()
        (artifact_dir / REVIEW_INFRASTRUCTURE_BLOCK_FILENAME).write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        detail = json.dumps(record, ensure_ascii=False)
        # The code stays at the front of `last_error`: `tail_text` keeps the tail, so bounding the
        # composed string would cut off the very prefix the control plane matches on.
        self.db.update_task(
            task.id,
            state=TaskState.FAILED,
            last_error=f"{REVIEW_INFRASTRUCTURE_BLOCKED}: "
            + tail_text(f"{block.summary()}\n{detail}", self.config.max_output_chars),
        )
        return OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED

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
        """Stop a task whose new-token usage has reached the configured ceiling.

        The budget counts new work only — `budget_used_tokens`, the fresh input plus the output of
        every completed run. Cached input reads are excluded, so a task that re-reads a large cached
        prompt many times is not charged for it. `raw_total_tokens` stays available for traffic
        diagnostics and is reported in the error, but it decides nothing.

        This runs at phase boundaries — before a Codex phase starts and after its run is recorded —
        so a single in-flight phase can overshoot the remaining budget. The runner has no
        intra-request token cap to enforce against, and Codex CLI 0.146.0 exposes none.
        """
        if self.config.max_task_tokens <= 0:
            return
        usage = self.db.task_token_usage(task.id)
        budgeted = budget_used_tokens(usage)
        if budgeted >= self.config.max_task_tokens:
            raise TaskUsageBudgetExceeded(
                f"TOKEN_BUDGET_EXCEEDED: {budgeted} new tokens used of "
                f"{self.config.max_task_tokens} configured tokens for {task.id} "
                f"(basis {BUDGET_BASIS}; raw traffic {usage['raw_total_tokens']} tokens, "
                f"cached input {usage['cached_input_tokens']} tokens not budgeted)."
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


def _noncompletion_outcome(final: dict) -> str:
    """Name the bounded outcome for a Codex result that `_handle_noncompletion` already acted on.

    This reads the same `status` field that method reads and makes no decision of its own; the
    state written to the database is whatever `_handle_noncompletion` wrote.
    """
    return OUTCOME_NEEDS_DECISION if final.get("status") == "needs_decision" else OUTCOME_NONCOMPLETION
