# Codex Forge architecture

## Design objective

Codex Forge turns bounded engineering tasks into durable, independently verified Git commits. The Python controller owns truth and progression; Codex workers supply implementation and review. A model's assertion that work passed is never accepted as validation evidence.

## Components

| Component | Responsibility |
|---|---|
| CLI and inbox | Register projects, create tasks, expose status/decisions, and import YAML work items. |
| SQLite store | Persist projects, tasks, attempts, states, decisions, runs, usage, branch/worktree paths, and failure fingerprints. |
| Orchestrator | Claim READY work, enforce dependencies and limits, select routes, run phases, and recover interrupted tasks. |
| Git operations | Create one task branch and isolated worktree, verify repository state, commit completed work, and generate patches. |
| Codex runner | Invoke `codex exec` without a TTY, approvals, or ambient conversational state; retain JSONL, prompts, stderr, structured output, and usage. |
| Validation runner | Execute configured commands directly in the worktree with timeouts, UTF-8, exact exit-code handling, bounded summaries, and raw logs. |
| Independent reviewer | Inspect the bounded change separately from implementation and return a schema-validated verdict/findings. |
| Scheduled service | Run a singleton daemon under a non-interactive Windows S4U task and restart it after controlled failure. |

## Trust and storage boundaries

```text
isolated Windows development VPS
|
+-- source repository (controller code only)
|
+-- ForgeRoot
|   +-- repositories/<project>       stable main clone
|   +-- worktrees/<project>/<task>   one isolated task checkout
|
+-- DataRoot
    +-- forge.db                     authoritative queue/run state
    +-- evidence/<project>/<task>    immutable diagnostic artifacts
    +-- inbox                        task ingestion
    +-- knowledge                    scoped reusable patterns
    +-- logs                         service/controller logs
```

The source repository is not the persistence boundary. Machine configuration, authentication, databases, worktrees, sessions, and evidence belong outside Git.

### Evidence disclosure boundary

The evidence tree is written for operators and for post-incident analysis, not for external
consumption. On disk it retains raw model prompts, JSONL model event streams, unrestricted process
stderr, thread identifiers, and absolute production paths. Nothing on disk is redacted.

External applications therefore never read the evidence tree or `forge.db`. They call the read-only
`task.evidence.list` and `task.evidence.show` control-plane commands, which are the disclosure
boundary. `src/codex_forge/evidence.py` owns that boundary end to end — discovery, classification,
opaque artifact identifiers, path canonicalization and containment, size bounds, redaction,
structured sanitization, and structured errors — so no other module decides what is publishable and
the CLI only renders what the module returns.

The boundary is an allowlist keyed on the controller's evidence layout, not on file extensions: an
artifact that is not positively recognized is excluded, so a new controller output cannot become
public by accident. Adding a new public artifact class is a deliberate change to that module and to
[the control-plane contract](CONTROL-PLANE-API.md).

## Task state flow

```text
BACKLOG --dependencies satisfied--> READY
READY --claim/attempt++--> IMPLEMENTING
IMPLEMENTING --> VALIDATING
VALIDATING --failure, bounded--> REPAIRING --> VALIDATING
VALIDATING --pass--> REVIEWING
REVIEWING --blocking finding, bounded--> REPAIRING --> REVIEWING
REVIEWING --pass--> final validation --> commit/evidence --> COMPLETED

any active state --product ambiguity--> NEEDS_DECISION
any active state --limit/loop/terminal failure--> FAILED
```

Dependency-blocked tasks remain out of the claim query, so they cannot freeze unrelated READY work. Claims and state transitions are persisted before expensive work begins.

## Worktree isolation

For each claimed task, Forge derives a task branch and creates/reuses a project-specific worktree below `workspace_root`. All worker, validation, review, repair, and commit operations use that worktree as their working directory. The stable project clone supplies the base revision and remains unchanged. Branch and worktree paths are recorded in SQLite and retained for restart recovery and diagnostics.

## Validation and review independence

The implementation worker emits schema-constrained machine-readable output. Forge then ignores any claimed test status and executes each configured validation command itself. A failing command stops a stop-on-failure sequence, while stdout, stderr, duration, exact exit code, and command remain recoverable on disk.

Review is a separate Codex run with a read-only role. Follow-up reviews receive prior findings and are scoped to those findings instead of repeating full implementation exploration. A blocking review finding must be repaired and re-reviewed before final validation and commit.

## Limits and loop resistance

Forge enforces complementary bounds:

- per-command and per-Codex-phase timeout;
- task attempt limit;
- validation/review repair-cycle limit;
- identical-failure fingerprint threshold;
- cumulative per-task new-token budget (fresh input + output, excluding cached input reads), checked at phase boundaries;
- process-scoped singleton lock for the controller.

An unchanged deterministic failure reaches `FAILED` rather than cycling. A human-dependent ambiguity reaches `NEEDS_DECISION`. Limits are durable configuration and are not weakened automatically.

## Model routing and context control

Local inventory, queue operations, validation, and status reporting use no model. Normal implementation and review use the balanced route at medium effort. High/critical risk or unresolved failure selects the escalation route at high effort. Each phase is a fresh bounded run; relevant failure or review context is injected explicitly, and full raw logs remain on disk instead of being resent.

Worker-only unrelated Codex features are disabled by configuration. Large repository documentation is not injected automatically; project `AGENTS.md` files provide scoped rules.

## Recovery

The scheduled task uses `IgnoreNew`, and the daemon holds a crash-released process lock, preventing duplicate controllers. At startup, Forge finds tasks left in active states. Tasks below their attempt limit return safely to READY while preserving branch/worktree state; exhausted tasks become FAILED. Recovery events are appended outside the repository.

Controlled scheduled-task restart recovery is validated. Full host reboot recovery remains an explicit release limitation until exercised on a maintenance window.

## Safety boundary

The VPS is the sandbox boundary for `danger-full-access`. Git worktrees isolate tasks from each other, but they are not an operating-system security boundary. Forge therefore belongs only on a dedicated development VPS without personal secrets, production deployment authority, or live financial connectivity.
