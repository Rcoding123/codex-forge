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

The reviewer's execution sandbox is `codex.review_sandbox`, defaulting to `read-only`. It selects only what the reviewer is *asked* for. A restricted request is still proven launchable before it is used, and still falls back to the configured worker sandbox when it is not; an explicit `danger-full-access` reviewer uses no restricted token and is therefore not probed. Widening it never widens the reviewer's role: review remains a no-source-edit phase judged solely on a schema-valid `ReviewResult`. Terminal evidence records the requested and the effective mode separately, so a review that ran under a widened sandbox says so.

Implementation and repair phase results remain immutable descriptions of those individual model runs. The controller captures the task branch's Git base before implementation begins. On terminal success it does not reuse the latest phase's changed-file claim: it derives both `task-result.json.changed_files` and `changes.patch` from that base to the final candidate commit. Terminal evidence therefore describes the complete candidate snapshot even when later attempts only repaired a subset of files.

The execution base is established once in an internal SQLite `execution_checkpoints` row. It is
separate from caller-supplied `admission_base_commit`: starting an implicit-base task never invents
admission provenance. Retries use the saved execution base for worktree lineage and model-facing
comparison authority. Missing legacy provenance fails closed when a previously recorded checkout
could already contain task work. Completed historical evidence is never backfilled or rewritten.

After final validation and semantic review pass, committed candidates receive a completion
checkpoint before publication. A subsequent attempt can finish a pending push without invoking
implementation, validation, or review again. It re-proves repository generation, checkout ownership,
cleanliness, lineage, candidate identity, and the specification/validation/policy digest. Publication
uses an immutable candidate refspec without force and requires the remote task branch to report
that exact candidate. Operational checkpoints cascade with task deletion; evidence remains retained.

Binding conformance uses the ordered exact-text union of architecture decisions and constraints.
Identical submitted text appearing in both sections is reported once; duplicate reviewer entries
still fail conformance. A review repair that fails controller validation passes the new command
evidence alongside unresolved findings to the next repair. Exhaustion retains validation ownership.

The trusted review choke point prospectively retains `review[-N]/controller-review.json` after
schema and command-closure checks and the existing conformance enforcement. This immutable
diagnostic binds the raw model review SHA256, model and effective verdicts, original itemized
acceptance/binding reports and actual effective findings. It records the decision already made;
public retrieval never reruns policy. Invalid or unclosed reviews have no trusted diagnostic.
Unique temporary files and no-clobber publication accept identical retries and refuse conflicts
through the existing nonretryable provenance failure path, without another model or repair call.

Governed finalization includes the diagnostic in its exact trusted-review history before sealing
the cache. Cached replay verifies/reuses identical evidence; pre-feature sealed caches remain
read-only and are not backfilled. Historical review readers likewise do not create diagnostics.
The additive public artifact kind uses existing bounded, redacted evidence retrieval; original
model files, legacy projections, schema defaults and runtime limits remain unchanged.

The transported JSON schemas are package resources under `src/codex_forge/schemas`. Transport
paths remain valid for the entire worker call, and local review decoding reads the same resources.
An installed wheel therefore does not depend on the source checkout layout.

## Opt-in project integration and phase gates

`project plan` freezes ordered phase membership and acceptance commands for unstarted tasks.
Legacy projects keep their existing scheduling and base selection. A planned project claims one
declared task at a time; other projects still run in parallel. Unlisted tasks stay queued. Plans
require automatic commits, an exact initial commit, and a new `forge-project/<name>` branch.
Explicit admission bases are never replaced; the first plan version admits only implicit-base tasks.

Each task snapshots the recorded integration tip as its execution base. After independent review
and final validation, Forge persists a pending integration before a compare-and-swap ref update.
The dedicated branch fast-forwards to the exact verified candidate, without changing the stable
checkout. Automatic publication, when configured, confirms both task and project branches before
task completion. Restart accepts only the recorded old tip or pending candidate and reuses the
existing completion checkpoint. Unexpected movement, changed specifications, or a checked-out
integration branch fails closed. Task admission provenance remains unchanged.

After all phase tasks complete and their integrations are recorded, controller acceptance commands
run in a fresh detached worktree of the exact integrated tip. The gate must execute at least one
command, pass every command, leave the checked commit clean and unchanged, and re-prove phase task
truth under the final SQLite write lock. Only then can the next phase run. Gate interruption keeps
its raw logs and starts a fresh gate attempt after restart; failures block until explicit resume.
Completed or active tasks cannot be manually retried. Repository relocation is refused during gates.

`project_workflows` holds operational cursor, generation, integration intent, and gate state.
Project deletion cascades that state. Raw plan, integration, and gate evidence remains under
`DataRoot/project-evidence/<workflow-id>`; detached gate worktrees remain under
`DataRoot/gate-worktrees/gate-<gate-id>`, using the full globally unique 32-hex gate ID without
another workflow path component. This shortens Windows Git and .NET build paths. Each checkout
directory is atomically reserved; an existing destination fails closed, including an empty one.
Older `gate-worktrees/<workflow-id>/<gate-id>` directories remain untouched; no state migration
occurs. Evidence still uses the complete workflow/gate identities. These artifacts have no task-evidence disclosure class.
`project progress` exposes a controller-owned projection and redacts command credentials.
Sealed integration records survive task deletion; deleting a task before its completion is recorded
blocks progression. Historical task evidence is never rewritten.

