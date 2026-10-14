from __future__ import annotations

import json
import hashlib
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from .config import DEFAULT_REVIEW_SANDBOX, ForgeConfig
from .db import ForgeDB
from .gitops import (
    BaseCommitUnavailable,
    CandidateStateUnavailable,
    GitError,
    WorktreeUnavailable,
    changed_files_between,
    commit_task,
    current_commit,
    explicit_candidate_commit,
    prepare_worktree,
    push_candidate,
    write_patch,
)
from .knowledge import KnowledgeStore
from .models import CodexRunResult, Project, Task, TaskState, ValidationResult
from .prompts import implementation_prompt, review_prompt, validation_failure_summary
from .processes import ProcessSupervisionError
from .schema_resources import load_schema
from .runner import (
    BLOCK_REVIEW_OUTPUT_UNUSABLE,
    BLOCK_REVIEW_TRANSPORT_FAILED,
    REVIEW_OUTPUT_COMMANDS_UNCLOSED,
    CodexRunner,
    decode_review_result,
    ReviewInfrastructureBlock,
    ReviewTransportError,
    SandboxResolution,
    detect_review_infrastructure_block,
    review_command_closure,
    sandbox_infrastructure_block,
    transport_http_status,
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
from .workflows import ProjectWorkflows


# The mode the reviewer is asked for when the operator has not configured one. Whether the reviewer
# gets what was asked for is decided by whether that sandbox can launch a command on this host, not
# assumed; `codex.review_sandbox` chooses what is asked for, never what is recorded as having run.
REVIEW_SANDBOX = DEFAULT_REVIEW_SANDBOX

# Written next to the review evidence, and the prefix `control_plane._safe_task_error` matches so
# Shared Chat reports an infrastructure fault rather than a generic task error.
REVIEW_INFRASTRUCTURE_BLOCK_FILENAME = "review-infrastructure-block.json"
REVIEW_INFRASTRUCTURE_BLOCKED = "REVIEW_INFRASTRUCTURE_BLOCKED"
# A task whose persisted admission base cannot be supplied by its registered repository.
# A repository fault, in the same class as a reviewer that could not run: nothing was
# learned about the candidate, so nothing about the candidate may be recorded.
REPOSITORY_UNAVAILABLE = "REPOSITORY_UNAVAILABLE"
CANDIDATE_STATE_UNAVAILABLE = "CANDIDATE_STATE_UNAVAILABLE"


class Orchestrator:
    def __init__(self, config: ForgeConfig, package_root: Path):
        self.config = config
        self.package_root = package_root
        self.db = ForgeDB(config.database_path)
        self.db.initialize()
        self.workflows = ProjectWorkflows(config, self.db)
        self.runner = CodexRunner(config)
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

    def resolve_blocker(self, task_id: str, reason: str, receipt: Path, receipt_sha256: str, *, prerequisite: bool = False) -> dict:
        """Resolve and execute under one lock; legacy controllers never see exhausted READY work."""
        from .blockers import queue_resolution, _public, _prove_plan

        with controller_lock(self.config.data_root / "controller.lock"):
            resolution = queue_resolution(self.config, task_id, reason, receipt, receipt_sha256, prerequisite=prerequisite)
            existing = self.db.blocker_continuation(task_id, resolution["attempt"])
            document = json.loads(existing["document_json"])
            def outcome(row):
                current = self.db.get_task(task_id)
                data = _public(row, document) | {"task_state": current.state.value}
                if prerequisite:
                    from .prerequisites import public_attribution

                    data["prerequisite_handoff"] = public_attribution(current)
                return data

            if existing["state"] == "TERMINAL":
                return outcome(existing)
            task = self.db.get_task(task_id)
            project = self.db.get_project(task.project_id)
            checkpoint = self.db.execution_checkpoint(task.id)
            if existing["state"] in ("QUEUED", "CLAIMED"):
                from .blockers import validate_continuation

                validate_continuation(self.config, self.db, task, project, {**existing, "state": "CLAIMED"})
            elif existing["state"] == "CONSUMED" and checkpoint["completion_json"] is not None:
                from .blockers import validate_continuation

                validate_continuation(self.config, self.db, task, project, existing, resume_completion=True)
            _prove_plan(self.config, self.db, project, task, checkpoint)
            plan = self.workflows.get(project.id)
            if plan and plan["state"] == "BLOCKED":
                self.workflows.resume(project.id)
            task = self.db.claim_blocker_continuation(task_id, task.attempts)
            try:
                self._execute_task(task)
            except BaseException as exc:
                latest = self.db.blocker_continuation(task.id, task.attempts)
                if latest["state"] == "CLAIMED":
                    # No paid call started: an explicit retry may use the same reservation.
                    self.db.update_task(task.id, state=TaskState.BLOCKED)
                    with self.db.connect() as connection:
                        connection.execute(
                            "UPDATE blocker_continuations SET state='QUEUED',updated_at=? WHERE task_id=? AND attempt=?",
                            (utc_now(), task.id, task.attempts),
                        )
                    raise
                if isinstance(exc, Exception):
                    self._handle_unexpected_failure(task, exc)
                else:
                    self.db.update_task(task.id, state=TaskState.FAILED, last_error="BLOCKER_CONTINUATION_INTERRUPTED")
                    raise
            finally:
                current = self.db.get_task(task.id)
                checkpoint = self.db.execution_checkpoint(task.id)
                latest = self.db.blocker_continuation(task.id, task.attempts)
                if (latest["state"] == "CONSUMED" and current.state in (TaskState.FAILED, TaskState.COMPLETED, TaskState.READY, TaskState.NEEDS_DECISION)
                        and (current.state != TaskState.FAILED or checkpoint["completion_json"] is None)):
                    with self.db.connect() as connection:
                        connection.execute(
                            "UPDATE blocker_continuations SET state='TERMINAL',updated_at=? WHERE task_id=? AND attempt=?",
                            (utc_now(), task.id, task.attempts),
                        )
            # Integration and actual postintegration acceptance use the existing controller path.
            self.workflows.advance_all()
            self.workflows.advance_all()
            latest = self.db.blocker_continuation(task.id, task.attempts)
            return outcome(latest)

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
        progressed = self.workflows.advance_all()
        tasks = self.db.claim_ready_tasks(self.config.max_parallel)
        if not tasks:
            return progressed
        with ThreadPoolExecutor(max_workers=self.config.max_parallel) as pool:
            futures = {pool.submit(self._execute_task, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    self._handle_unexpected_failure(task, exc)
        return len(tasks) + progressed

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
        from .task_submission import (
            _normalize_expected_registration,
            _normalize_task_id,
            insert_task_guarded,
            verify_expected_registration,
        )
        from .utils import stable_id

        project_name = str(data["project"])
        project = self.db.get_project(project_name)
        if not project:
            raise ValueError(f"Unknown project: {project_name}")
        # An inbox file may bind itself to a repository generation exactly as a contract caller
        # does. A supplied expectation is never discarded; an omitted one keeps the legacy
        # behavior, with the locked guard still holding the resolved generation to the insert.
        verify_expected_registration(
            project,
            _normalize_expected_registration(data.get("expected_repository_registration_id")),
        )
        title = str(data["title"])
        objective = str(data["objective"])
        # Inbox submissions pass through the same id authority as both CLI submission paths: the
        # shared normalizer refuses unsafe and aliasing ids, and the guarded insert refuses ids
        # colliding with a live case variant or retained evidence inside the insert transaction.
        task_id = _normalize_task_id(data.get("id")) or stable_id(
            "TASK", f"{project.id}:{title}:{objective}"
        )
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
            architecture_decisions=list(data.get("architecture_decisions") or []),
            constraints=list(data.get("constraints") or []),
            scope=str(data.get("scope") or ""),
            out_of_scope=str(data.get("out_of_scope") or ""),
            max_attempts=int(data.get("max_attempts", 3)),
        )
        insert_task_guarded(self.db, self.config, project, task)

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
        continuation = getattr(self.db, "blocker_continuation", lambda *_: None)(task.id, task.attempts)
        if continuation and continuation["state"] in ("CLAIMED", "CONSUMED"):
            # New artifacts never overwrite the failed attempt's original implementation/logs.
            artifact_dir = Path(json.loads(continuation["document_json"])["artifact_root"])
            if continuation["state"] == "CLAIMED":
                from .blockers import validate_continuation

                validate_continuation(self.config, self.db, task, project, continuation)
        checkpoint = self.db.execution_checkpoint(task.id)
        if checkpoint and (
            checkpoint["repository_registration_id"] != project.repository_registration_id
            or (task.admission_base_commit and checkpoint["base_commit"] != task.admission_base_commit)
        ):
            raise CandidateStateUnavailable("Execution checkpoint identity no longer matches the task.")
        if checkpoint and ((checkpoint["candidate_commit"] is None) != (checkpoint["completion_json"] is None)):
            raise CandidateStateUnavailable("Execution completion checkpoint is incomplete.")
        if not checkpoint and not task.admission_base_commit and task.attempts > 1 and (task.branch or task.worktree_path):
            raise CandidateStateUnavailable("Older interrupted task has no verifiable execution base checkpoint.")
        workflow_base = self.workflows.execution_base(project, task, checkpoint)
        execution_base = checkpoint["base_commit"] if checkpoint else workflow_base or task.admission_base_commit
        execution_task = replace(task, admission_base_commit=execution_base) if execution_base else task
        branch, worktree = prepare_worktree(project, execution_task, self.config.workspace_root)
        # A task admitted from an exact commit is compared against that commit and nothing else,
        # on every attempt: `prepare_worktree` has already proven this checkout descends from it,
        # and the durable value cannot drift as the worktree accumulates repair commits. Without
        # one, the branch snapshot taken before any worker runs remains the comparison point --
        # repairs and later controller attempts reuse the worktree, but terminal evidence must
        # retain this task base rather than the most recent model phase.
        base_commit = checkpoint["base_commit"] if checkpoint else task.admission_base_commit or current_commit(worktree)
        self.db.establish_execution_base(task.id, project.repository_registration_id, base_commit)
        self.db.update_task(task.id, branch=branch, worktree_path=worktree)
        task.branch = branch
        task.worktree_path = worktree
        commands = flatten_unique([*project.validation_commands, *task.validation_commands])
        if checkpoint and checkpoint["completion_json"] is not None:
            try:
                final = json.loads(checkpoint["completion_json"])
                candidate = explicit_candidate_commit(project.repo_path, worktree, base_commit)
                if (
                    candidate != checkpoint["candidate_commit"]
                    or checkpoint["specification_digest"] != _specification_digest(project, task, commands)
                    or final["commit"] != candidate or final["base_commit"] != base_commit
                    or final["status"] != "completed"
                    or checkpoint["publication_required"] != int(project.auto_push)
                ):
                    raise ValueError("Completion checkpoint no longer matches the verified candidate.")
            except (ValueError, KeyError, TypeError) as exc:
                raise CandidateStateUnavailable("Completion checkpoint is unusable.") from exc
            return self._complete_task(task, project, worktree, artifact_dir, final)
        # Prompts share the controller's immutable comparison base without changing admission provenance.
        prompt_task = replace(task, execution_base_commit=base_commit)

        patterns = self.knowledge.relevant(project.id, f"{task.title} {task.objective} {' '.join(task.tags)}")
        phase, initial_repair_cycle, failure = "implementation", 0, task.last_error if task.attempts > 1 else None
        if continuation:
            from .blockers import validate_continuation

            document = validate_continuation(self.config, self.db, task, project, continuation)
            phase, initial_repair_cycle = f"validation-repair-{document['repair_slot']}", document["repair_slot"]
            failure = (
                f"Operator resolved the recorded execution blocker: {document['reason']}\n"
                f"Immutable supporting receipt: {document['receipt_path']}\n"
                f"Receipt SHA-256: {document['receipt_sha256']}\n"
                "Inspect that evidence; preserve all admitted requirements and prior evidence. "
                "The controller retains independent review and the postintegration phase gate."
            )
        result = self._run_codex_phase(
            task,
            project,
            artifact_dir,
            phase=phase,
            model=self.config.implementation_model if continuation else self._implementation_model(task),
            effort="high" if continuation else self._implementation_effort(task),
            prompt=implementation_prompt(
                project,
                prompt_task,
                worktree,
                patterns,
                previous_failure=failure,
            ),
            schema="task-result.schema.json",
            role=ROLE_IMPLEMENTER,
        )
        if self._handle_noncompletion(task, result.final):
            return _noncompletion_outcome(result.final)

        validation_results = self._validate(task, worktree, artifact_dir, commands)
        repair_cycle = initial_repair_cycle
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
                prompt=implementation_prompt(project, prompt_task, worktree, patterns, previous_failure=failure),
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
        resolution = self.runner.resolve_sandbox(self.config.review_sandbox, cwd=worktree)
        if resolution.sandbox is None:
            return self._fail_review_infrastructure(
                task, artifact_dir, sandbox_infrastructure_block(resolution)
            )
        try:
            review = self._run_codex_phase(
                task,
                project,
                artifact_dir,
                phase="review",
                model=self._review_model(task),
                effort=self._review_effort(task),
                prompt=review_prompt(
                    project, prompt_task, worktree, validation_results=validation_results,
                    max_output_chars=self.config.max_output_chars,
                ),
                schema="review-result.schema.json",
                sandbox=resolution.sandbox,
                role=ROLE_REVIEWER,
            )
        except ReviewTransportError as error:
            return self._retry_or_fail_review_transport(task, artifact_dir, resolution, error)
        blocked = detect_review_infrastructure_block(
            review, requested_sandbox=resolution.requested, sandbox=resolution.sandbox
        )
        if blocked is not None:
            return self._fail_review_infrastructure(task, artifact_dir, blocked)
        review_final, unusable = self._trusted_review_final(task, artifact_dir, resolution, review)
        if review_final is None:
            return unusable

        review_cycle = 0
        repair_failure = None
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
                prompt=implementation_prompt(
                    project, prompt_task, worktree, patterns, previous_failure=repair_failure, review_findings=findings,
                ),
                schema="task-result.schema.json",
                role=ROLE_IMPLEMENTER,
            )
            if self._handle_noncompletion(task, repair.final):
                return _noncompletion_outcome(repair.final)
            result = repair
            validation_results = self._validate(task, worktree, artifact_dir, commands, suffix=f"review-repair-{review_cycle}")
            if not self._all_passed(validation_results):
                repair_failure = validation_failure_summary(validation_results)
                continue
            repair_failure = None
            try:
                review = self._run_codex_phase(
                    task,
                    project,
                    artifact_dir,
                    phase=f"review-{review_cycle + 1}",
                    model=self._review_model(task),
                    effort=self._review_effort(task),
                    prompt=review_prompt(
                        project, prompt_task, worktree, previous_findings=findings,
                        validation_results=validation_results, max_output_chars=self.config.max_output_chars,
                    ),
                    schema="review-result.schema.json",
                    sandbox=resolution.sandbox,
                    role=ROLE_REVIEWER,
                )
            except ReviewTransportError as error:
                return self._retry_or_fail_review_transport(task, artifact_dir, resolution, error)
            blocked = detect_review_infrastructure_block(
                review, requested_sandbox=resolution.requested, sandbox=resolution.sandbox
            )
            if blocked is not None:
                return self._fail_review_infrastructure(task, artifact_dir, blocked)
            review_final, unusable = self._trusted_review_final(task, artifact_dir, resolution, review)
            if review_final is None:
                return unusable

        if repair_failure is not None:
            self._retry_or_fail(task, repair_failure)
            return OUTCOME_VALIDATION_FAILED
        if review_final.get("verdict") != "pass":
            self._retry_or_fail(task, json.dumps(review_final, ensure_ascii=False))
            return OUTCOME_REVIEW_CHANGES_REQUIRED

        validation_results = self._validate(task, worktree, artifact_dir, commands, suffix="final")
        if not self._all_passed(validation_results):
            self._retry_or_fail(task, validation_failure_summary(validation_results))
            return OUTCOME_VALIDATION_FAILED

        with self.telemetry.phase_span(phase_name=PHASE_EVIDENCE, role=ROLE_CONTROLLER) as span:
            patch_path = artifact_dir / "changes.patch"
            if project.auto_commit:
                from .prerequisites import verify_locked

                verify_locked(self.config, self.db, task)
                commit_task(worktree, task)
                verify_locked(self.config, self.db, task, committed=True)
            candidate_commit = (
                explicit_candidate_commit(project.repo_path, worktree, base_commit)
                if project.auto_commit or task.admission_base_commit else None
            )
            # One base and one candidate feed every terminal field below, so the patch, the
            # changed-file set, the recorded candidate and anything derived from them cannot
            # disagree about which delta this task produced.
            write_patch(worktree, patch_path, base_commit, candidate_commit)
            final_changed_files = changed_files_between(worktree, base_commit, candidate_commit)
            final_result = {
                **result.final,
                "status": "completed",
                # Phase results retain the model's incremental claim. Terminal evidence is instead
                # the complete Git truth integration will compare: approved base -> final candidate.
                "changed_files": final_changed_files,
                # The exact commit the diff, the changed files and the candidate are all measured
                # from, so an integrator never has to re-derive it or guess which base was used.
                "base_commit": base_commit,
                "admission_base_commit": task.admission_base_commit,
                "controller_validation": [self._validation_to_dict(item) for item in validation_results],
                "review": review_final,
                # Retained so a completed task records which sandbox the independent review actually
                # ran under, including when the requested one could not launch a command here.
                "review_sandbox": resolution.sandbox,
                "review_sandbox_requested": resolution.requested,
                "branch": branch,
                "commit": candidate_commit,
                "worktree": str(worktree),
                "patch": str(patch_path),
                "completed_at": utc_now(),
            }
            from .prerequisites import terminal_evidence

            attribution = terminal_evidence(self.config, self.db, task, artifact_dir, base_commit, candidate_commit, final_changed_files)
            if attribution is not None:
                final_result["prerequisite_handoff"] = attribution
            if candidate_commit is not None:
                self.db.save_completion_checkpoint(
                    task.id, candidate_commit, final_result, _specification_digest(project, task, commands), project.auto_push,
                )
            span.set_outcome(OUTCOME_OK)

        return self._complete_task(task, project, worktree, artifact_dir, final_result)

    def _complete_task(self, task: Task, project: Project, worktree: Path, artifact_dir: Path, final: dict) -> str:
        """Finish verified local work; publication retries never invoke a model."""
        from .prerequisites import verify_locked

        verify_locked(self.config, self.db, task, committed=True)
        candidate = final["commit"]
        if project.auto_push:
            if candidate is None:
                raise CandidateStateUnavailable("Publication requires a verified candidate commit.")
            push_candidate(worktree, task.branch, candidate)
        self.workflows.integrate(project, task, worktree, final)
        patch = artifact_dir / "changes.patch"
        if str(patch) != final["patch"]:
            write_patch(worktree, patch, final["base_commit"], candidate)
        final_result = {**final, "patch": str(patch), "worktree": str(worktree), "completed_at": utc_now()}
        (artifact_dir / "task-result.json").write_text(
            json.dumps(final_result, indent=2, ensure_ascii=False), encoding="utf-8",
        )

        with self.telemetry.phase_span(phase_name=PHASE_COMPLETION, role=ROLE_CONTROLLER) as span:
            self.knowledge.add_patterns(task.id, project.id, final_result.get("reusable_patterns") or [])
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
            from .prerequisites import prompt_context, verify_locked

            verify_locked(self.config, self.db, task)
            prompt += prompt_context(self.config, self.db, task)
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
            verify_locked(self.config, self.db, task)
            if (phase == "implementation" and result.exit_code == 0 and isinstance(result.final, dict)
                    and result.final.get("status") == "blocked" and isinstance(self.db, ForgeDB)):
                from .blockers import record_blocked_candidate

                record_blocked_candidate(self.config, self.db, task, project, run_id, phase_dir)
            if span.recording:
                span.set_many(phase_usage_attributes(result.usage))
            self._enforce_usage_budget(task)
            if result.exit_code != 0:
                # Recorded before raising so the span carries the specific cause; the raised text
                # itself is never attached, because it embeds a stderr tail.
                span.set_error_code(ERROR_CODEX_EXIT_NONZERO)
                message = (
                    f"Codex phase {phase} failed with exit {result.exit_code}: "
                    f"{tail_text(result.stderr_path.read_text(encoding='utf-8', errors='replace'))}"
                )
                if role == ROLE_REVIEWER:
                    # A reviewer whose request the transport refused has said nothing about the
                    # candidate. The typed raise lets the review call sites keep that fact; every
                    # other phase keeps the generic failure it always had.
                    raise ReviewTransportError(
                        message,
                        phase=phase,
                        exit_code=result.exit_code,
                        http_status=transport_http_status(result),
                        evidence={
                            "events": str(result.jsonl_path),
                            "final": str(result.final_path),
                            "stderr": str(result.stderr_path),
                        },
                    )
                raise RuntimeError(message)
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
            from .prerequisites import verify_locked

            verify_locked(self.config, self.db, task)
            results = run_validations(
                commands,
                cwd=worktree,
                artifact_dir=artifact_dir / f"validation-{suffix}",
                timeout_seconds=self.config.task_timeout_minutes * 60,
                max_summary_chars=self.config.max_output_chars,
            )
            verify_locked(self.config, self.db, task)
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

    def _retry_or_fail_review_transport(
        self,
        task: Task,
        artifact_dir: Path,
        resolution: SandboxResolution,
        error: ReviewTransportError,
    ) -> str:
        """Bounded retry for a review the transport could not execute, classified as infrastructure.

        The provider refused or failed the review request itself — an invalid structured-output
        schema rejected before any model answer, an API error, a transport-level CLI failure — so
        nothing was said about the candidate. Unlike a sandbox that can never launch a command on
        this host, a transport fault can be transient, so the ordinary attempt and identical-failure
        bounds still apply. What never changes is the owning failure class: every stored error for
        this path carries the `REVIEW_INFRASTRUCTURE_BLOCKED` prefix, including the terminal one
        loop detection writes, so no amount of retrying reads an environment fault as a finding
        about the candidate's code. No repair phase follows and the task is never completed on this
        path: there is no semantic review verdict to repair from or to complete against.

        Classification is autonomous and uses controller facts only. Durable state — the block
        record, `last_error`, and the identical-failure fingerprint — is built solely from what
        the controller itself observed: the review phase, the `review_transport_failed` reason,
        the process exit code, and the numeric HTTP status when the provider's error event carried
        one. No provider-generated string (error code, type, param, message, request ID)
        participates anywhere durable: there is no allowlist to certify, no code to classify, and
        nothing for an operator to maintain when the provider invents a new failure. A rejection
        Forge has never seen — a rate limit, an authentication refusal, a malformed response, an
        outage, an unknown error code — lands in the same bounded retry class as every other
        rejection with the same exit code and status, automatically. The provider's verbatim
        output stays in the raw event/stderr artifacts the runner retained, which the block's
        evidence map points at and which remain the only owners of provider text.
        """
        # `phase` is the constant review phase, not `error.phase`: a first review and a re-review
        # refused the same way are the same failure class, and the fingerprint must say so. The
        # identity slots are fixed-arity, with absent facts rendered as None, so a failure with no
        # status can never collide with one whose status happens to format the same way.
        identity = [
            "phase=review",
            f"reason={BLOCK_REVIEW_TRANSPORT_FAILED}",
            f"exit_code={error.exit_code}",
            f"status={error.http_status}",
        ]
        summary = ["review transport failed"]
        if error.exit_code is not None:
            summary.append(f"exit_code={error.exit_code}")
        if error.http_status is not None:
            summary.append(f"status={error.http_status}")
        stable_summary = "; ".join(summary)
        block = ReviewInfrastructureBlock(
            reason=BLOCK_REVIEW_TRANSPORT_FAILED,
            requested_sandbox=resolution.requested,
            sandbox=resolution.sandbox,
            exit_codes=(error.exit_code,) if error.exit_code is not None else (),
            commands=(),
            detail=(
                f"{stable_summary}. The provider's verbatim output is retained in the attempt's "
                "review event log and stderr log."
            ),
            evidence=dict(error.evidence),
            http_status=error.http_status,
        )
        (artifact_dir / REVIEW_INFRASTRUCTURE_BLOCK_FILENAME).write_text(
            json.dumps(block.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._retry_or_fail(
            task,
            stable_summary,
            classification=REVIEW_INFRASTRUCTURE_BLOCKED,
            fingerprint_source="|".join(identity),
        )
        return OUTCOME_REVIEW_INFRASTRUCTURE_BLOCKED

    def _review_result_schema(self) -> dict:
        """The review-result schema document, loaded once from the same file the transport ships."""
        schema = getattr(self, "_review_result_schema_document", None)
        if schema is None:
            schema = load_schema("review-result.schema.json")
            self._review_result_schema_document = schema
        return schema

    def _trusted_review_final(
        self, task: Task, artifact_dir: Path, resolution: SandboxResolution, review: CodexRunResult
    ) -> tuple[dict | None, str | None]:
        """The only door from a completed review run into review semantics.

        A semantic review exists if and only if `decode_review_result` produced one from the
        run's raw final-output bytes; the runner's in-memory `final` — which fabricates a
        blocked-shaped dict from raw text when the file is missing or malformed — is deliberately
        never consulted for a review phase. On success the conformance-enforced review is
        returned. On failure the attempt is classified through the unusable-output
        infrastructure path and the terminal outcome comes back instead: no semantic
        blocking_reason, no finding, no repair, no candidate-quality conclusion.

        Valid bytes are necessary but not sufficient. A decoded result is admitted only if the
        run's own event stream also certifies that every command the review launched recorded an
        outcome: a reviewer that started the full gate, never observed it and answered anyway
        asserted its verdict on evidence that does not exist, and the same is true whichever way
        the verdict fell — an unobserved gate impeaches a `pass` and a `changes_required` alike,
        so this runs before conformance and before any repair decision. Decoding is checked
        first, so every failure class the controller already names keeps its existing kind and
        its existing failure identity; closure only reaches the results the controller would
        otherwise have trusted.
        """
        decoded, kind = decode_review_result(review, self._review_result_schema())
        if decoded is None:
            return None, self._retry_or_fail_review_output_unusable(
                task, artifact_dir, resolution, review, kind
            )
        closure = review_command_closure(review)
        if not closure.certified:
            return None, self._retry_or_fail_review_output_unusable(
                task,
                artifact_dir,
                resolution,
                review,
                REVIEW_OUTPUT_COMMANDS_UNCLOSED,
                commands=closure.open_commands,
            )
        return enforce_specification_conformance(task, decoded), None

    def _retry_or_fail_review_output_unusable(
        self,
        task: Task,
        artifact_dir: Path,
        resolution: SandboxResolution,
        review: CodexRunResult,
        kind: str | None,
        commands: tuple[str, ...] = (),
    ) -> str:
        """Bounded retry for a review whose exit-0 output never became a valid ReviewResult.

        The reviewer process completed, but no semantic review exists: the final output was
        missing, empty, malformed, or failed the review-result schema. That is a protocol fault
        of the review infrastructure, owned by `REVIEW_INFRASTRUCTURE_BLOCKED` exactly like a
        transport refusal — nothing was said about the candidate, so no repair phase follows and
        the task is never completed on this path.

        Classification uses controller facts only: the review phase, the
        `review_output_unusable` reason, the controller-named decode kind, and the process exit
        code. No reviewer byte, parser message, path or request ID participates in `last_error`,
        the block record, or the identical-failure fingerprint; the raw final output, event
        stream and stderr artifacts the runner retained stay the only owners of the unusable
        text, and the block's evidence map points at them. Two malformed outputs with different
        volatile content but the same controller-observable failure are therefore the same
        bounded failure class, and loop detection still terminates the retrying.

        `commands` — the launched commands whose outcome an unclosed review never recorded — is
        the one reviewer-influenced input here, and it reaches the block artifact's
        `failed_commands` field and nothing else. It is bounded at its source and stays out of
        `last_error`, the summary and the fingerprint, exactly like every other raw byte.
        """
        summary = f"review output unusable; kind={kind}"
        if review.exit_code is not None:
            summary += f"; exit_code={review.exit_code}"
        # Fixed-arity identity, absent facts rendered as None — the same law as the transport
        # fingerprint, with the reason keeping the two classes disjoint.
        identity = [
            "phase=review",
            f"reason={BLOCK_REVIEW_OUTPUT_UNUSABLE}",
            f"kind={kind}",
            f"exit_code={review.exit_code}",
            "status=None",
        ]
        # Controller-authored and safe for the durable record: an unaccounted command launch and
        # an event stream that cannot be read are different operator-facing facts, and the block
        # artifact says which one refused this review.
        detail_lead = ""
        if kind == REVIEW_OUTPUT_COMMANDS_UNCLOSED:
            detail_lead = (
                "The reviewer's own event stream never recorded an outcome for a command it "
                "launched, so the verdict rests on evidence the run never produced. "
                if commands
                else "The reviewer's event stream could not be read, so no launched command "
                "could be shown to have recorded an outcome. "
            )
        block = ReviewInfrastructureBlock(
            reason=BLOCK_REVIEW_OUTPUT_UNUSABLE,
            requested_sandbox=resolution.requested,
            sandbox=resolution.sandbox,
            exit_codes=(review.exit_code,) if review.exit_code is not None else (),
            commands=commands,
            detail=(
                f"{summary}. {detail_lead}The reviewer's raw final output, event stream and "
                "stderr are retained in the attempt's raw artifacts."
            ),
            evidence={
                "events": str(review.jsonl_path),
                "final": str(review.final_path),
                "stderr": str(review.stderr_path),
            },
            kind=kind,
        )
        (artifact_dir / REVIEW_INFRASTRUCTURE_BLOCK_FILENAME).write_text(
            json.dumps(block.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._retry_or_fail(
            task,
            summary,
            classification=REVIEW_INFRASTRUCTURE_BLOCKED,
            fingerprint_source="|".join(identity),
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

    def _retry_or_fail(
        self,
        task: Task,
        error: str,
        *,
        classification: str | None = None,
        fingerprint_source: str | None = None,
    ) -> None:
        # A caller that owns a canonical stable identity for its failure class passes it as
        # `fingerprint_source`, keeping the human summary and the identical-failure identity
        # separate; every other caller keeps the historical behavior of hashing the normalized
        # error text. Either way the bounds and the loop detection are the same.
        bounded_error = tail_text(error, self.config.max_output_chars)
        normalized = " ".join(bounded_error.split())
        fingerprint = hashlib.sha256(
            (fingerprint_source if fingerprint_source is not None else normalized).encode("utf-8")
        ).hexdigest()
        identical_failures = task.identical_failures + 1 if task.failure_fingerprint == fingerprint else 1
        loop_detected = identical_failures >= self.config.max_identical_failures
        state = (
            TaskState.FAILED
            if loop_detected or task.attempts >= task.max_attempts
            else TaskState.READY
        )
        if loop_detected:
            bounded_error = f"LOOP_DETECTED after {identical_failures} identical failures.\n{bounded_error}"
        if classification:
            # The class stays at the very front of `last_error`: `control_plane._safe_task_error`
            # matches on the prefix, and loop detection may add information but never gets to
            # replace the owning failure class with its own.
            bounded_error = f"{classification}: {bounded_error}"
        self.db.update_task(
            task.id,
            state=state,
            last_error=bounded_error,
            failure_fingerprint=fingerprint,
            identical_failures=identical_failures,
        )

    def _handle_unexpected_failure(self, task: Task, exc: Exception) -> None:
        if isinstance(exc, ProcessSupervisionError):
            self.db.update_task(task.id, state=TaskState.FAILED, last_error="PROCESS_SUPERVISION_FAILED")
            return
        if isinstance(exc, TaskUsageBudgetExceeded):
            self.db.update_task(task.id, state=TaskState.FAILED, last_error=str(exc))
            return
        if isinstance(exc, (BaseCommitUnavailable, WorktreeUnavailable)):
            self._fail_repository_unavailable(task, exc)
            return
        if isinstance(exc, CandidateStateUnavailable):
            self._fail_candidate_state_unavailable(task, exc)
            return
        self._retry_or_fail(task, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

    def _fail_repository_unavailable(self, task: Task, exc: GitError) -> None:
        """End the attempt on a repository fault without inventing an implementation failure.

        A task admitted from an exact commit cannot run when its repository no longer supplies
        that commit, or when the checkout it would reuse no longer descends from it. Nothing was
        implemented, nothing was reviewed and nothing was validated, so no repair cycle follows
        and no reviewer conclusion exists to record. This deliberately does not go through
        `_retry_or_fail`: that would advance the identical-failure fingerprint and counter, which
        are the durable record of repeated *implementation* failure, and would return the task to
        READY to burn another attempt resolving the same absent commit. The failure class stays
        at the front of `last_error`, where the control plane reads it, exactly as the review
        infrastructure block does.
        """
        self.db.update_task(
            task.id,
            state=TaskState.FAILED,
            last_error=f"{REPOSITORY_UNAVAILABLE}: "
            + tail_text(str(exc), self.config.max_output_chars),
        )

    def _fail_candidate_state_unavailable(
        self, task: Task, exc: CandidateStateUnavailable
    ) -> None:
        """Fail without a repair loop when no commit completely represents the final checkout."""
        self.db.update_task(
            task.id,
            state=TaskState.FAILED,
            last_error=f"{CANDIDATE_STATE_UNAVAILABLE}: "
            + tail_text(str(exc), self.config.max_output_chars),
        )

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


def _contract_item_findings(
    items: list[str] | tuple[str, ...], entries: list, *, text_key: str, label: str
) -> list[dict]:
    """Findings unless the report is an exact bijection of the contract, met with evidence.

    The report must be a one-to-one representation of the expected contract: every stored item
    reported exactly once, verbatim, and nothing else reported at all. Matching is verbatim:
    task intake already normalized the stored contract text, so review conformance compares the
    reviewer's echo to that exact string — no Unicode normalization, whitespace collapsing, or
    paraphrase acceptance happens here, and an echo that differs in any way is not coverage.
    A duplicate is itself invalid review evidence, whether the copies agree or contradict each
    other, so entries are never collapsed into a map where a later duplicate could hide an
    earlier contradiction. An entry whose text is not a contract item is rejected, never
    ignored: a paraphrased or contradictory extra version of an item must surface as a finding
    even when the verbatim entry it shadows is reported met with green-test evidence. Only for
    an exact entry do met/evidence apply: met must be true and evidence non-empty.
    """
    heading = label[:1].upper() + label[1:]
    findings: list[dict] = []
    expected = list(items)
    expected_set = set(expected)
    if len(entries) != len(expected):
        findings.append(
            {
                "severity": "high",
                "file": None,
                "line": None,
                "issue": (
                    f"{heading} report is not one-to-one with the contract: "
                    f"{len(entries)} report entries for {len(expected)} contract items."
                ),
                "required_fix": (
                    f"Report every {label} exactly once, echoed exactly as written, "
                    "and report nothing else."
                ),
            }
        )
    for entry in entries:
        text = entry.get(text_key) if isinstance(entry, dict) else None
        if isinstance(text, str) and text in expected_set:
            continue
        reported = text if isinstance(text, str) else repr(entry)
        contradicts = isinstance(entry, dict) and entry.get("met") is not True
        findings.append(
            {
                "severity": "critical" if contradicts else "high",
                "file": None,
                "line": None,
                "issue": f"{heading} report does not match any contract item: {reported}",
                "required_fix": (
                    f"Report only the task's stored {label} texts, each echoed exactly as "
                    "written; a paraphrase, reformatting, or additional version of an item "
                    "is not part of the contract and cannot be reviewed in its place."
                ),
            }
        )
    for item in expected:
        matches = [
            entry for entry in entries if isinstance(entry, dict) and entry.get(text_key) == item
        ]
        if not matches:
            findings.append(
                {
                    "severity": "high",
                    "file": None,
                    "line": None,
                    "issue": f"{heading} not verified by review: {item}",
                    "required_fix": (
                        f"Verify the candidate against this {label} exactly as "
                        "written and repair any nonconformance."
                    ),
                }
            )
        elif len(matches) > 1:
            findings.append(
                {
                    "severity": "high",
                    "file": None,
                    "line": None,
                    "issue": f"{heading} reported {len(matches)} times by review: {item}",
                    "required_fix": (
                        f"Report this {label} exactly once; duplicate reports are invalid "
                        "review evidence even when they agree."
                    ),
                }
            )
        elif matches[0].get("met") is not True:
            issue = f"Unmet {label}: {item}"
            evidence = matches[0].get("evidence")
            if isinstance(evidence, str) and evidence.strip():
                issue = f"{issue} Reviewer evidence: {evidence.strip()}"
            findings.append(
                {
                    "severity": "critical",
                    "file": None,
                    "line": None,
                    "issue": issue,
                    "required_fix": (
                        f"Change the candidate to satisfy this {label} exactly as "
                        "written, including any candidate-authored tests that encode a "
                        "different contract."
                    ),
                }
            )
        else:
            evidence = matches[0].get("evidence")
            if not (isinstance(evidence, str) and evidence.strip()):
                findings.append(
                    {
                        "severity": "high",
                        "file": None,
                        "line": None,
                        "issue": f"{heading} reported met without evidence: {item}",
                        "required_fix": (
                            f"Verify the candidate against this {label} with concrete evidence "
                            "and repair any nonconformance."
                        ),
                    }
                )
    return findings


def _specification_digest(project: Project, task: Task, commands: list[str]) -> str:
    """Bind completion to the exact specification, validation commands, and publication policy."""
    data = {
        "project_id": project.id, "registration": project.repository_registration_id,
        "title": task.title, "objective": task.objective, "acceptance": task.acceptance,
        "architecture_decisions": task.architecture_decisions, "constraints": task.constraints,
        "scope": task.scope, "out_of_scope": task.out_of_scope, "commands": commands,
        "auto_commit": project.auto_commit, "auto_push": project.auto_push,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def acceptance_conformance_findings(task: Task, review_final: dict) -> list[dict]:
    """Findings for acceptance criteria the review did not confirm the candidate meets.

    The task's acceptance criteria are the contract the operator approved. Tests are evidence
    for the specification; tests do not redefine the specification. The report must be an exact
    bijection of the stored criteria: a criterion reported unmet, reported without evidence,
    reported more than once, not reported against at all, or accompanied by any entry that is
    not itself a stored criterion — verbatim, exactly as stored — must block completion even
    when every candidate-authored test passes and the reviewer's overall verdict says `pass`.
    """
    return _contract_item_findings(
        task.acceptance,
        review_final.get("acceptance_criteria") or [],
        text_key="criterion",
        label="acceptance criterion",
    )


def binding_conformance_findings(task: Task, review_final: dict) -> list[dict]:
    """Findings for binding requirements the review did not confirm the candidate meets.

    Architecture decisions and constraints are binding implementation requirements — the same
    contract authority as acceptance criteria, stated as requirements on how the work is done
    rather than on what must be demonstrably true. The review reports the itemized union of both
    in `binding_requirements` as an exact bijection of the stored texts; a requirement reported
    unmet, reported without evidence, reported more than once, not reported against at all, or
    accompanied by any entry that is not itself a stored requirement — including a paraphrase or
    reformatting that no longer matches the stored text byte for byte — blocks completion even
    when every candidate-authored test passes and the reviewer's overall verdict says `pass`.
    """
    return _contract_item_findings(
        task.binding_requirements,
        review_final.get("binding_requirements") or [],
        text_key="text",
        label="binding requirement",
    )


def enforce_specification_conformance(task: Task, review_final: dict) -> dict:
    """Refuse a review outcome that leaves any part of the task contract unmet or unexamined.

    This is the single conformance choke point: acceptance criteria and binding requirements
    (architecture decisions and constraints) are enforced here, in that order, and nowhere else.
    The returned dict is the effective review result the controller acts on. The effective
    verdict is decided only after every conformance finding from both report kinds is known:
    if any exists, the verdict is `changes_required`, and no property of the report — a met
    flag, candidate-test evidence, or the reviewer's own verdict — is consulted again to
    restore `pass`. Only a report that is an exact met-with-evidence bijection of the whole
    contract passes the reviewer's result through untouched; otherwise each conformance finding
    is appended, so repair receives the exact contract text instead of only the candidate's own
    test truth.
    """
    unconfirmed = [
        *acceptance_conformance_findings(task, review_final),
        *binding_conformance_findings(task, review_final),
    ]
    if not unconfirmed:
        return review_final
    return {
        **review_final,
        "verdict": "changes_required",
        "findings": [*(review_final.get("findings") or []), *unconfirmed],
    }


def _noncompletion_outcome(final: dict) -> str:
    """Name the bounded outcome for a Codex result that `_handle_noncompletion` already acted on.

    This reads the same `status` field that method reads and makes no decision of its own; the
    state written to the database is whatever `_handle_noncompletion` wrote.
    """
    return OUTCOME_NEEDS_DECISION if final.get("status") == "needs_decision" else OUTCOME_NONCOMPLETION
