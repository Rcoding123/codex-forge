"""Explicit, same-attempt continuation of a closed model-reported blocker.

The operator resolves an execution blocker, not the task specification or its budgets.
One existing validation-repair slot is reserved; failed historical artifacts never change.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from .db import ACTIVE_TASK_STATES, ForgeDB
from .gitops import (
    CandidateStateUnavailable, _git, checkout_is_owned_by, current_commit,
    ensure_disposable_checkout, require_admission_base_commit, require_base_lineage,
)
from .models import TaskState
from .runner import _instance_conforms
from .schema_resources import load_schema
from .task_submission import task_fingerprint
from .utils import derived_worktree_path, flatten_unique, utc_now
from .workflows import ProjectWorkflows, current_task, _digest


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plain_bytes(path: Path, *, maximum: int = 1_048_576) -> bytes:
    return _plain_document(path, maximum=maximum)[0]


def _stamp(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode)


def _plain_document(path: Path, *, maximum: int = 1_048_576) -> tuple[bytes, tuple]:
    """Read a bounded plain file; links/reparse traversal never supply proof bytes."""
    for part in (path, *path.parents):
        info = os.lstat(part)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Blocker proof cannot traverse a link or reparse point.")
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError("Blocker proof must be a bounded plain file.")
    data = path.read_bytes()
    after = os.lstat(path)
    if _stamp(info) != _stamp(after) or len(data) > maximum:
        raise ValueError("Blocker proof changed during reading.")
    return data, _stamp(after)


def candidate_snapshot(config, project, task, base: str) -> str:
    """Bind HEAD, branch, index, and tracked/nonignored-untracked working bytes."""
    worktree = task.worktree_path
    expected = derived_worktree_path(config.workspace_root, project.name, task.id)
    if worktree is None or worktree.resolve() != expected.resolve():
        raise CandidateStateUnavailable("Blocker checkout is not the task's owned workspace.")
    ensure_disposable_checkout(worktree, config.workspace_root / project.name, [project.repo_path])
    if not checkout_is_owned_by(project.repo_path, worktree):
        raise CandidateStateUnavailable("Blocker checkout is not owned by the registered repository.")
    require_admission_base_commit(project.repo_path, base)
    require_base_lineage(worktree, base, "HEAD")
    head = current_commit(worktree)
    branch = _git(worktree, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if branch != task.branch or _git(project.repo_path, "rev-parse", f"refs/heads/{branch}").stdout.strip() != head:
        raise CandidateStateUnavailable("Blocker checkout branch identity changed.")
    index = _git(worktree, "ls-files", "--stage", "-z").stdout
    status = _git(worktree, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    names = _git(worktree, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    files = []
    stamps = []
    for name in sorted(set(names.split("\0")) - {""}):
        path = worktree / name
        if path.resolve().is_relative_to(worktree.resolve()) is False:
            raise CandidateStateUnavailable("Blocker candidate path escaped its checkout.")
        if os.path.lexists(path):
            data, identity = _plain_document(path, maximum=256 * 1024 * 1024)
            files.append((name, _hash(data)))
            stamps.append((path, identity))
        else:
            files.append((name, None))
            stamps.append((path, None))
    for path, identity in stamps:
        current = _stamp(os.lstat(path)) if os.path.lexists(path) else None
        if current != identity:
            raise CandidateStateUnavailable("Blocker file changed during the complete candidate scan.")
    if (current_commit(worktree) != head or _git(worktree, "ls-files", "--stage", "-z").stdout != index
            or _git(worktree, "status", "--porcelain", "-z", "--untracked-files=all").stdout != status
            or _git(worktree, "symbolic-ref", "--short", "HEAD").stdout.strip() != branch
            or _git(worktree, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout != names):
        raise CandidateStateUnavailable("Blocker candidate changed during proof.")
    return _hash(json.dumps([head, branch, index, status, files], ensure_ascii=False).encode("utf-8"))


def queue_resolution(config, task_id: str, reason: str, receipt: Path, expected_sha256: str) -> dict:
    """Queue only the unused first validation-repair slot; never change attempt/admission truth."""
    reason = reason.strip()
    if not reason or len(reason) > 4_000:
        raise ValueError("A bounded operator reason is required.")
    receipt = receipt.absolute()
    raw = _plain_bytes(receipt)
    digest = _hash(raw)
    if digest != expected_sha256:
        raise ValueError("Operator receipt does not match the supplied SHA-256.")
    db = ForgeDB(config.database_path)
    db.initialize()
    task = db.get_task(task_id)
    if task is None:
        raise ValueError("Task was not found.")
    existing = db.blocker_continuation(task.id, task.attempts)
    if existing:
        document = json.loads(existing["document_json"])
        if document["reason"] != reason or document["receipt_sha256"] != digest or document["receipt_path"] != str(receipt):
            raise ValueError("An existing continuation is bound to a different resolution.")
        return _public(existing, document)
    if (task.state != TaskState.FAILED or task.attempts < 1 or task.attempts != task.max_attempts
            or config.max_repair_cycles < 1):
        raise ValueError("Only failed model-blocked attempts with an unused repair slot can continue.")
    project = db.get_project(task.project_id)
    checkpoint = db.execution_checkpoint(task.id)
    if not project or not checkpoint or checkpoint["completion_json"] is not None:
        raise ValueError("A nonterminal owned execution base is required.")
    if checkpoint["repository_registration_id"] != project.repository_registration_id:
        raise ValueError("Blocker repository generation changed.")
    if task.admission_base_commit and checkpoint["base_commit"] != task.admission_base_commit:
        raise ValueError("Blocker admission base changed.")
    _prove_plan(config, db, project, task, checkpoint)
    with db.connect() as connection:
        runs = connection.execute("SELECT * FROM runs WHERE task_id=? ORDER BY id", (task.id,)).fetchall()
    attempt_root = config.evidence_root / project.name / task.id / f"attempt-{task.attempts:02d}"
    if not runs:
        raise ValueError("No closed implementation run exists.")
    latest = runs[-1]
    if (latest["phase"] != "implementation" or latest["exit_code"] != 0 or not latest["completed_at"]
            or Path(latest["artifact_dir"]).absolute() != (attempt_root / "implementation").absolute()):
        raise ValueError("Latest run is not this attempt's closed original implementation.")
    if any(Path(row["artifact_dir"]).absolute().is_relative_to(attempt_root.absolute())
           and row["phase"] != "implementation" for row in runs):
        raise ValueError("The attempt has already progressed beyond its original implementation.")
    if (sum(Path(row["artifact_dir"]).absolute().is_relative_to(attempt_root.absolute()) for row in runs) != 1
            or {path.name for path in attempt_root.iterdir()} != {"implementation"}):
        raise ValueError("The failed attempt has ambiguous or already consumed phase evidence.")
    output = attempt_root / "implementation" / "implementation.final.json"
    final_bytes = _plain_bytes(output)
    final = json.loads(final_bytes)
    if not _instance_conforms(final, load_schema("task-result.schema.json")) or final["status"] != "blocked":
        raise ValueError("Only a schema-valid model-reported blocker can be resolved.")
    snapshot = candidate_snapshot(config, project, task, checkpoint["base_commit"])
    proof = json.loads(raw)
    if (not isinstance(proof, dict) or proof.get("task_id") != task.id or proof.get("attempt") != task.attempts
            or proof.get("candidate_base") != checkpoint["base_commit"]
            or proof.get("candidate_snapshot_sha256") != snapshot
            or proof.get("implementation_sha256") != _hash(final_bytes)):
        raise ValueError("Operator receipt does not bind this exact failed output and complete candidate.")
    historical_snapshot = output.parent / "blocked-candidate.json"
    if historical_snapshot.exists():
        recorded = json.loads(_plain_bytes(historical_snapshot))
        if recorded != {"task_id": task.id, "attempt": task.attempts, "source_run_id": latest["id"],
                        "implementation_sha256": _hash(final_bytes), "candidate_snapshot_sha256": snapshot,
                        "base_commit": checkpoint["base_commit"], "task_fingerprint": task_fingerprint(task)}:
            raise ValueError("Candidate no longer matches its controller-sealed failed snapshot.")
    elif not isinstance(proof.get("legacy_attestation"), str) or not proof["legacy_attestation"].strip():
        raise ValueError("Legacy evidence lacks a full failed-tree seal; an explicit supervisor attestation is required.")
    else:
        _verify_legacy_proof(project, task, checkpoint["base_commit"], proof)
    identity = uuid.uuid4().hex
    artifact_root = config.evidence_root / project.name / task.id / f"attempt-{task.attempts:02d}-c-{identity[:8]}"
    document = {
        "id": identity, "task_id": task.id, "attempt": task.attempts, "repair_slot": 1,
        "reason": reason, "receipt_path": str(receipt), "receipt_sha256": digest,
        "artifact_root": str(artifact_root), "implementation_path": str(output),
        "implementation_sha256": _hash(final_bytes), "source_run_id": latest["id"],
        "task_fingerprint": task_fingerprint(task), "base_commit": checkpoint["base_commit"],
        "repository_registration_id": project.repository_registration_id, "candidate_snapshot": snapshot,
        "worktree": str(task.worktree_path), "branch": task.branch,
        "max_repair_cycles": config.max_repair_cycles, "project_digest": _digest(project, task),
    }
    # Re-prove under the insertion lock. No READY task can be observed without sealed proof.
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone()
        active = connection.execute(
            "SELECT id FROM tasks WHERE state IN (?,?,?,?) LIMIT 1", tuple(state.value for state in ACTIVE_TASK_STATES),
        ).fetchone()
        newest = connection.execute("SELECT id FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
        if (active or not current or current["state"] != TaskState.FAILED.value or current["attempts"] != task.attempts
                or task_fingerprint(db._task_from_row(current)) != document["task_fingerprint"]
                or not newest or newest[0] != latest["id"]):
            raise ValueError("Blocker task changed during resolution.")
        if candidate_snapshot(config, project, task, checkpoint["base_commit"]) != snapshot or _hash(_plain_bytes(receipt)) != digest:
            raise ValueError("Blocker candidate or receipt changed during resolution.")
        artifact_root.mkdir(parents=True, exist_ok=False)
        (artifact_root / "operator-receipt.raw").write_bytes(raw)
        (artifact_root / "resolution.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
        connection.execute(
            "INSERT INTO blocker_continuations(task_id,attempt,state,document_json,updated_at) VALUES(?,?,?,?,?)",
            (task.id, task.attempts, "QUEUED", json.dumps(document), utc_now()),
        )
        connection.execute("UPDATE tasks SET state=?,updated_at=? WHERE id=?", (TaskState.BLOCKED.value, utc_now(), task.id))
    return _public({"state": "QUEUED"}, document)


def _public(row, document) -> dict:
    return {"task_id": document["task_id"], "attempt": document["attempt"],
            "continuation_id": document["id"], "repair_slot": document["repair_slot"], "state": row["state"]}


def validate_continuation(config, db, task, project, row, *, resume_completion: bool = False) -> dict:
    """Recheck before the reserved slot executes; queued authority cannot become stale."""
    document = json.loads(row["document_json"])
    checkpoint = db.execution_checkpoint(task.id)
    if (not checkpoint or checkpoint["base_commit"] != document["base_commit"]
            or project.repository_registration_id != document["repository_registration_id"]
            or task_fingerprint(task) != document["task_fingerprint"]
            or str(task.worktree_path) != document["worktree"] or task.branch != document["branch"]
            or config.max_repair_cycles != document["max_repair_cycles"]
            or _digest(project, task) != document["project_digest"]):
        raise CandidateStateUnavailable("Blocker continuation authority changed.")
    root = Path(document["artifact_root"])
    if (_hash(_plain_bytes(Path(document["receipt_path"]))) != document["receipt_sha256"]
            or _hash(_plain_bytes(root / "operator-receipt.raw")) != document["receipt_sha256"]
            or json.loads(_plain_bytes(root / "resolution.json")) != document
            or _hash(_plain_bytes(Path(document["implementation_path"]))) != document["implementation_sha256"]):
        raise CandidateStateUnavailable("Blocker continuation receipt or historical output changed.")
    if resume_completion:
        if row["state"] != "CONSUMED" or checkpoint["completion_json"] is None:
            raise CandidateStateUnavailable("No sealed completion can resume its consumed repair slot.")
    elif row["state"] != "CLAIMED" or candidate_snapshot(config, project, task, document["base_commit"]) != document["candidate_snapshot"]:
        raise CandidateStateUnavailable("Blocker continuation candidate changed or its slot was already consumed.")
    return document


def _prove_plan(config, db, project, task, checkpoint):
    workflows = ProjectWorkflows(config, db)
    plan = workflows.get(project.id)
    if plan is None:
        return
    pending = plan["pending"]
    allowed_tips = {checkpoint["base_commit"]}
    if pending is not None:
        if (checkpoint["completion_json"] is None or pending["task_id"] != task.id
                or pending["base_commit"] != checkpoint["base_commit"]
                or pending["candidate_commit"] != checkpoint["candidate_commit"]):
            raise CandidateStateUnavailable("Blocker has no matching sealed integration intent.")
        allowed_tips.add(pending["candidate_commit"])
    if (plan["state"] not in ("RUNNING", "BLOCKED") or current_task(plan) != task.id
            or (plan["state"] == "BLOCKED" and plan["last_error"] != "PROJECT_TASK_BLOCKED")
            or workflows._prove_ref(project, plan) not in allowed_tips
            or plan["head_commit"] not in allowed_tips
            or plan["specifications"][task.id] != _digest(project, task)):
        raise CandidateStateUnavailable("Blocker no longer owns the frozen workflow tip.")


def _verify_legacy_proof(project, task, base: str, proof: dict) -> None:
    """A narrow evidence-only legacy attestation is new repair input, never a historical seal."""
    legacy = proof.get("legacy_proof")
    if not isinstance(legacy, dict):
        raise ValueError("Legacy continuation requires exact application and retained validation proof.")
    allowed = legacy.get("allowed_change_root")
    if (not isinstance(allowed, str) or not allowed.startswith("docs/evidence/") or ".." in Path(allowed).parts
            or Path(allowed).is_absolute() or "\\" in allowed
            or allowed not in {line.strip().rstrip("*").rstrip("/") for line in task.scope.splitlines()}):
        raise ValueError("Legacy continuation must be evidence-only within its admitted scope.")
    worktree = task.worktree_path
    if current_commit(worktree) != base:
        raise ValueError("Legacy continuation cannot infer a prior committed candidate.")
    changes = _git(worktree, "diff", "--name-only", "-z", base, "--").stdout.split("\0")
    changes += _git(worktree, "ls-files", "-z", "--others", "--exclude-standard").stdout.split("\0")
    if any(name and not name.startswith(allowed + "/") for name in changes):
        raise ValueError("Legacy candidate contains application changes outside its evidence-only attestation.")

    def retained(key):
        item = legacy.get(key)
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("Legacy retained evidence identity is missing.")
        path = Path(item["path"])
        if not path.is_absolute() or not path.resolve().is_relative_to((worktree / allowed).resolve()):
            raise ValueError("Legacy evidence is outside the admitted candidate evidence root.")
        raw = _plain_bytes(path)
        if _hash(raw) != item.get("sha256"):
            raise ValueError("Legacy retained evidence changed.")
        return json.loads(raw), item["sha256"], path

    entries, manifest_hash, _ = retained("historical_input_manifest")
    record, _, record_path = retained("historical_validation_record")
    inputs = legacy.get("application_inputs")
    if not isinstance(inputs, dict) or not inputs or not isinstance(entries, list):
        raise ValueError("Legacy application input manifest is missing.")
    mapping = {item["path"]: item["sha256"] for item in entries if isinstance(item, dict) and "path" in item and "sha256" in item}
    if mapping != inputs or len(mapping) != len(entries):
        raise ValueError("Legacy application input manifests disagree or are ambiguous.")
    tracked = set(_git(worktree, "ls-files", "-z", "--cached").stdout.split("\0"))
    for name, digest in inputs.items():
        if (name not in tracked or name.startswith(allowed + "/")
                or not (worktree / name).resolve().is_relative_to(worktree.resolve())
                or _hash(_plain_bytes(worktree / name, maximum=256 * 1024 * 1024)) != digest):
            raise ValueError("Legacy application input changed or is not an immutable baseline input.")
    commands = flatten_unique([*project.validation_commands, *task.validation_commands])
    results = record.get("results") if isinstance(record, dict) else None
    if (not results or not isinstance(results, list) or record.get("stage") != "candidate"
            or record.get("comparison_base") != base or record.get("head") != base
            or record.get("branch") != task.branch or record.get("integration_ref_at_start") != base
            or record.get("source_manifest_sha256") != manifest_hash
            or [item.get("command") for item in results] != commands
            or any(type(item.get("exit_code")) is not int or item["exit_code"] != 0 or item.get("status") != "passed" for item in results)):
        raise ValueError("Legacy candidate validation does not match its admitted checks, source manifest, or base.")
    for item in results:
        log = item.get("log")
        if not isinstance(log, str) or Path(log).name != log:
            raise ValueError("Legacy validation log identity is invalid.")
        _plain_bytes(record_path.parent / log, maximum=256 * 1024 * 1024)


def record_blocked_candidate(config, db, task, project, run_id: int, phase_dir: Path) -> None:
    """Future blocked attempts retain a controller seal before their FAILED transition."""
    checkpoint = db.execution_checkpoint(task.id)
    output = phase_dir / "implementation.final.json"
    raw = _plain_bytes(output)
    final = json.loads(raw)
    if not _instance_conforms(final, load_schema("task-result.schema.json")) or final["status"] != "blocked":
        return
    record = {"task_id": task.id, "attempt": task.attempts, "source_run_id": run_id,
              "implementation_sha256": _hash(raw), "candidate_snapshot_sha256": candidate_snapshot(config, project, task, checkpoint["base_commit"]),
              "base_commit": checkpoint["base_commit"], "task_fingerprint": task_fingerprint(task)}
    with (phase_dir / "blocked-candidate.json").open("x", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