`project extend` creates another frozen plan only after completion, from the same dedicated branch
and its proven completed tip. It archives the previous complete cursor and gate state alongside
the original plan and logs. This supports a bounded pilot followed by further declared phases.
It does not invent new tasks or expand budgets automatically.

## Limits and loop resistance

Forge enforces complementary bounds:

- per-command and per-Codex-phase timeout;
- task attempt limit;
- validation/review repair-cycle limit;
- identical-failure fingerprint threshold;
- cumulative per-task new-token budget (fresh input + output, excluding cached input reads), checked at phase boundaries;
- process-scoped singleton lock for the controller.

An unchanged deterministic failure reaches `FAILED` rather than cycling. A human-dependent ambiguity reaches `NEEDS_DECISION`. Limits are durable configuration and are not weakened automatically.

Codex workers and validation commands share process-tree supervision. Windows starts the root
suspended, assigns a kill-on-close Job Object with no breakaway flags, and then resumes it. POSIX
starts a new process session. Timeout, interruption, and normal root exit terminate remaining
descendants and verify closure before returning. Failed containment or closure cannot trigger an
automatic retry. Output is streamed to evidence files, including partial validation output.

Worker count, polling seconds, phase timeout, and configured summary length must be positive
integers. Repair cycles and token limits may be zero; zero tokens retains the unlimited-budget
meaning. Booleans, strings, and fractional values are rejected for integer limits.

## Model routing and context control

Reviewer prompts place stable policy before task IDs, checkout paths, and candidate-specific data.
Binding requirements appear once as the ordered exact-text union; submission fields remain lossless
in storage and implementation prompts. Both worker roles receive the complete ordered union of
project/task validation commands.

Every review receives the current controller validation results, including exact commands, exit
codes, log paths, and bounded output tails. Captured output is evidence, never instruction authority.
The reviewer may rerun checks for missing/stale evidence or concrete regression risk, and must still
inspect code and prove specification conformance. It reviews the current staged, unstaged, and
untracked worktree state; HEAD or the index alone may still be the base. Final controller validation
always runs after semantic review. No validation/review gate is skipped to reduce token counts.

Local inventory, queue operations, validation, and status reporting use no model. Normal implementation and review use the balanced route at medium effort. High/critical risk or unresolved failure selects the escalation route at high effort. Each phase is a fresh bounded run; relevant failure or review context is injected explicitly, and full raw logs remain on disk instead of being resent.

Worker-only unrelated Codex features are disabled by configuration. Large repository documentation is not injected automatically; project `AGENTS.md` files provide scoped rules.

## Recovery

An explicit `task resolve-blocker` operation can continue an exhausted, closed original
implementation whose schema-valid result is `blocked`, using the unused first validation-repair
slot of that same attempt. Reservation never increments/decrements attempts, changes admitted text,
resets failure counters, or expands repair/token/time limits; later failures use normal accounting. Any prior current-attempt validation/review
or repair activity, ambiguous run provenance, completed task, or generic process failure is refused.

The command holds the controller singleton lock through reservation, claim, repair, validation,
independent review, final validation, integration, and phase progression. Reservations use BLOCKED,
then REPAIRING; an exhausted READY task is never exposed to an older running controller. The
existing frozen workflow may resume only its current task-blocked cursor. Postintegration phase
acceptance remains controller-owned and cannot be replaced by operator prerequisite evidence.

Future model-blocked implementation runs retain a controller-owned failed-tree snapshot. Operator
receipts bind the exact output, base and full current candidate. Legacy runs without that snapshot
are eligible only for an explicit evidence-only prospective-input attestation: HEAD must equal
the persisted base; changed files remain within an admitted `docs/evidence/` root; retained exact
validation commands, logs, and application hashes must match that base and current inputs.
This attestation does not reconstruct a historical tree or approve implementation correctness.

Operational reservations live in `blocker_continuations`, separate from task admission and frozen
plan signatures. New artifacts use distinct `attempt-NN-c-<8hex>` roots, with the same attempt number;
old failed files remain untouched. Raw operator receipts and resolution metadata are private.
Standard phase results/validation logs/terminal artifacts retain the existing disclosure classes.
An identical resolution is idempotent. An unstarted reservation can be explicitly reclaimed after
restart; a started paid slot cannot replay. A sealed completion can resume publication/integration
without model calls. All worktree, receipt, task, generation and workflow proofs are rechecked.

A separate `task handoff-prerequisite` uses that same reserved continuation slot when a shared
prerequisite must be implemented outside the original task scope. Its public preflight computes
exact receipt identities without mutating task/evidence/operational state or initializing the DB.
Execution proves the original controller seal, independently COMPLETED source task at the same
base, source registration/specification/checkpoint, and a PASSED same-registration detached gate.
A subsequent gate candidate is allowed only when its extra changes are regular evidence files.
The complete source delta must match the receipt's exact allowed paths and be disjoint from the
failed worker delta. Portable mode100644 files only are supported within documented implementation
roots; authority, Git control, owner-data, unsafe path, alias, link and mode changes fail closed.

