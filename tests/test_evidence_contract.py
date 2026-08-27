"""Tests for the read-only `task.evidence.*` control-plane contract.

Every test builds an isolated temporary configuration, database, and evidence root. Nothing here
touches a real Forge installation, a production database, or production evidence.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from codex_forge import evidence
from codex_forge.cli import main
from codex_forge.config import ForgeConfig, load_config
from codex_forge.control_plane import ERROR_EXIT_CODES, error_exit_code, redact_command
from codex_forge.db import ForgeDB
from codex_forge.evidence import (
    MAX_ARTIFACT_BYTES,
    MAX_READABLE_BYTES,
    EvidenceError,
    compute_artifact_id,
    evidence_list,
    evidence_show,
    redact_text,
)
from codex_forge.models import Project, Task, TaskState


PROJECT_NAME = "démo-工具"
TASK_ID = "TASK-évidence-01"


def _path(path: Path) -> str:
    return path.as_posix()


def _configured(tmp_path: Path, *, initialize: bool = True) -> tuple[ForgeConfig, ForgeDB]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        f"""
[paths]
workspace_root = '{_path(tmp_path / "worktrees")}'
data_root = '{_path(tmp_path / "data")}'
repository_root = '{_path(tmp_path / "repositories")}'
inbox_root = '{_path(tmp_path / "inbox")}'
database_path = '{_path(tmp_path / "data" / "forge.db")}'
evidence_root = '{_path(tmp_path / "evidence")}'
knowledge_root = '{_path(tmp_path / "knowledge")}'

[codex]
approval_policy = 'never'
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    db = ForgeDB(config.database_path)
    if initialize:
        db.initialize()
    return config, db


def _seed(db: ForgeDB, tmp_path: Path, *, state: TaskState = TaskState.COMPLETED) -> Task:
    project = Project(
        id="project-evidence",
        name=PROJECT_NAME,
        repo_path=tmp_path / "repositories" / PROJECT_NAME,
        default_branch="main",
        validation_commands=["python -m pytest"],
    )
    db.add_project(project)
    task = Task(
        id=TASK_ID,
        project_id=project.id,
        title="Evidence contract",
        objective="Expose evidence safely.",
        acceptance=[],
        validation_commands=[],
        state=state,
        max_attempts=3,
    )
    db.add_task(task)
    return task


def _task_root(config: ForgeConfig) -> Path:
    return config.evidence_root / PROJECT_NAME / TASK_ID


def _write(path: Path, content: str) -> Path:
    """Write byte-exact UTF-8 so tests assert on exact sizes and newlines on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    return path


FINAL_RESULT = {
    "status": "completed",
    "summary": "Implemented the export.",
    "changed_files": ["src/app.py"],
    "validation_performed": ["python -m pytest"],
    "assumptions": [],
    "blocking_reason": None,
    "decisions_needed": [],
    "reusable_patterns": [{"trigger": "export", "lesson": "buffer writes", "evidence": "tests"}],
    "controller_validation": [
        {
            "command": "python -m pytest",
            "exit_code": 0,
            "passed": True,
            "duration_seconds": 1.5,
            "stdout": r"C:\CodexData\evidence\p\t\attempt-00\validation-final\validation-01.stdout.log",
            "stderr": r"C:\CodexData\evidence\p\t\attempt-00\validation-final\validation-01.stderr.log",
        }
    ],
    "review": {"verdict": "pass", "summary": "No blocking issues.", "findings": []},
    "branch": "forge/task-1",
    "commit": "abc123",
    "worktree": r"C:\CodexData\worktrees\p\t",
    "patch": r"C:\CodexData\evidence\p\t\attempt-00\changes.patch",
    "completed_at": "2026-08-04T10:00:00+00:00",
}

REVIEW_RESULT = {
    "verdict": "changes_required",
    "summary": "One issue remains.",
    "findings": [
        {
            "severity": "high",
            "file": "src/app.py",
            "line": 42,
            "issue": "Unbounded read.",
            "required_fix": "Bound the read.",
        }
    ],
}

IMPLEMENTATION_RESULT = {
    "status": "completed",
    "summary": "First pass.",
    "changed_files": ["src/app.py"],
    "validation_performed": [],
    "assumptions": ["assumed UTF-8"],
    "blocking_reason": None,
    "decisions_needed": [],
    "reusable_patterns": [],
}


def _build_evidence(config: ForgeConfig) -> Path:
    """Create a realistic two-attempt evidence tree that mirrors the controller's real layout."""
    root = _task_root(config)

    first = root / "attempt-00"
    _write(first / "implementation" / "implementation.final.json", json.dumps(IMPLEMENTATION_RESULT))
    _write(first / "implementation" / "implementation.prompt.txt", "raw prompt with secrets")
    _write(first / "implementation" / "implementation.events.jsonl", '{"type":"thread.started"}\n')
    _write(first / "implementation" / "implementation.stderr.log", "model stderr noise")
    _write(first / "validation-initial" / "validation-01.stdout.log", "1 failed\n")
    _write(first / "validation-initial" / "validation-01.stderr.log", "AssertionError\n")

    second = root / "attempt-01"
    _write(second / "implementation" / "implementation.final.json", json.dumps(IMPLEMENTATION_RESULT))
    _write(second / "implementation" / "implementation.prompt.txt", "raw prompt")
    _write(second / "validation-initial" / "validation-01.stdout.log", "1 failed\n")
    # A repair phase and the validation that follows it deliberately share one directory.
    _write(
        second / "validation-repair-1" / "validation-repair-1.final.json",
        json.dumps(IMPLEMENTATION_RESULT),
    )
    _write(second / "validation-repair-1" / "validation-repair-1.prompt.txt", "repair prompt")
    _write(second / "validation-repair-1" / "validation-01.stdout.log", "2 passed\n")
    _write(second / "review" / "review.final.json", json.dumps(REVIEW_RESULT))
    _write(second / "review" / "review.events.jsonl", '{"type":"turn.completed"}\n')
    _write(second / "review" / "review.prompt.txt", "review prompt")
    _write(second / "validation-final" / "validation-01.stdout.log", "2 passed\n")
    _write(second / "task-result.json", json.dumps(FINAL_RESULT))
    _write(second / "changes.patch", "--- a/src/app.py\n+++ b/src/app.py\n@@\n+ok\n")
    return root


# ---------------------------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------------------------


def _run(config: ForgeConfig, capsys, *arguments: str) -> tuple[int, str, str]:
    exit_code = main(["--config", str(config.config_path), *arguments])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _run_json(config: ForgeConfig, capsys, *arguments: str) -> tuple[int, dict, str]:
    exit_code, out, err = _run(config, capsys, *arguments)
    return exit_code, _single_json_document(out), err


