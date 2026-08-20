# Codex Forge — engineering authority

Shared engineering contract for every agent (Claude Code, Codex) working on
this repository. This repository is the autonomous controller, not a project
being controlled by it.

## Authority order

1. Code, schemas, tests — executable truth; documentation loses.
2. `AGENTS.md` (this file).
3. `docs/ARCHITECTURE.md`, `docs/CONTROL-PLANE-API.md`.
4. `docs/ENGINEERING_LESSONS.md` — durable engineering lessons.
5. `CONTRIBUTING.md`, `README.md`.
6. Other planning/descriptive docs.

Descriptive documents never override executable truth.

## Read order for architecture or implementation work

1. `AGENTS.md` — this repository's engineering law.
2. `docs/ARCHITECTURE.md` and the relevant `docs/CONTROL-PLANE-API.md` section.
3. `docs/ENGINEERING_LESSONS.md` — do not repeat a failure it already records.
4. The owning code, schemas and tests — executable truth.

The lessons file is not a changelog and does not restate the contracts; it
records why they are shaped the way they are. It never overrides this file.

### Session startup and rereads

`docs/AGENT_CONTEXT.md` is a navigation index — a map from subsystems to their
owning files, plus headline invariants. It carries no authority and never
overrides this file, the architecture/API docs, or the lessons; where they
disagree, they win.

- New working session: read `docs/AGENT_CONTEXT.md`, then the read order
  above as the task requires.
- Subsequent tasks in the same session: do not reread the large authority
  documents in full if they were already read this session and have not
  changed. Consult `docs/AGENT_CONTEXT.md` and open only the task-relevant
  sections and files. Code, schemas and tests remain executable truth.
- For broad navigation or reachability work, prefer a researcher/explorer
  subagent that returns exact files and functions to the primary coding
  agent, instead of loading large unrelated modules into the primary context.
  Small tasks do not require delegation.

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