The immutable continuation retains full original state and source provenance; `prerequisite_receipts`
retains one-use authority, and `prerequisite_events` appends the imported composite checkpoint.
Intent precedes filesystem changes. Atomic per-file import and bounded old/new/temp-prefix proofs
allow a partial import to resume without changing index, HEAD, refs, admission or execution base.
All other drift is refused. Receipt/event use history survives task deletion. Model/validation
boundaries lock prerequisite bytes and index visibility; terminal checks require exact committed
source blobs/modes/deletions. All local proof/context checks precede consumption of the paid slot.
Review sees the unchanged task contract plus explicit prerequisite attribution and any unresolved
trusted earlier review findings. Full base..candidate evidence is retained; separately classified
prerequisite/worker patches and terminal metadata make the two deltas inspectable. The existing
completion checkpoint and workflow integration/gate paths remain the publication authorities.

The scheduled task uses `IgnoreNew`, and the daemon holds a crash-released process lock, preventing duplicate controllers. At startup, Forge finds tasks left in active states. Tasks below their attempt limit return safely to READY while preserving branch/worktree state; exhausted tasks become FAILED. Recovery events are appended outside the repository.

Controlled scheduled-task restart recovery is validated. Full host reboot recovery remains an explicit release limitation until exercised on a maintenance window.

## Safety boundary

The VPS is the sandbox boundary for `danger-full-access`. Git worktrees isolate tasks from each other, but they are not an operating-system security boundary. Forge therefore belongs only on a dedicated development VPS without personal secrets, production deployment authority, or live financial connectivity.

## Prospective prerequisite finalization

A separate `task finalize-prerequisite` can supersede a TERMINAL failed handoff with one
independently completed, reviewed, exact-base gated prerequisite. The parent row and all previous
evidence remain immutable. A new `prerequisite_finalizations` journal has a unique task/parent,
transactional review-start boundary and operational task-delete cascade; retained evidence and
one-use prerequisite authority survive deletion. No implementation or repair phase is permitted.

The operator receipt is a NEW prospective attestation of the current candidate, not a claim that
a later blocked repair had a historical full-tree seal. It binds original base/admission, full
current HEAD/index/working identity, latest blocked review repair, trusted unresolved review, old
source and new source/gate/scope, exact finite replacements, prospective worker paths/hashes and explicitly selected metadata and
historical evidence. Existing source evidence is immutable. Replacement and original worker delta
are disjoint except explicitly typed, independently reviewed/gated metadata selection; aliases, links, executable modes, hidden index/exclude tricks and unrelated scope
changes fail closed. Atomic replacement journals accept only exact old/new/temp-prefix bytes.

Original validation, one independent review and final validation precede the shared ordinary
commit/completion/integration tail. Any failure terminates without self-repair. A durable started
review never replays; recovery requires an exact controller-certified cached review or completion.
Commit intent pins the intended tree and parent before commit so publication recovery never
substitutes another candidate. Active finalization owns VALIDATING/REVIEWING states, so public deletion cannot remove its checkout
while importing or validating. An interrupted resumable operation becomes BLOCKED; exhausted daemon
recovery becomes FAILED. No stage exposes an exhausted READY task.

Attempts/repair history, admission, frozen plans, token accounting and limits stay unchanged.
Timeout60 means the existing per-Codex-process/per-validation-command limit; this operation does
not invent an aggregate task-time budget. New review usage accrues to the original task.


Typed optional finalization selection separates current prospective worker input, gated capability W,
and separately budgeted/gated reconciliation C. The controller enforces finite exact metadata
operations, unchanged unselected worker/test/history bytes and unchanged W blobs. A selected Python
file may only lose one explicitly identified literal list element, with all other AST and bytes
preserved; text document operations are exact bounded spans. The immutable receipt and successor
journal bind both independently proven sources and the prospective before bytes. No repository name,
lab identifier or application-specific selector is built into the controller.


Finalization ignore exemptions use committed versioned ignore policy only. Unknown generated
ignore files are permitted solely below entire ancestors ignored by that policy, proved in an
isolated case-sensitive Git context with ambient Git config/environment removed. Self-hiding root/nested/staged ignores and mutable local/global exclusions
cannot hide candidate inputs. Effective external clean filters are refused before the sole review.
Worker and imported files require raw working-byte identity with staged/committed blobs; unchanged
baseline files retain both their exact prospective checkout bytes and original committed blob/mode.
The existing builtin CRLF/LF checkout representation of an unchanged baseline is preserved, never
used as authority to rewrite worker input. The final complete staged/committed tree is re-proved;
Git conversion introduced during validation/review cannot produce a completed unreviewed candidate.

The target, old parent source, capability source, reconciliation source and each retained gate
are guarded before existing conversion-capable source proof commands.
