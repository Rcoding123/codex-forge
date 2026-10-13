"""Opt-in, serial project integration and controller-owned phase gates.

Git and SQLite cannot commit together. A saved pending update is the only authority
for accepting a ref already at its candidate after a controller interruption.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .config import ForgeConfig
from .control_plane import redact_command
from .db import ACTIVE_TASK_STATES, ForgeDB
from .evidence import safe_path_component
from .gitops import (
    CandidateStateUnavailable, _git, explicit_candidate_commit,
    push_candidate, require_admission_base_commit, verify_repository,
)
from .models import Project, Task, TaskState
from .prompts import validation_failure_summary
from .utils import ensure_dir, flatten_unique, json_dumps, utc_now
from .validation import run_validations


def eligible_task(document: dict, task_id: str) -> bool:
    """Used inside the authoritative claim transaction; malformed state releases nothing."""
    try:
        return document["state"] == "RUNNING" and current_task(document) == task_id
    except (KeyError, IndexError, TypeError):
        return False


def current_task(document: dict) -> str:
    return document["phases"][document["phase_index"]]["tasks"][document["task_index"]]


def _require_workflow_identity(document: dict) -> None:
    identity = document.get("id") if isinstance(document, dict) else None
    if not isinstance(identity, str) or not re.fullmatch(r"workflow-[0-9a-f]{32}", identity):
        raise CandidateStateUnavailable("Project workflow identity is invalid.")


def _digest(project: Project, task: Task) -> str:
    # One definition of completion identity, shared with the task controller.
    from .orchestrator import _specification_digest
    return _specification_digest(project, task, flatten_unique([
        *project.validation_commands, *task.validation_commands,
    ]))


class ProjectWorkflows:
    def __init__(self, config: ForgeConfig, db: ForgeDB):
        self.config, self.db = config, db

    def get(self, project_id: str) -> dict | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT document_json FROM project_workflows WHERE project_id=?", (project_id,),
            ).fetchone()
        if not row:
            return None
        document = json.loads(row[0])
        _require_workflow_identity(document)
        return document

    @staticmethod
    def _save(connection, project_id: str, document: dict) -> None:
        cursor = connection.execute(
            "UPDATE project_workflows SET document_json=?,updated_at=? WHERE project_id=? "
            "AND json_extract(document_json,'$.id')=? AND EXISTS "
            "(SELECT 1 FROM projects WHERE id=? AND repository_registration_id=?)",
            (json_dumps(document), utc_now(), project_id, document["id"], project_id, document["registration"]),
        )
        if cursor.rowcount != 1:
            raise CandidateStateUnavailable("Project plan identity changed during its state transition.")

    def _artifacts(self, project: Project, document: dict) -> Path:
        # Project evidence has its own namespace, outside task evidence disclosure.
        _require_workflow_identity(document)
        return ensure_dir(self.config.data_root / "project-evidence" / document["id"])

    def install(self, project: Project, definition: dict, *, extend: bool = False) -> dict:
        """Caller holds the controller lock; registration also serializes against claims/deletion."""
        if not isinstance(definition, dict) or set(definition) != {"branch", "base_commit", "phases"}:
            raise ValueError("Plan requires exactly branch, base_commit, and phases.")
        branch, base, phases = (definition[key] for key in ("branch", "base_commit", "phases"))
        if not isinstance(branch, str) or not re.fullmatch(r"forge-project/[a-z0-9][a-z0-9-]{0,63}", branch):
            raise ValueError("Integration branch must be forge-project/<lowercase name>, using letters, digits, and '-'.")
        if not safe_path_component(branch.split("/")[1]):
            raise ValueError("Integration branch name is not a safe Windows path component.")
        if branch.casefold() == project.default_branch.casefold():
            raise ValueError("Integration branch must differ from the default branch.")
        verify_repository(project.repo_path)
        _git(project.repo_path, "check-ref-format", f"refs/heads/{branch}")
        if not isinstance(base, str):
            raise ValueError("base_commit must be a canonical commit object id.")
        require_admission_base_commit(project.repo_path, base)
        if not isinstance(phases, list) or not phases:
            raise ValueError("Plan requires at least one phase.")
        normalized, ids, names = [], [], set()
        for phase in phases:
            if not isinstance(phase, dict) or set(phase) != {"name", "tasks", "acceptance_commands"}:
                raise ValueError("Each phase requires name, tasks, and acceptance_commands.")
            name, tasks, commands = (phase[key] for key in ("name", "tasks", "acceptance_commands"))
            if not isinstance(name, str) or not name.strip() or name in names or len(name) > 100:
                raise ValueError("Phase names must be unique nonempty strings of at most 100 characters.")
            names.add(name)
            for values in (tasks, commands):
                if not isinstance(values, list) or not values or any(
                    not isinstance(value, str) or not value.strip() for value in values
                ):
                    raise ValueError("Phase tasks and acceptance commands must be nonempty string lists.")
            if any(task_id in ids for task_id in tasks) or len(set(tasks)) != len(tasks):
                raise ValueError("A task may appear in only one plan position.")
            ids.extend(tasks)
            normalized.append({"name": name, "tasks": tasks, "acceptance_commands": commands})
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM projects WHERE id=?", (project.id,)).fetchone()
            if not row or row["repository_registration_id"] != project.repository_registration_id:
                raise ValueError("Repository registration changed.")
            project = self.db._project_from_row(row)
            verify_repository(project.repo_path)
            require_admission_base_commit(project.repo_path, base)
            if not project.auto_commit:
                raise ValueError("Project plans require automatic commits.")
            prior_row = connection.execute(
                "SELECT document_json FROM project_workflows WHERE project_id=?", (project.id,),
            ).fetchone()
            prior = json.loads(prior_row[0]) if prior_row else None
            if prior is not None:
                _require_workflow_identity(prior)
            if extend:
                if not prior or prior["state"] != "COMPLETED":
                    raise ValueError("Only a completed project plan can be extended.")
                if branch != prior["branch"] or base != prior["head_commit"] or self._prove_ref(project, prior) != base:
                    raise ValueError("Extension must use the existing branch and its proven completed tip.")
            elif prior:
                raise ValueError("Project already has an immutable plan; use project extend after completion.")
            rows = connection.execute("SELECT * FROM tasks WHERE project_id=?", (project.id,)).fetchall()
            if any(row["state"] in ACTIVE_TASK_STATES for row in rows):
                raise ValueError("Cannot install a plan while project tasks are active.")
            tasks_by_id = {row["id"]: self.db._task_from_row(row) for row in rows}
            specifications = {}
            for position, task_id in enumerate(ids):
                task = tasks_by_id.get(task_id)
                if not task or task.attempts or task.branch or task.worktree_path or task.admission_base_commit:
                    raise ValueError("Planned tasks must be unstarted tasks of this project without an admission base.")
                if task.state not in (TaskState.READY, TaskState.BACKLOG):
                    raise ValueError("Planned tasks must be READY or BACKLOG.")
                if any(dep in ids[position:] for dep in task.dependencies):
                    raise ValueError("Plan dependencies must precede their dependent task.")
                for dependency in task.dependencies:
                    dependency_row = connection.execute(
                        "SELECT project_id,state FROM tasks WHERE id=?", (dependency,),
                    ).fetchone()
                    if not dependency_row or (
                        dependency_row["project_id"] == project.id and dependency not in ids[:position]
                        and dependency_row["state"] != TaskState.COMPLETED
                    ):
                        raise ValueError("Plan dependencies must be declared earlier or belong to runnable external work.")
                specifications[task_id] = _digest(project, task)
            document = {
                "id": f"workflow-{uuid.uuid4().hex}", "registration": project.repository_registration_id,
                "branch": branch, "base_commit": base, "head_commit": base, "phases": normalized,
                "specifications": specifications, "phase_index": 0, "task_index": 0,
                "state": "RUNNING", "pending": None, "integrations": [], "gates": [], "last_error": "",
                "previous_workflow_id": prior["id"] if prior else None,
            }
            # A branch left by a crash before DB commit is never silently adopted.
            if prior is None:
                exists = _git(project.repo_path, "show-ref", "--quiet", "--verify", f"refs/heads/{branch}", check=False)
                if exists.returncode != 1:
                    raise ValueError("Integration branch already exists or its absence cannot be proven.")
                _git(project.repo_path, "update-ref", f"refs/heads/{branch}", base, "0" * len(base))
            else:
                archive = self._artifacts(project, prior) / "completed-plan.json"
                snapshot = json.dumps(prior, indent=2)
                if archive.exists():
                    if archive.read_text(encoding="utf-8") != snapshot:
                        raise ValueError("Completed plan archive differs from its current checkpoint.")
                else:
                    archive.write_text(snapshot, encoding="utf-8")
                connection.execute("DELETE FROM project_workflows WHERE project_id=?", (project.id,))
            connection.execute(
                "INSERT INTO project_workflows VALUES(?,?,?)", (project.id, json_dumps(document), utc_now()),
            )
            (self._artifacts(project, document) / "plan.json").write_text(
                json.dumps(document, indent=2), encoding="utf-8",
            )
        return document

    @staticmethod
    def _prove_ref(project: Project, document: dict) -> str:
        _require_workflow_identity(document)
        if project.repository_registration_id != document["registration"] or not project.auto_commit:
            raise CandidateStateUnavailable("Project plan registration or commit policy changed.")
        branch = document["branch"]
        if not branch.startswith("forge-project/") or branch.casefold() == project.default_branch.casefold():
            raise CandidateStateUnavailable("Project integration branch is no longer dedicated.")
        _git(project.repo_path, "check-ref-format", f"refs/heads/{branch}")
        inventory = _git(project.repo_path, "worktree", "list", "--porcelain").stdout
        if f"branch refs/heads/{branch}" in inventory.splitlines():
            raise CandidateStateUnavailable("Project integration branch is checked out.")
        tip = _git(project.repo_path, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").stdout.strip()
        require_admission_base_commit(project.repo_path, document["base_commit"])
        return tip

    def execution_base(self, project: Project, task: Task, checkpoint: dict | None) -> str | None:
        document = self.get(project.id)
        if document is None:
            return None
        if not eligible_task(document, task.id):
            raise CandidateStateUnavailable("Task is not eligible in its project plan.")
        tip = self._prove_ref(project, document)
        pending = document["pending"]
        base = pending["base_commit"] if pending else document["head_commit"]
        allowed_tips = {base, pending["candidate_commit"]} if pending else {base}
        if tip not in allowed_tips or (pending and pending["task_id"] != task.id):
            raise CandidateStateUnavailable("Project branch differs from its integration checkpoint.")
        if _digest(project, task) != document["specifications"][task.id]:
            raise CandidateStateUnavailable("Planned task specification changed.")
        if (task.admission_base_commit and task.admission_base_commit != base) or (
            checkpoint and checkpoint["base_commit"] != base
        ):
            raise CandidateStateUnavailable("Task base differs from the integrated project tip.")
        return base

    def integrate(self, project: Project, task: Task, worktree: Path, final: dict) -> None:
        document = self.get(project.id)
        if document is None:
            return
        base = self.execution_base(project, task, self.db.execution_checkpoint(task.id))
        candidate = explicit_candidate_commit(project.repo_path, worktree, base)
        checkpoint = self.db.execution_checkpoint(task.id)
        if not checkpoint or (
            candidate != final["commit"] or base != final["base_commit"]
            or checkpoint["candidate_commit"] != candidate
            or checkpoint["specification_digest"] != document["specifications"][task.id]
            or checkpoint["repository_registration_id"] != document["registration"]
        ):
            raise CandidateStateUnavailable("Integration requires the exact verified completion checkpoint.")
        pending = {"task_id": task.id, "base_commit": base, "candidate_commit": candidate}
        if document["pending"] is not None and document["pending"] != pending:
            raise CandidateStateUnavailable("Pending integration candidate changed.")
        document["pending"] = pending
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._save(connection, project.id, document)
        # Durable intent precedes the non-transactional Git update.
        tip = self._prove_ref(project, document)
        if tip == base:
            _git(project.repo_path, "update-ref", f"refs/heads/{document['branch']}", candidate, base)
        elif tip != candidate:
            raise CandidateStateUnavailable("Project branch moved during integration.")
        if self._prove_ref(project, document) != candidate:
            raise CandidateStateUnavailable("Project branch does not contain the verified candidate.")
        if project.auto_push:
            push_candidate(worktree, document["branch"], candidate)
        document["head_commit"] = candidate
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._save(connection, project.id, document)
        path = self._artifacts(project, document) / f"integration-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps(pending, indent=2), encoding="utf-8")

    def advance_all(self) -> int:
        progressed = 0
        with self.db.connect() as connection:
            projects = [row[0] for row in connection.execute("SELECT project_id FROM project_workflows")]
        for project_id in projects:
            document = self.get(project_id)
            if document is None:
                continue
            if document["state"] in ("COMPLETED", "BLOCKED"):
                continue
            project = self.db.get_project(project_id)
            try:
                progressed += int(self._advance(project, document))
            except Exception:
                # Raw diagnostics remain local; public state is controller-owned.
                import traceback
                raw = self._artifacts(project, document) / f"failure-{uuid.uuid4().hex}.log"
                raw.write_text(traceback.format_exc(), encoding="utf-8")
                latest = self.get(project_id)
                if latest is None or latest["id"] != document["id"]:
                    continue  # A governed deletion/re-registration cannot be overwritten.
                document = latest
                document.update(state="BLOCKED", last_error="PROJECT_PLAN_CHECK_FAILED")
                for gate in document["gates"]:
                    if gate["status"] == "RUNNING":
                        gate["status"] = "FAILED"
                with self.db.connect() as connection:
                    try:
                        self._save(connection, project_id, document)
                    except CandidateStateUnavailable:
                        continue  # The generation disappeared or changed after the re-read.
        return progressed

    def _advance(self, project: Project, document: dict) -> bool:
        tip = self._prove_ref(project, document)
        pending = document["pending"]
        allowed = {document["head_commit"]}
        if pending:
            allowed.update([pending["base_commit"], pending["candidate_commit"]])
        if tip not in allowed:
            raise CandidateStateUnavailable("Project branch moved outside the integration journal.")
        phase = document["phases"][document["phase_index"]]
        if document["state"] == "RUNNING":
            task = self.db.get_task(current_task(document))
            if task is None:
                raise CandidateStateUnavailable("Planned task was deleted.")
            if task.state in (TaskState.FAILED, TaskState.BLOCKED, TaskState.NEEDS_DECISION):
                document.update(state="BLOCKED", last_error="PROJECT_TASK_BLOCKED")
            elif task.state == TaskState.COMPLETED:
                if not pending or pending["task_id"] != task.id or tip != pending["candidate_commit"]:
                    raise CandidateStateUnavailable("Completed task has no proven project integration.")
                if task.result.get("commit") != tip or task.result.get("base_commit") != pending["base_commit"]:
                    raise CandidateStateUnavailable("Completed task no longer matches its integration.")
                document["integrations"].append(pending)
                document["pending"] = None
                document["head_commit"] = tip
                document["task_index"] += 1
                if document["task_index"] == len(phase["tasks"]):
                    document["state"] = "GATING"
            else:
                return False
            with self.db.connect() as connection:
                self._save(connection, project.id, document)
            return True
        # A restart never trusts an unfinished gate; each execution gets its own checkout/logs.
        gate_id = uuid.uuid4().hex
        for previous in document["gates"]:
            if previous["status"] == "RUNNING":
                previous["status"] = "INTERRUPTED"
        gate = {"id": gate_id, "phase": phase["name"], "commit": tip,
                "commands": phase["acceptance_commands"], "status": "RUNNING"}
        document["gates"].append(gate)
        with self.db.connect() as connection:
            self._save(connection, project.id, document)
        logs = ensure_dir(self._artifacts(project, document) / f"gate-{gate_id}")
        worktree = self.config.data_root / "gate-worktrees" / document["id"] / gate_id
        ensure_dir(worktree.parent)
        _git(project.repo_path, "worktree", "add", "--detach", str(worktree), tip)
        explicit_candidate_commit(project.repo_path, worktree, tip)
        results = run_validations(
            gate["commands"], cwd=worktree, artifact_dir=logs,
            timeout_seconds=self.config.task_timeout_minutes * 60,
            max_summary_chars=self.config.max_output_chars,
        )
        from .orchestrator import Orchestrator
        gate["validation"] = [Orchestrator._validation_to_dict(result) for result in results]
        clean_candidate = explicit_candidate_commit(project.repo_path, worktree, tip)
        if clean_candidate != tip or self._prove_ref(project, document) != tip:
            raise CandidateStateUnavailable("Phase gate modified its checked commit or project branch.")
        passed = bool(results) and all(result.passed for result in results)
        gate["status"] = "PASSED" if passed else "FAILED"
        if not passed:
            (logs / "summary.txt").write_text(validation_failure_summary(results), encoding="utf-8")
            document.update(state="BLOCKED", last_error="PROJECT_ACCEPTANCE_FAILED")
        else:
            document["phase_index"] += 1
            document["task_index"] = 0
            document["state"] = "COMPLETED" if document["phase_index"] == len(document["phases"]) else "RUNNING"
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM projects WHERE id=?", (project.id,)).fetchone()
            if not row or row["repo_path"] != str(project.repo_path):
                raise CandidateStateUnavailable("Project repository changed during its gate.")
            if passed:
                proofs = {item["task_id"]: item for item in document["integrations"]}
                for task_id in phase["tasks"]:
                    proof = proofs.get(task_id)
                    if not proof:
                        raise CandidateStateUnavailable("Phase is missing a completed integration proof.")
                    task_row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                    # Deletion retains the sealed integration proof; a live row must still agree.
                    if task_row is not None:
                        live_task = self.db._task_from_row(task_row)
                        if live_task.state != TaskState.COMPLETED or (
                            live_task.result.get("commit") != proof["candidate_commit"]
                            or live_task.result.get("base_commit") != proof["base_commit"]
                            or _digest(project, live_task) != document["specifications"][task_id]
                        ):
                            raise CandidateStateUnavailable("Phase task changed during acceptance.")
            (logs / "result.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")
            self._save(connection, project.id, document)
        return True

    def resume(self, project_id: str) -> dict:
        document = self.get(project_id)
        if not document or document["state"] != "BLOCKED":
            raise ValueError("Only a blocked project plan can resume.")
        phase = document["phases"][document["phase_index"]]
        document["state"] = "GATING" if document["task_index"] == len(phase["tasks"]) else "RUNNING"
        document["last_error"] = ""
        with self.db.connect() as connection:
            self._save(connection, project_id, document)
        return document

    @staticmethod
    def public(document: dict | None) -> dict | None:
        if document is None:
            return None
        return {key: document[key] for key in (
            "id", "branch", "base_commit", "head_commit", "state", "phase_index", "task_index", "last_error",
            "previous_workflow_id",
        )} | {"phases": [{**phase, "acceptance_commands": [redact_command(command)
                            for command in phase["acceptance_commands"]]} for phase in document["phases"]],
             "integrations": document["integrations"],
             "gates": [{key: gate[key] for key in ("id", "phase", "commit", "status")} for gate in document["gates"]]}
