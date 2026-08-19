# Codex Forge — engineering authority

Shared engineering contract for every agent (Claude Code, Codex) working on
this repository. This repository is the autonomous controller, not a project
being controlled by it.

## Authority order

1. Code, schemas, tests — executable truth; documentation loses.
2. `AGENTS.md` (this file).
3. `docs/ARCHITECTURE.md`, `docs/CONTROL-PLANE-API.md`.
4. `CONTRIBUTING.md`, `README.md`.
5. Other planning/descriptive docs.

Descriptive documents never override executable truth.

## Ownership

- Forge owns task truth, execution, review, repair, validation, and evidence.
- Shared Chat and other clients never open or mutate `forge.db` directly.
- External clients use supported Forge contracts only.

## Controller laws

- Keep runtime dependencies minimal and Windows-first behavior intact.
- Never weaken the invariant that tasks run in isolated Git worktrees.
- Simulation, Market Replay/Playback, historical-data recording, GUI
  automation, and local test execution are allowed.
- Never add live broker connectivity, live credentials, order routing,
  withdrawals, or production-deployment capabilities.
- Controller state must be durable in SQLite and evidence files; do not rely
  on chat memory.
- All command execution must remain non-interactive.
- Preserve raw logs on disk, but keep model-facing summaries bounded.

## Review law

- Raw reviewer bytes are untrusted.
- Only a successfully decoded, schema-valid `ReviewResult` is semantic review.
- Malformed, missing, or provider-failed review is infrastructure/protocol
  failure; infrastructure failure is never implementation failure.
- Tests are evidence; they do not redefine the specification.

## Evidence law

- Governed evidence is retained. Task deletion removes operational registry
  state, not evidence; retained evidence stays readable through supported
  evidence commands.
- Evidence ambiguity fails closed.
- Do not rewrite historical evidence casually.

## Deletion law

Merged behavior (see `tests/test_delete.py` and the Task delete / Project
delete sections of `docs/CONTROL-PLANE-API.md`):

- Active tasks cannot be deleted.
- Deleting a non-running task removes registry-owned task/runs/decisions
  state; evidence and verification-relevant Git history are retained.
- Disposable worktree cleanup is governed and fail-closed.
- Project deletion requires zero tasks and never cascades task deletion.
- The user source repository and its evidence are retained.

## Development gates

Canonical completion gates:

    python -m pytest
    python -m ruff check .
    python -m compileall -q src tests
    git diff --check

`pytest` includes MANIFEST integrity via `tests/test_manifest.py`. Add or
update tests for controller behavior. Use RTK for supported noisy commands,
but fall back to the exact direct read-only command if RTK rejects it. With
RTK 0.44.2, run pytest through `rtk proxy pytest ...`; do not trust compressed
passing-suite output.

## Git / change discipline

- Explicit staging only; never `git add -A`.
- Development branches use `agent/*`; never push directly to `main`; PR
  required; a human merges.
- Runtime Forge-generated `forge/{slug}` branches are not developer branches.
- No silent contract changes; contract/API changes require corresponding
  tests and docs.