def _single_json_document(stdout: str) -> dict:
    decoder = json.JSONDecoder()
    document, index = decoder.raw_decode(stdout.lstrip())
    assert stdout.lstrip()[index:].strip() == "", "stdout carried trailing output"
    assert isinstance(document, dict)
    return document


def _assert_envelope(document: dict, command: str, *, success: bool) -> None:
    assert document["schema_version"] == "1.0"
    assert document["command"] == command
    assert document["success"] is success
    if success:
        assert document["error"] is None
        assert isinstance(document["data"], dict)
    else:
        assert document["data"] is None
        assert isinstance(document["error"], dict)
        assert isinstance(document["error"]["message"], str) and document["error"]["message"]
        assert isinstance(document["error"]["details"], dict)


def _kinds(payload: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for artifact in payload["artifacts"]:
        grouped.setdefault(artifact["artifact_kind"], []).append(artifact)
    return grouped


def _artifact_of_kind(payload: dict, kind: str) -> dict:
    matches = _kinds(payload).get(kind) or []
    assert matches, f"no artifact of kind {kind}"
    return matches[0]


# ---------------------------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------------------------


def test_task_without_evidence_returns_empty_manifest(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, payload, stderr = _run_json(config, capsys, "task", "evidence", "list", TASK_ID, "--json")

    assert exit_code == 0
    assert stderr == ""
    _assert_envelope(payload, "task.evidence.list", success=True)
    data = payload["data"]
    assert data["evidence_available"] is False
    assert data["artifacts"] == []
    assert data["artifact_count"] == 0
    assert data["task_id"] == TASK_ID
    assert data["project"] == PROJECT_NAME
    assert data["task_registered"] is True
    assert data["task_state"] == "COMPLETED"
    assert data["availability"] == {
        "final_result": False,
        "validation_result": False,
        "review_result": False,
        "patch": False,
        "repair_summary": False,
        "implementation_result": False,
    }


def test_unknown_task_reports_task_not_found(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, payload, stderr = _run_json(
        config, capsys, "task", "evidence", "list", "TASK-missing", "--json"
    )

    assert exit_code == 2
    assert "TASK_NOT_FOUND" in stderr
    _assert_envelope(payload, "task.evidence.list", success=False)
    assert payload["error"]["code"] == "TASK_NOT_FOUND"


def test_manifest_covers_multiple_attempts_and_phases(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)

    exit_code, payload, _ = _run_json(config, capsys, "task", "evidence", "list", TASK_ID, "--json")
    data = payload["data"]

    assert exit_code == 0
    assert data["evidence_available"] is True
    assert data["attempts_with_evidence"] == [0, 1]
    names = [artifact["display_name"] for artifact in data["artifacts"]]
    assert names == [
        "attempt-00/implementation/implementation.final.json",
        "attempt-00/validation-initial/validation-01.stderr.log",
        "attempt-00/validation-initial/validation-01.stdout.log",
        "attempt-01/changes.patch",
        "attempt-01/implementation/implementation.final.json",
        "attempt-01/review/review.final.json",
        "attempt-01/task-result.json",
        "attempt-01/validation-final/validation-01.stdout.log",
        "attempt-01/validation-initial/validation-01.stdout.log",
        "attempt-01/validation-repair-1/validation-01.stdout.log",
        "attempt-01/validation-repair-1/validation-repair-1.final.json",
    ]
    assert data["availability"] == {
        "final_result": True,
        "validation_result": True,
        "review_result": True,
        "patch": True,
        "repair_summary": True,
        "implementation_result": True,
    }
    for artifact in data["artifacts"]:
        assert set(artifact) == {
            "artifact_id",
            "artifact_kind",
            "display_name",
            "media_type",
            "attempt",
            "phase",
            "original_size_bytes",
            "sha256",
            "modified_at",
            "content_available",
            "truncated_when_read",
            "redaction_required",
            "structured",
        }
        assert artifact["redaction_required"] is True
        assert artifact["content_available"] is True
        assert artifact["truncated_when_read"] is False
        assert artifact["original_size_bytes"] > 0
        assert artifact["modified_at"] is not None


def test_manifest_ordering_and_ids_are_deterministic(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)

    first = evidence_list(config, TASK_ID)
    second = evidence_list(config, TASK_ID)

    assert [item["artifact_id"] for item in first["artifacts"]] == [
        item["artifact_id"] for item in second["artifacts"]
    ]
    assert [item["display_name"] for item in first["artifacts"]] == [
        item["display_name"] for item in second["artifacts"]
    ]
    for artifact in first["artifacts"]:
        assert artifact["artifact_id"] == compute_artifact_id(TASK_ID, artifact["display_name"])
        assert artifact["artifact_id"].startswith("ev_")
        assert len(artifact["artifact_id"]) == 35


def test_manifest_hashes_match_the_retrieved_artifact(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)

    manifest = evidence_list(config, TASK_ID)

    for entry in manifest["artifacts"]:
        assert entry["sha256"] is not None and len(entry["sha256"]) == 64
        shown = evidence_show(config, TASK_ID, entry["artifact_id"])
        assert shown["content"]["sha256"] == entry["sha256"]


def test_manifest_reports_binary_content_as_unavailable(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    target = root / "attempt-00" / "changes.patch"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"GIT binary patch\x00\x01\x02")

    entry = evidence_list(config, TASK_ID)["artifacts"][0]

    assert entry["content_available"] is False
    assert entry["sha256"] is not None


def test_artifact_ids_are_opaque_and_task_scoped():
    same = compute_artifact_id("TASK-1", "attempt-00/task-result.json")
    other_task = compute_artifact_id("TASK-2", "attempt-00/task-result.json")
    other_path = compute_artifact_id("TASK-1", "attempt-01/task-result.json")

    assert same != other_task and same != other_path
    assert "task-result" not in same
    assert "attempt" not in same


@pytest.mark.parametrize(
    ("display_name", "kind", "media_type", "structured", "phase"),
    [
        ("attempt-01/task-result.json", "final_result", "application/json", True, None),
        ("attempt-01/changes.patch", "patch", "text/x-diff", False, None),
        ("attempt-01/review/review.final.json", "review_result", "application/json", True, "review"),
        (
            "attempt-01/validation-final/validation-01.stdout.log",
            "validation_log",
            "text/plain",
            False,
            "validation-final",
        ),
        (
            "attempt-01/validation-repair-1/validation-repair-1.final.json",
            "repair_summary",
            "application/json",
            True,
            "validation-repair-1",
        ),
        (
            "attempt-00/implementation/implementation.final.json",
            "implementation_result",
            "application/json",
            True,
            "implementation",
        ),
    ],
)
def test_classification_policy(
    tmp_path: Path, display_name: str, kind: str, media_type: str, structured: bool, phase
):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)

    payload = evidence_list(config, TASK_ID)
    match = next(item for item in payload["artifacts"] if item["display_name"] == display_name)

    assert match["artifact_kind"] == kind
    assert match["media_type"] == media_type
    assert match["structured"] is structured
    assert match["phase"] == phase


def test_excluded_artifacts_never_appear(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _build_evidence(config)
    _write(root / "attempt-01" / "review" / "review.stderr.log", "model stderr")
    _write(root / "attempt-01" / "credentials.env", "API_KEY=abc")
    _write(root / "attempt-01" / "review" / "unexpected.log", "unknown")

    payload = evidence_list(config, TASK_ID)
    names = {item["display_name"] for item in payload["artifacts"]}

    assert not any(name.endswith(".prompt.txt") for name in names)
    assert not any(name.endswith(".events.jsonl") for name in names)
    assert not any(name.endswith("implementation.stderr.log") for name in names)
    assert not any(name.endswith("review.stderr.log") for name in names)
    assert "attempt-01/credentials.env" not in names
    assert "attempt-01/review/unexpected.log" not in names


def test_excluded_artifact_ids_are_not_retrievable(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)
    hidden = compute_artifact_id(TASK_ID, "attempt-01/review/review.prompt.txt")

    exit_code, payload, _ = _run_json(
        config, capsys, "task", "evidence", "show", TASK_ID, "--artifact-id", hidden, "--json"
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "ARTIFACT_NOT_FOUND"


def test_unexpected_nesting_is_ignored(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _build_evidence(config)
    _write(root / "attempt-01" / "review" / "nested" / "task-result.json", "{}")
    _write(root / "stray" / "task-result.json", "{}")

    names = {item["display_name"] for item in evidence_list(config, TASK_ID)["artifacts"]}

    assert "attempt-01/review/nested/task-result.json" not in names
    assert "stray/task-result.json" not in names


# ---------------------------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------------------------


def test_artifact_retrieval_returns_bounded_content(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)
    manifest = evidence_list(config, TASK_ID)
    artifact = _artifact_of_kind(manifest, "patch")

    exit_code, payload, stderr = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        artifact["artifact_id"],
        "--json",
    )

    assert exit_code == 0
    assert stderr == ""
    _assert_envelope(payload, "task.evidence.show", success=True)
    content = payload["data"]["content"]
    assert content["encoding"] == "utf-8"
    assert content["representation"] == "redacted_text"
    assert "+++ b/src/app.py" in content["text"]
    assert content["truncated"] is False
    assert content["complete"] is True
    assert content["returned_size_bytes"] == len(content["text"].encode("utf-8"))
    assert len(content["sha256"]) == 64
    assert payload["data"]["artifact"]["sha256"] == content["sha256"]
    assert payload["data"]["limits"]["max_returned_bytes"] == MAX_ARTIFACT_BYTES
    assert payload["data"]["limits"]["max_readable_bytes"] == MAX_READABLE_BYTES


def test_structured_task_result_is_sanitized(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)
    manifest = evidence_list(config, TASK_ID)
    artifact = _artifact_of_kind(manifest, "final_result")

    payload = evidence_show(config, TASK_ID, artifact["artifact_id"])
    structured = payload["structured_data"]

    assert payload["structured_status"] == "sanitized"
    assert structured["status"] == "completed"
    assert structured["changed_files"] == ["src/app.py"]
    assert structured["branch"] == "forge/task-1"
    assert structured["commit"] == "abc123"
    assert structured["review"]["verdict"] == "pass"
    assert structured["controller_validation"] == [
        {"command": "python -m pytest", "exit_code": 0, "passed": True, "duration_seconds": 1.5}
    ]
    # Internal absolute paths are dropped by the allowlist, not merely masked.
    assert "worktree" not in structured
    assert "patch" not in structured
    for entry in structured["controller_validation"]:
        assert "stdout" not in entry and "stderr" not in entry
    assert "evidence" not in json.dumps(structured).lower() or "CodexData" not in json.dumps(structured)
    assert "CodexData" not in json.dumps(structured)
    # Free-form reusable patterns are allowlisted down to non-path fields.
    assert structured["reusable_patterns"] == [{"trigger": "export", "lesson": "buffer writes"}]
    assert payload["formatted_text"]
    assert "Status: completed" in payload["formatted_text"]
    # The content channel carries the allowlisted projection, never the raw document.
    assert payload["content"]["representation"] == "sanitized_json"
    assert json.loads(payload["content"]["text"]) == structured
    assert "CodexData" not in payload["content"]["text"]
    assert '"worktree"' not in payload["content"]["text"]
    assert '"patch"' not in payload["content"]["text"]
    assert '"stdout"' not in payload["content"]["text"]


def test_structured_content_channel_cannot_bypass_the_allowlist(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(
        root / "attempt-00" / "task-result.json",
        json.dumps({**FINAL_RESULT, "thread_id": "th_leak", "session_id": "sess_leak"}),
    )
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])
    document = json.dumps(payload)

    assert "th_leak" not in document
    assert "sess_leak" not in document
    assert r"C:\\CodexData" not in document
    assert "CodexData" not in document


def test_structured_review_result_is_sanitized(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)
    manifest = evidence_list(config, TASK_ID)
    artifact = _artifact_of_kind(manifest, "review_result")

    payload = evidence_show(config, TASK_ID, artifact["artifact_id"])

    assert payload["structured_status"] == "sanitized"
    assert payload["structured_data"]["verdict"] == "changes_required"
    assert payload["structured_data"]["findings"][0]["severity"] == "high"
    assert "Verdict: changes_required" in payload["formatted_text"]


def test_arbitrary_json_fields_are_not_passed_through(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(
        root / "attempt-00" / "task-result.json",
        json.dumps({**FINAL_RESULT, "thread_id": "th_secret", "raw_prompt": "do the thing"}),
    )
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])

    assert "thread_id" not in payload["structured_data"]
    assert "raw_prompt" not in payload["structured_data"]


def test_malformed_controller_json_does_not_crash(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(root / "attempt-00" / "task-result.json", "{not valid json")
    manifest = evidence_list(config, TASK_ID)

    exit_code, payload, _ = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        manifest["artifacts"][0]["artifact_id"],
        "--json",
    )

    assert exit_code == 0
    _assert_envelope(payload, "task.evidence.show", success=True)
    assert payload["data"]["structured_status"] == "invalid_json"
    assert payload["data"]["structured_data"] is None
    assert payload["data"]["formatted_text"] is None
    # Sanitization did not apply, so the caller is told the content is raw-but-redacted text.
    assert payload["data"]["content"]["representation"] == "redacted_text"
    assert payload["data"]["content"]["text"] == "{not valid json"


def test_unsupported_structured_shape_is_reported(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(root / "attempt-00" / "task-result.json", json.dumps(["not", "an", "object"]))
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])

    assert payload["structured_status"] == "unsupported_shape"
    assert payload["structured_data"] is None


# ---------------------------------------------------------------------------------------------
# Content safety
# ---------------------------------------------------------------------------------------------


SECRET_SAMPLES = [
    ("api_key = sk-abcdefghijklmnopqrstuvwxyz012345", "sk-abcdefghij"),
    ("ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz", "sk-ant-abcdef"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnop"),
    ("authorization=Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNz"),
    ("curl --token abcdefghijklmnop https://example.com", "abcdefghijklmnop"),
    ("password: hunter2hunter2", "hunter2hunter2"),
    ("PASSWORD=hunter2hunter2", "hunter2hunter2"),
    ('{"client_secret": "s3cr3tvaluehere"}', "s3cr3tvaluehere"),
    ("refresh_token => ya29.abcdefghijklmnop", "ya29.abcdefghij"),
    ("git clone https://user:pa55word@example.com/repo.git", "pa55word"),
    ("token gho_abcdefghijklmnopqrstuvwxyz0123", "gho_abcdefghij"),
    ("using github_pat_abcdefghijklmnopqrstuvwxyz0123456789", "github_pat_abcdef"),
    ("slack xoxb-1234567890-abcdefghijkl", "xoxb-1234567890"),
    ("aws AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
    ("google AIzaSyA-abcdefghijklmnopqrstuvwxyz12", "AIzaSyA-abcdefghij"),
    (
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        "eyJzdWIiOiIxMjM0",
    ),
]


@pytest.mark.parametrize(("line", "secret"), SECRET_SAMPLES)
def test_redaction_covers_every_supported_secret_pattern(line: str, secret: str):
    redacted, changed = redact_text(line)

    assert changed is True
    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_redaction_is_idempotent_and_leaves_no_marker_debris():
    line = "Authorization: Bearer abcdefghijklmnopqrst"

    once, _ = redact_text(line)
    twice, changed_again = redact_text(once)

    assert once == "Authorization: [REDACTED]"
    assert twice == once
    assert changed_again is False


def test_redaction_preserves_json_structure_around_a_secret():
    line = '{"authorization": "Bearer abcdefghijklmnop", "status": "ok"}'

    redacted, changed = redact_text(line)

    assert changed is True
    assert "abcdefghijklmnop" not in redacted
    assert redacted == '{"authorization": [REDACTED], "status": "ok"}'
    assert '"status": "ok"' in redacted


def test_private_key_block_is_redacted_line_by_line():
    text = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxyz\n"
        "abcdefghijklmnop\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after\n"
    )

    redacted, changed = redact_text(text)

    assert changed is True
    assert "MIIEowIBAAKCAQEAxyz" not in redacted
    assert redacted.count("\n") == text.count("\n")
    assert redacted.splitlines()[0] == "before"
    assert redacted.splitlines()[-1] == "after"


def test_redaction_preserves_line_structure_and_ordinary_output():
    text = "test_one PASSED\nexit_code=0\nduration=1.25\ncollected 12 items\n"

    redacted, changed = redact_text(text)

    assert changed is False
    assert redacted == text


def test_redaction_never_joins_lines_across_a_secret_argument():
    text = "--token\nnext-line-value\n"

    redacted, _ = redact_text(text)

    assert redacted.count("\n") == text.count("\n")
    assert "next-line-value" in redacted


# Adversarial inputs at the return limit, named so pytest ids stay readable.
_BACKTRACK_CASES = {
    "word-run": lambda n: "a" * n,
    "near-miss-tail": lambda n: "a" * (n - 10) + "secre",
    "repeated-near-miss": lambda n: "api_ke" * (n // 6),
    "long-prefix-then-match": lambda n: "a" * (n - 20) + "password=x",
    "bearer-run": lambda n: "Bearer " + "A" * (n - 7),
    "authorization-run": lambda n: "Authorization: " + "x" * (n - 15),
    "jwt-run": lambda n: "eyJ" + "a" * (n - 3),
    "punctuation-run": lambda n: ".-" * (n // 2),
}


@pytest.mark.parametrize("case", sorted(_BACKTRACK_CASES))
def test_redaction_does_not_backtrack_catastrophically(case: str):
    """The redaction patterns nest quantifiers, so guard the worst case at the return limit."""
    import time

    payload = _BACKTRACK_CASES[case](MAX_ARTIFACT_BYTES)

    started = time.perf_counter()
    redact_text(payload)
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"redaction took {elapsed:.2f}s on {len(payload)} bytes"


def test_redaction_stays_linear_at_the_read_bound():
    """Redaction sees the complete artifact, so the guard belongs at the read bound, not the return bound."""
    import time

    payload = "exit_code=0\n" * (MAX_READABLE_BYTES // 12)

    started = time.perf_counter()
    redact_text(payload)
    elapsed = time.perf_counter() - started

    assert elapsed < 20.0, f"redaction took {elapsed:.2f}s on {len(payload)} bytes"


def test_redact_command_behaviour_is_unchanged():
    assert redact_command("$env:API_KEY='abc' ; run") == "$env:API_KEY=[REDACTED] ; run"
    assert redact_command("tool --password hunter2") == "tool --password [REDACTED]"
    assert redact_command("git clone https://u:p@host/r.git") == "git clone https://[REDACTED]@host/r.git"


def test_content_is_redacted_and_flagged(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(
        root / "attempt-00" / "validation-final" / "validation-01.stdout.log",
        "running\nAuthorization: Bearer abcdefghijklmnopqrst\ndone\n",
    )
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])

    assert payload["content"]["redacted"] is True
    assert "abcdefghijklmnopqrst" not in payload["content"]["text"]
    assert payload["content"]["text"].splitlines()[0] == "running"
    assert payload["content"]["text"].splitlines()[-1] == "done"


def test_production_paths_are_scrubbed_from_content(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(
        root / "attempt-00" / "validation-final" / "validation-01.stdout.log",
        f"wrote {config.evidence_root}\\p\\t\\out.log\nworktree {config.workspace_root}\\p\n",
    )
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])
    text = payload["content"]["text"]

    assert str(config.evidence_root) not in text
    assert str(config.workspace_root) not in text
    assert "<evidence-root>" in text
    assert "<workspace-root>" in text
    assert payload["content"]["redacted"] is True


def test_truncation_at_the_byte_limit(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    body = "a" * (MAX_ARTIFACT_BYTES + 5_000)
    target = _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", body)
    manifest = evidence_list(config, TASK_ID)
    entry = manifest["artifacts"][0]

    assert entry["truncated_when_read"] is True
    assert entry["original_size_bytes"] == target.stat().st_size

    payload = evidence_show(config, TASK_ID, entry["artifact_id"])
    content = payload["content"]

    assert content["truncated"] is True
    assert content["complete"] is False
    assert content["returned_size_bytes"] == MAX_ARTIFACT_BYTES
    assert content["original_size_bytes"] == len(body)
    assert len(content["text"]) == MAX_ARTIFACT_BYTES


@pytest.mark.parametrize(
    ("secret", "fragment"),
    [
        ("sk-ant-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF", "sk-ant-abcdefghij"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnop"),
        ("password=hunter2hunter2hunter2", "hunter2hunter2"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghij"),
    ],
)
def test_secret_straddling_the_return_limit_is_still_redacted(
    tmp_path: Path, secret: str, fragment: str
):
    """Redaction must see the whole artifact: a secret spanning the cut must not leak a prefix."""
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    # Place the secret so it begins just before the return limit and ends after it.
    padding = MAX_ARTIFACT_BYTES - (len(secret) // 2)
    _write(
        root / "attempt-00" / "validation-final" / "validation-01.stdout.log",
        "x" * padding + secret + "\ntail\n",
    )
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])
    text = payload["content"]["text"]

    assert payload["content"]["redacted"] is True
    assert fragment not in text
    assert secret not in text
    assert len(text.encode("utf-8")) <= MAX_ARTIFACT_BYTES
    # Redaction runs before truncation, so replacing the secret can bring the text back under the
    # limit. Either outcome is correct; leaking the straddling prefix is not.
    assert payload["content"]["complete"] is not payload["content"]["truncated"]


def test_sanitized_projection_never_exceeds_the_return_limit(tmp_path: Path):
    """Pretty-printing can expand a document; the response must still honour the documented bound."""
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    bulky = {
        **FINAL_RESULT,
        "changed_files": [f"src/module_{index:05d}.py" for index in range(20_000)],
    }
    _write(root / "attempt-00" / "task-result.json", json.dumps(bulky, separators=(",", ":")))
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])
    content = payload["content"]

    assert content["returned_size_bytes"] <= MAX_ARTIFACT_BYTES
    assert len(content["text"].encode("utf-8")) <= MAX_ARTIFACT_BYTES
    if payload["structured_data"] is not None:
        assert len(json.dumps(payload["structured_data"]).encode("utf-8")) <= MAX_ARTIFACT_BYTES
    if content["truncated"]:
        assert content["complete"] is False


def test_nested_json_inside_an_allowlisted_field_is_sanitized(tmp_path: Path):
    """A non-string value in a string field must not bypass redaction and path scrubbing."""
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    poisoned = {
        **FINAL_RESULT,
        "status": {
            "note": f"{config.evidence_root}\\prod\\secret",
            "auth": "Authorization: Bearer abcdefghijklmnopqrst",
            "deeper": [{"key": "api_key=sk-abcdefghijklmnopqrstuvwx"}],
        },
        "summary": ["ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuv"],
    }
    _write(root / "attempt-00" / "task-result.json", json.dumps(poisoned))
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])
    document = json.dumps(payload)

    assert "abcdefghijklmnopqrst" not in document
    assert "sk-abcdefghij" not in document
    assert "sk-ant-abcdefghij" not in document
    assert str(config.evidence_root) not in document
    assert "[REDACTED]" in json.dumps(payload["structured_data"])


def test_deeply_nested_structured_json_is_bounded(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(root / "attempt-00" / "task-result.json", "[" * 120_000 + "]" * 120_000)
    manifest = evidence_list(config, TASK_ID)

    exit_code, payload, _ = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        manifest["artifacts"][0]["artifact_id"],
        "--json",
    )

    assert exit_code == 0
    _assert_envelope(payload, "task.evidence.show", success=True)
    assert payload["data"]["structured_data"] is None
    assert payload["data"]["structured_status"] in {"unsupported_shape", "invalid_json"}
    assert "Traceback" not in json.dumps(payload)


def test_absurd_attempt_directory_name_is_ignored(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    # Longer than the bounded digit run the attempt pattern accepts, so it is never converted.
    _write(root / ("attempt-" + "9" * 50) / "task-result.json", json.dumps(FINAL_RESULT))
    _write(root / "attempt-00" / "task-result.json", json.dumps(FINAL_RESULT))

    payload = evidence_list(config, TASK_ID)

    assert [item["display_name"] for item in payload["artifacts"]] == ["attempt-00/task-result.json"]


def test_corrupt_task_state_reports_a_structured_database_error(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    with db.connect() as connection:
        connection.execute("UPDATE tasks SET state='NOT_A_REAL_STATE' WHERE id=?", (TASK_ID,))

    exit_code, payload, _ = _run_json(config, capsys, "task", "evidence", "list", TASK_ID, "--json")

    assert exit_code == 1
    _assert_envelope(payload, "task.evidence.list", success=False)
    assert payload["error"]["code"] == "DATABASE_ERROR"
    assert "NOT_A_REAL_STATE" not in json.dumps(payload)
    assert "Traceback" not in json.dumps(payload)


def test_truncation_does_not_split_a_multibyte_character(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    # "é" is two bytes, so the truncation boundary lands mid-character.
    body = "é" * (MAX_ARTIFACT_BYTES // 2 + 1_000)
    _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", body)
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])

    assert payload["content"]["truncated"] is True
    assert set(payload["content"]["text"]) == {"é"}


def test_sha256_covers_the_original_bytes_not_the_returned_text(tmp_path: Path):
    import hashlib

    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    body = "b" * (MAX_ARTIFACT_BYTES + 100)
    target = _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", body)
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])

    assert payload["content"]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert payload["content"]["original_size_bytes"] == target.stat().st_size


def test_empty_file_is_served(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", "")
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])

    assert payload["content"]["text"] == ""
    assert payload["content"]["original_size_bytes"] == 0
    assert payload["content"]["truncated"] is False
    assert payload["content"]["redacted"] is False
    assert manifest["artifacts"][0]["content_available"] is True


