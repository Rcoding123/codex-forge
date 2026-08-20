# Codex Forge — agent context index

Navigation index only. This file carries no authority: code, schemas and tests
are executable truth, and `AGENTS.md` → `docs/ARCHITECTURE.md` /
`docs/CONTROL-PLANE-API.md` → `docs/ENGINEERING_LESSONS.md` remain the
authority order. If this index disagrees with any of them, they win — fix the
index. Use it to find the owning seam quickly instead of rereading the
authority documents in full.

## Ownership map

- Forge is the governed execution controller: it owns task truth, attempts,
  budgets, validation, review, repair, and evidence.
- Clients (Shared Chat, CLI consumers) never open or mutate `forge.db`
  directly; the control-plane contract (`docs/CONTROL-PLANE-API.md`) is the
  boundary.
- Coding backend: `codex exec`, invoked by `CodexRunner` in `runner.py`.

## Subsystem → owning files

Source in `src/codex_forge/`.

| Subsystem | Start here |
| --- | --- |
| Task submission / persistence | `task_submission.py` (validation/normalization/serialization), `db.py` (SQLite store), `models.py`; inbox template `templates/task.yaml` |
| Review / trusted output | `runner.py` (`decode_review_result`, `ReviewTransportError`, conformance), `schemas/review-result.schema.json` |
| Orchestrator / execution | `orchestrator.py` (claims READY work, phases, limits, recovery), `runner.py` (`CodexRunner`), `usage.py` (token-usage normalization) |
| Evidence | `evidence.py` (read-only, versioned external access), `schemas/task-result.schema.json` |
| Git / worktrees | `gitops.py` |
| Task / project deletion | no dedicated module — lives in `db.py` / `orchestrator.py` / `cli.py`; contract in `docs/CONTROL-PLANE-API.md` (Task delete / Project delete); behavior proven by `tests/test_delete.py` |
| CLI / control plane | `cli.py`, `control_plane.py`, `docs/CONTROL-PLANE-API.md` |
| Config / prompts / knowledge | `config.py`, `prompts.py`, `knowledge.py`, `validation.py` |
| Telemetry | `telemetry.py` (OTel tracing), `agent_telemetry.py` (Gate 4 primitives — implemented, tested, unwired) |
| Schemas | `schemas/*.schema.json`; schema versioning policy is textual in `docs/CONTROL-PLANE-API.md` |

Tests are flat in `tests/` — start from the test named for the seam:
`test_delete.py`, `test_task_submission.py`, `test_review_transport.py`,
`test_review_infrastructure.py`, `test_specification_conformance.py`,
`test_conformance_fail_closed.py`, `test_orchestrator.py`,
`test_control_plane.py`, `test_evidence_contract.py`, `test_manifest.py`
(MANIFEST integrity).

## High-value invariants (headlines — the law lives in AGENTS.md)

- Raw reviewer bytes are untrusted; only schema-valid `ReviewResult` counts as
  semantic review.
- Tasks always run in isolated Git worktrees; never weaken this.
- Infrastructure/protocol failure is never implementation failure.
- Active tasks cannot be deleted; task deletion removes registry state only,
  never evidence. Project deletion requires zero tasks and never cascades.
- Simulation-only controller: the capability restrictions in the AGENTS.md
  controller laws are permanent; never add what they exclude.

## Gates

    python -m pytest          # via `rtk proxy pytest ...`; includes MANIFEST integrity
    python -m ruff check .
    python -m compileall -q src tests
    git diff --check
