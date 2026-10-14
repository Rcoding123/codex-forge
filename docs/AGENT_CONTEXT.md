# Codex Forge — agent context index

Navigation index only. This file carries no authority: code, schemas and tests
are executable truth, and `AGENTS.md` → `docs/ARCHITECTURE.md` /
`docs/CONTROL-PLANE-API.md` → `docs/ENGINEERING_LESSONS.md` remain the
authority order. If this index disagrees with any of them, they win — fix the
index. Use it to find the owning seam quickly instead of rereading the
authority documents in full.

## Status

- Current Forge baseline: `7f7c8db` (main) — PR #14, the canonical Shared Chat
  contract fixture pins the exact admission base (`--base-commit` /
  `admission_base_commit`); no runtime change since PR #12, repository-
  generation admission (contract `1.4`). Contract `1.2` (PR #10) added the
  Forge-minted `repository_registration_id`; `1.3` (PR #11) added the exact
  admission base and `project relocate`.
- Shared Chat `a89bc05` + Forge `7f7c8db` are certified for the
  repository-generation **and exact admission-base** boundary: Shared Chat
  now sends `--base-commit` (the local default-branch tip of the bound
  generation, proven a commit) on every `task add`, and Forge admits and
  executes from exactly that commit. Shared Chat `7c591c4` + Forge `e679c36`
  were certified for the repository-generation boundary. The base law itself
  is unchanged since PR #11.

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
| Review / trusted output | `runner.py` (`decode_review_result`, `ReviewTransportError`, conformance), `schema_resources.py`, `src/codex_forge/schemas/review-result.schema.json` |
| Orchestrator / execution | `orchestrator.py` (claims READY work, phases, limits, recovery), `runner.py` (`CodexRunner`), `usage.py` (token-usage normalization) |
| Evidence | `evidence.py` (read-only, versioned external access), `src/codex_forge/schemas/task-result.schema.json` |
| Git / worktrees | `gitops.py` |
| Task / project deletion | no dedicated module — lives in `db.py` / `orchestrator.py` / `cli.py`; contract in `docs/CONTROL-PLANE-API.md` (Task delete / Project delete); behavior proven by `tests/test_delete.py` |
| Repository generation / admission | `task_submission.py` (`require_expected_registration`, `verify_expected_registration`, `admit_task`, `_locked_generation_guard`, `resolve_base_commit`), `db.py` (`add_project_guarded` mints the registration, `relocate_project`), `control_plane.py` (`*_SINCE` version gates); proven by `tests/test_repository_registration.py`, `tests/test_binding_contract.py` (boundary fixture pin), `tests/test_admission_base_commit.py` |
| CLI / control plane | `cli.py`, `control_plane.py`, `docs/CONTROL-PLANE-API.md` |
| Config / prompts / knowledge | `config.py`, `prompts.py`, `knowledge.py`, `validation.py` |
| Telemetry | `telemetry.py` (OTel tracing), `agent_telemetry.py` (Gate 4 primitives — implemented, tested, unwired) |
| Schemas | `schema_resources.py`, `src/codex_forge/schemas/*.schema.json`; schema versioning policy is textual in `docs/CONTROL-PLANE-API.md` |
| Process supervision / recovery | `processes.py`, `db.py` (`execution_checkpoints`), `orchestrator.py`; regression tests `test_processes.py`, `test_execution_checkpoints.py` |
| Exhausted model-blocker continuation | `blockers.py`, `db.py` (`blocker_continuations`), `orchestrator.py` / `cli.py` (`task resolve-blocker`); `test_blocker_continuations.py` |
| Project integration / phase gates | `workflows.py`, `db.py` (`project_workflows`, claims/retries/relocation), `cli.py` (`project plan/progress/resume/extend`); `test_project_workflows.py` |

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
- `repository_registration_id` is the repository-generation identity: Forge
  mints it, it is opaque, it enters no task id / fingerprint / attempt, it
  survives relocation, and delete + recreate rotates it. `repo_path` is a
  relocatable descriptor, never identity. Relocation ≠ generation change;
  delete/recreate = generation change.
- Contract `1.4` requires `expected_repository_registration_id`; a supplied
  expectation is enforced at every version. Admission proves caller
  expectation == resolved generation == locked generation, for a created task
  and for an adopted/reconciled one alike, and answers with the exact Task
  snapshot proven under that transaction — never a post-proof re-read.
- Mutation and adversarial experiments (reverting a fix to prove a test
  bites, rotating or poisoning state) run in an isolated worktree or scratch
  copy — never by modifying or resetting the primary uncommitted worktree.

## Gates

    python -m pytest          # via `rtk proxy pytest ...`; includes MANIFEST integrity
    python -m ruff check .
    python -m compileall -q src tests
    git diff --check