def test_unicode_text_round_trips(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", "Résumé 工具 ✅\n")
    manifest = evidence_list(config, TASK_ID)

    exit_code, payload, _ = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        manifest["artifacts"][0]["artifact_id"],
        "--json",
    )

    assert exit_code == 0
    assert payload["data"]["content"]["text"] == "Résumé 工具 ✅\n"


def test_crlf_content_round_trips_unchanged(tmp_path: Path):
    """Forge writes evidence in text mode, so real Windows artifacts carry CRLF endings."""
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", "one\r\ntwo\r\n")
    manifest = evidence_list(config, TASK_ID)

    payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])

    assert payload["content"]["text"] == "one\r\ntwo\r\n"
    assert payload["content"]["redacted"] is False


def test_invalid_utf8_is_rejected_without_leaking_bytes(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    target = root / "attempt-00" / "validation-final" / "validation-01.stdout.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"valid text \xff\xfe more text")
    manifest = evidence_list(config, TASK_ID)

    exit_code, payload, stderr = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        manifest["artifacts"][0]["artifact_id"],
        "--json",
    )

    assert exit_code == 2
    _assert_envelope(payload, "task.evidence.show", success=False)
    assert payload["error"]["code"] == "ARTIFACT_INVALID_UTF8"
    assert "\\xff" not in json.dumps(payload)
    assert "valid text" not in json.dumps(payload)
    assert "ARTIFACT_INVALID_UTF8" in stderr


def test_binary_content_is_rejected(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    target = root / "attempt-00" / "changes.patch"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"GIT binary patch\x00\x01\x02\x03payload")
    manifest = evidence_list(config, TASK_ID)

    exit_code, payload, _ = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        manifest["artifacts"][0]["artifact_id"],
        "--json",
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "ARTIFACT_UNSAFE_TYPE"
    assert "payload" not in json.dumps(payload)


def test_oversize_artifact_is_refused(tmp_path: Path, monkeypatch, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", "x" * 4_096)
    monkeypatch.setattr(evidence, "MAX_READABLE_BYTES", 1_024)
    manifest = evidence_list(config, TASK_ID)

    assert manifest["artifacts"][0]["content_available"] is False

    exit_code, payload, _ = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        manifest["artifacts"][0]["artifact_id"],
        "--json",
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "ARTIFACT_TOO_LARGE"


# ---------------------------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "artifact_id",
    [
        "../../../../windows/win.ini",
        "..\\..\\forge.toml",
        "attempt-00/task-result.json",
        r"C:\CodexData\forge.db",
        "/etc/passwd",
        "\\\\server\\share\\file.txt",
        "task-result.json:hidden",
        "ev_" + "z" * 32,
        "ev_short",
        "",
        "CON",
        "\\\\.\\PhysicalDrive0",
    ],
)
def test_path_shaped_artifact_ids_are_rejected_by_the_id_gate(
    tmp_path: Path, artifact_id: str, capsys
):
    """First line of defence: a path-shaped identifier never reaches path handling at all."""
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)

    exit_code, payload, _ = _run_json(
        config, capsys, "task", "evidence", "show", TASK_ID, "--artifact-id", artifact_id, "--json"
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "ARTIFACT_NOT_FOUND"
    assert "win.ini" not in json.dumps(payload["data"] or {})


@pytest.mark.parametrize(
    "relative",
    [
        "../../../../windows/win.ini",
        "..\\..\\forge.toml",
        "attempt-00/../../../escape.txt",
        "attempt-01/review/review.prompt.txt",
        "attempt-01/credentials.env",
    ],
)
def test_well_formed_ids_for_non_public_paths_are_rejected(tmp_path: Path, relative: str, capsys):
    """Second line of defence: an id that PASSES the shape gate still resolves to nothing.

    These identifiers are computed exactly as Forge computes them, so they are syntactically valid
    and reach the manifest lookup. They must fail there, not at the shape check.
    """
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)
    artifact_id = compute_artifact_id(TASK_ID, relative)
    assert artifact_id.startswith("ev_") and len(artifact_id) == 35

    exit_code, payload, _ = _run_json(
        config, capsys, "task", "evidence", "show", TASK_ID, "--artifact-id", artifact_id, "--json"
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "ARTIFACT_NOT_FOUND"
    document = json.dumps(payload)
    assert "win.ini" not in document
    assert "prompt" not in document
    assert "credentials" not in document


def test_alternate_data_stream_names_are_rejected_by_the_name_guard():
    assert evidence._safe_component("task-result.json") is True
    assert evidence._safe_component("task-result.json:hidden") is False
    assert evidence._safe_component("..") is False
    assert evidence._safe_component(".") is False
    assert evidence._safe_component("a/b") is False
    assert evidence._safe_component("a\\b") is False
    assert evidence._safe_component("") is False


def test_directories_named_like_artifacts_are_not_served(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    (root / "attempt-00" / "task-result.json").mkdir(parents=True)
    (root / "attempt-00" / "changes.patch").mkdir(parents=True)

    payload = evidence_list(config, TASK_ID)

    assert payload["artifacts"] == []


def test_reading_a_directory_reports_unsafe_type(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    directory = root / "attempt-00" / "validation-final"
    directory.mkdir(parents=True)

    with pytest.raises(EvidenceError) as excinfo:
        evidence._read_bounded(directory)

    assert excinfo.value.code in {"ARTIFACT_UNSAFE_TYPE", "FILESYSTEM_ERROR"}


def test_symlink_escape_is_refused(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _build_evidence(config)
    outside = tmp_path / "outside-secret.json"
    outside.write_text(json.dumps({"secret": "value"}), encoding="utf-8")
    link = root / "attempt-00" / "task-result.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    names = {item["display_name"] for item in evidence_list(config, TASK_ID)["artifacts"]}

    assert "attempt-00/task-result.json" not in names


def test_symlinked_attempt_directory_escape_is_refused(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-attempt"
    _write(outside / "task-result.json", json.dumps(FINAL_RESULT))
    try:
        (root / "attempt-00").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    payload = evidence_list(config, TASK_ID)

    assert payload["artifacts"] == []


def test_evidence_root_containment_is_proven(tmp_path: Path):
    config, _ = _configured(tmp_path)

    with pytest.raises(EvidenceError) as excinfo:
        evidence.task_evidence_root(config, "..", TASK_ID)

    assert excinfo.value.code == "ARTIFACT_OUTSIDE_EVIDENCE_ROOT"

    with pytest.raises(EvidenceError) as nested:
        evidence.task_evidence_root(config, PROJECT_NAME, "../../escape")

    assert nested.value.code == "ARTIFACT_OUTSIDE_EVIDENCE_ROOT"


def test_resolve_within_rejects_an_outside_candidate(tmp_path: Path):
    root = tmp_path / "evidence" / "task"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(EvidenceError) as excinfo:
        evidence._resolve_within(outside, root)

    assert excinfo.value.code == "ARTIFACT_OUTSIDE_EVIDENCE_ROOT"


# ---------------------------------------------------------------------------------------------
# Consistency and race safety
# ---------------------------------------------------------------------------------------------


def test_artifact_removed_between_list_and_show(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _build_evidence(config)
    manifest = evidence_list(config, TASK_ID)
    artifact = _artifact_of_kind(manifest, "final_result")
    (root / "attempt-01" / "task-result.json").unlink()

    exit_code, payload, _ = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        artifact["artifact_id"],
        "--json",
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "ARTIFACT_NOT_FOUND"


def test_artifact_changed_during_read_is_retryable(tmp_path: Path, monkeypatch, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    target = _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", "first\n")
    manifest = evidence_list(config, TASK_ID)

    real_fstat = os.fstat
    calls = {"count": 0}

    def growing_fstat(fileno):
        calls["count"] += 1
        if calls["count"] > 1:
            # Simulate the controller appending while the read was in flight.
            target.write_text("first\nsecond\n", encoding="utf-8")
        return real_fstat(fileno)

    monkeypatch.setattr(evidence.os, "fstat", growing_fstat)

    exit_code, payload, _ = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        manifest["artifacts"][0]["artifact_id"],
        "--json",
    )

    assert exit_code == 1
    _assert_envelope(payload, "task.evidence.show", success=False)
    assert payload["error"]["code"] == "ARTIFACT_CHANGED_DURING_READ"
    assert payload["error"]["details"]["retryable"] is True


def test_probe_refuses_a_symlink_swapped_in_after_discovery(tmp_path: Path):
    """Discovery and probing are separate passes; the manifest must not hash a swapped-in link."""
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    target = _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", "inside\n")
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"outside secret\n")

    artifacts = evidence.discover_artifacts(root, TASK_ID)
    assert len(artifacts) == 1

    target.unlink()
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    probe = evidence._probe(artifacts[0])

    assert probe.sha256 is None
    assert probe.readable is False


def test_identity_check_fails_closed_without_filesystem_identity(tmp_path: Path, monkeypatch):
    """A filesystem that cannot identify files must not silently disable the swap check."""
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", "content\n")
    manifest = evidence_list(config, TASK_ID)
    artifact_id = manifest["artifacts"][0]["artifact_id"]

    monkeypatch.setattr(evidence, "_identity", lambda info: None)

    with pytest.raises(EvidenceError) as excinfo:
        evidence_show(config, TASK_ID, artifact_id)

    assert excinfo.value.code == "ARTIFACT_CHANGED_DURING_READ"


def test_same_file_helper_semantics():
    import os as _os

    a = _os.stat_result((0o100644, 11, 22, 1, 0, 0, 10, 0, 0, 0))
    same = _os.stat_result((0o100644, 11, 22, 1, 0, 0, 10, 0, 0, 0))
    other = _os.stat_result((0o100644, 99, 22, 1, 0, 0, 10, 0, 0, 0))
    unidentifiable = _os.stat_result((0o100644, 0, 0, 1, 0, 0, 10, 0, 0, 0))

    assert evidence._same_file(a, same) is True
    assert evidence._same_file(a, other) is False
    # Both sides unidentifiable must not compare equal — that is the fail-closed case.
    assert evidence._same_file(unidentifiable, unidentifiable) is False


def test_reading_does_not_lock_the_artifact(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    root = _task_root(config)
    target = _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", "line\n")
    manifest = evidence_list(config, TASK_ID)

    with target.open("a", encoding="utf-8") as writer:
        # The controller holds the file open for appending while the contract reads it.
        payload = evidence_show(config, TASK_ID, manifest["artifacts"][0]["artifact_id"])
        writer.write("appended\n")

    assert payload["content"]["text"] == "line\n"


# ---------------------------------------------------------------------------------------------
# Error contract, streams, and exit codes
# ---------------------------------------------------------------------------------------------


def test_evidence_not_found_for_show_without_evidence(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, payload, _ = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "show",
        TASK_ID,
        "--artifact-id",
        compute_artifact_id(TASK_ID, "attempt-00/task-result.json"),
        "--json",
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "EVIDENCE_NOT_FOUND"


def test_evidence_unavailable_when_project_is_missing(tmp_path: Path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    with db.connect() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM projects")

    with pytest.raises(EvidenceError) as excinfo:
        evidence_show(config, TASK_ID, compute_artifact_id(TASK_ID, "attempt-00/task-result.json"))

    assert excinfo.value.code == "EVIDENCE_UNAVAILABLE"


def test_uninitialized_database_reports_configuration_error(tmp_path: Path, capsys):
    config, _ = _configured(tmp_path, initialize=False)

    exit_code, payload, _ = _run_json(config, capsys, "task", "evidence", "list", TASK_ID, "--json")

    assert exit_code == 2
    assert payload["error"]["code"] == "CONFIGURATION_ERROR"


def test_unsupported_schema_version_is_rejected(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, payload, _ = _run_json(
        config,
        capsys,
        "task",
        "evidence",
        "list",
        TASK_ID,
        "--schema-version",
        "2.0",
        "--json",
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "UNSUPPORTED_SCHEMA"
    assert payload["error"]["details"]["supported"] == ["1.0", "1.1", "1.2", "1.3", "1.4"]


@pytest.mark.parametrize("requested", ["1.0", "1.1", "1.2", "1.3", "1.4"])
def test_requested_supported_schema_versions_are_accepted(tmp_path: Path, capsys, requested: str):
    """Each supported version is served, and the response declares the version that was asked for."""
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, payload, _ = _run_json(
        config, capsys, "task", "evidence", "list", TASK_ID, "--schema-version", requested, "--json"
    )

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["schema_version"] == requested


def test_database_error_is_structured(tmp_path: Path, monkeypatch, capsys):
    import sqlite3

    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    def failing_get_task(self, task_id):
        raise sqlite3.OperationalError("database is locked: C:/CodexData/forge.db")

    monkeypatch.setattr(ForgeDB, "get_task", failing_get_task)

    exit_code, payload, _ = _run_json(config, capsys, "task", "evidence", "list", TASK_ID, "--json")

    assert exit_code == 1
    assert payload["error"]["code"] == "DATABASE_ERROR"
    assert "locked" not in json.dumps(payload)
    assert "CodexData" not in json.dumps(payload)


def test_invalid_argument_reports_the_evidence_command(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, payload, _ = _run_json(config, capsys, "task", "evidence", "show", TASK_ID, "--json")

    assert exit_code == 2
    _assert_envelope(payload, "task.evidence.show", success=False)
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


def test_every_evidence_error_code_has_a_documented_exit_code():
    codes = [
        "TASK_NOT_FOUND",
        "EVIDENCE_NOT_FOUND",
        "EVIDENCE_UNAVAILABLE",
        "ARTIFACT_NOT_FOUND",
        "ARTIFACT_NOT_PUBLIC",
        "ARTIFACT_OUTSIDE_EVIDENCE_ROOT",
        "ARTIFACT_UNSAFE_TYPE",
        "ARTIFACT_TOO_LARGE",
        "ARTIFACT_INVALID_UTF8",
        "ARTIFACT_CHANGED_DURING_READ",
        "UNSUPPORTED_SCHEMA",
        "CONFIGURATION_ERROR",
        "DATABASE_ERROR",
        "FILESYSTEM_ERROR",
    ]
    for code in codes:
        assert code in ERROR_EXIT_CODES
        assert error_exit_code(code) in {1, 2}
    assert error_exit_code("ARTIFACT_CHANGED_DURING_READ") == 1
    assert error_exit_code("ARTIFACT_NOT_FOUND") == 2


def test_no_traceback_or_raw_exception_reaches_stdout(tmp_path: Path, monkeypatch, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    def exploding(*args, **kwargs):
        raise RuntimeError("boom at C:/CodexData/forge.db with token sk-abcdefghijklmnop")

    monkeypatch.setattr("codex_forge.cli.evidence_list", exploding)

    exit_code, payload, _ = _run_json(config, capsys, "task", "evidence", "list", TASK_ID, "--json")

    assert exit_code == 1
    assert payload["error"]["code"] == "COMMAND_FAILED"
    document = json.dumps(payload)
    assert "Traceback" not in document
    assert "boom" not in document
    assert "sk-abcdefghijklmnop" not in document
    assert "CodexData" not in document


def test_stdout_carries_exactly_one_json_document(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)

    exit_code, out, _ = _run(config, capsys, "task", "evidence", "list", TASK_ID, "--json")

    assert exit_code == 0
    _single_json_document(out)
    assert out.count('"schema_version"') == 1


def test_legacy_console_encoding_is_handled(tmp_path: Path, capsys, monkeypatch):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    root = _task_root(config)
    _write(root / "attempt-00" / "validation-final" / "validation-01.stdout.log", "Résumé 工具\n")

    exit_code, payload, _ = _run_json(config, capsys, "task", "evidence", "list", TASK_ID, "--json")

    assert exit_code == 0
    assert payload["data"]["artifact_count"] == 1


# ---------------------------------------------------------------------------------------------
# Human-readable behaviour and compatibility
# ---------------------------------------------------------------------------------------------


def test_human_readable_list_output(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)

    exit_code, out, err = _run(config, capsys, "task", "evidence", "list", TASK_ID)

    assert exit_code == 0
    assert err == ""
    assert out.startswith("ev_")
    assert "attempt-00/implementation/implementation.final.json" in out
    assert ".prompt.txt" not in out


def test_human_readable_list_without_evidence(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, out, _ = _run(config, capsys, "task", "evidence", "list", TASK_ID)

    assert exit_code == 0
    assert out.strip() == f"No evidence recorded for {TASK_ID}."


def test_human_readable_show_output(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)
    manifest = evidence_list(config, TASK_ID)
    artifact = _artifact_of_kind(manifest, "patch")

    exit_code, out, err = _run(
        config, capsys, "task", "evidence", "show", TASK_ID, "--artifact-id", artifact["artifact_id"]
    )

    assert exit_code == 0
    assert err == ""
    assert out.startswith("patch\tattempt-01/changes.patch")
    assert "+++ b/src/app.py" in out


def test_human_readable_failure_writes_only_to_stderr(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, out, err = _run(config, capsys, "task", "evidence", "list", "TASK-missing")

    assert exit_code == 2
    assert out == ""
    assert "TASK_NOT_FOUND" in err


@pytest.mark.parametrize(
    ("arguments", "command"),
    [
        (["status", "--json"], "status"),
        (["project", "list", "--json"], "project.list"),
        (["task", "list", "--json"], "task.list"),
        (["decision", "list", "--json"], "decision.list"),
    ],
)
def test_existing_schema_1_0_commands_are_unchanged(
    tmp_path: Path, capsys, arguments: list[str], command: str
):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, payload, stderr = _run_json(config, capsys, *arguments)

    assert exit_code == 0
    assert stderr == ""
    _assert_envelope(payload, command, success=True)


def test_task_show_still_works_alongside_evidence(tmp_path: Path, capsys):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)

    exit_code, payload, _ = _run_json(config, capsys, "task", "show", TASK_ID, "--json")

    assert exit_code == 0
    _assert_envelope(payload, "task.show", success=True)
    assert payload["data"]["task"]["task_id"] == TASK_ID


def test_evidence_commands_do_not_mutate_task_or_database_state(tmp_path: Path, capsys):
    import hashlib

    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    _build_evidence(config)
    manifest = evidence_list(config, TASK_ID)

    def snapshot() -> tuple:
        with db.connect() as connection:
            tasks = [tuple(row) for row in connection.execute("SELECT * FROM tasks")]
            projects = [tuple(row) for row in connection.execute("SELECT * FROM projects")]
            runs = [tuple(row) for row in connection.execute("SELECT * FROM runs")]
            decisions = [tuple(row) for row in connection.execute("SELECT * FROM decisions")]
        return (tasks, projects, runs, decisions)

    def evidence_snapshot() -> dict[str, str]:
        digests: dict[str, str] = {}
        for path in sorted(_task_root(config).rglob("*")):
            if path.is_file():
                digests[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return digests

    before_db = snapshot()
    before_files = evidence_snapshot()
    before_size = config.database_path.stat().st_size

    _run_json(config, capsys, "task", "evidence", "list", TASK_ID, "--json")
    for artifact in manifest["artifacts"]:
        _run_json(
            config,
            capsys,
            "task",
            "evidence",
            "show",
            TASK_ID,
            "--artifact-id",
            artifact["artifact_id"],
            "--json",
        )

    assert snapshot() == before_db
    assert evidence_snapshot() == before_files
    assert config.database_path.stat().st_size == before_size


def test_public_and_excluded_kind_registries_are_disjoint():
    assert not set(evidence.PUBLIC_ARTIFACT_KINDS) & set(evidence.EXCLUDED_ARTIFACT_KINDS)
    kinds = {
        entry.kind
        for entry in (
            *evidence._STRUCTURED_CLASSES.values(),
            evidence._PATCH_CLASS,
            evidence._VALIDATION_LOG_CLASS,
        )
    }
    assert kinds == set(evidence.PUBLIC_ARTIFACT_KINDS)


@pytest.mark.parametrize(
    ("group", "name", "expected"),
    [
        ("implementation", "implementation.prompt.txt", "model_prompt"),
        ("review", "review.events.jsonl", "model_event_stream"),
        ("implementation", "implementation.stderr.log", "model_stderr"),
        ("review", "random.txt", "unclassified"),
        (None, "credentials.env", "unclassified"),
    ],
)
def test_exclusion_reasons(group, name, expected):
    assert evidence.excluded_kind(group, name) == expected
    assert evidence.classify_group_file(group or "review", name) is None


def test_windows_only_reparse_point_helper_is_available():
    if sys.platform != "win32":
        pytest.skip("reparse-point attributes exist only on Windows")
    assert evidence._FILE_ATTRIBUTE_REPARSE_POINT == 0x400
