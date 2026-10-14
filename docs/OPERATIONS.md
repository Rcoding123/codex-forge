# Operations guide

## Operating assumptions

Run Forge only on a dedicated Windows development VPS. The default non-interactive, unrestricted Codex execution policy is inappropriate for a shared workstation or any host with production credentials, live brokerage access, or unrelated private data.

Use one Windows account for bootstrap, Codex authentication, and the `CodexForge` scheduled task. Persistent state must remain outside the Git checkout.

## Install

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\bootstrap.ps1
.\forge.ps1 doctor
.\forge.ps1 status --json
```

Bootstrap is idempotent. It detects storage roots unless `-ForgeRoot` and `-DataRoot` are provided, creates the private Python environment, configures Codex, initializes SQLite, runs acceptance checks, and registers the S4U task.

The active `forge.toml` is machine-local and ignored. Review its paths, models, efforts, limits, and disabled worker features before onboarding projects.

## Scheduled task

The intended task properties are:

- name `CodexForge`;
- the same account that owns Codex authentication;
- S4U logon and highest available run level;
- startup and logon triggers;
- `IgnoreNew` multiple-instance policy;
- three one-minute restart attempts;
- no interactive desktop requirement;
- repository path as working directory;
- logs in the configured data root.

Inspect it without changing state:

```powershell
Get-ScheduledTask -TaskName CodexForge | Format-List *
Get-ScheduledTaskInfo -TaskName CodexForge | Format-List *
```

Perform a controlled restart:

```powershell
.\restart-service.ps1
.\forge.ps1 doctor
.\forge.ps1 status --json
```

The restart script targets only a command line verified as the Forge daemon, terminates its descendants, verifies no duplicate remains, starts the scheduled task, and requires `Running` state.

## Daily checks

```powershell
.\forge.ps1 doctor
.\forge.ps1 status --json
.\forge.ps1 task list --state FAILED
.\forge.ps1 decision list
```

Investigate repeated failures before retrying. A manual retry cannot exceed the task's configured maximum attempts.

## Runtime hardening and pending publication

Update while the daemon is stopped, back up the data root, and restart after validation. SQLite
creates the internal checkpoint table automatically; concurrent initialization is supported. The
CLI JSON contracts and their default version remain unchanged. Checkpoints belong to the controller,
so operators and clients must not edit them directly.

A failed push retains its reviewed candidate and initial execution base. The next allowed attempt
re-proves the candidate and retries publication without paying for another model phase. Keep the
recorded worktree clean and the task specification unchanged. Publication is confirmed against the
exact remote task-branch tip; no force push is used.

`CANDIDATE_STATE_UNAVAILABLE` on an older interrupted implicit-base task means that its original
execution base cannot be proven. Do not manufacture a checkpoint from current HEAD. Inspect the
retained branch and evidence, determine the intended base, and submit a new task with that exact
`--base-commit` under a new task id. Retain the original task evidence.

`PROCESS_SUPERVISION_FAILED` means containment or process closure could not be proven. The task
fails without an automatic retry. Inspect host process state and raw logs before resolving the host
problem and retrying. Timeout evidence retains output produced before termination.

Configuration loading rejects zero/negative worker counts, polling intervals, and summary lengths,
as well as boolean, string, or fractional integer settings. `max_task_tokens = 0` still disables the
token ceiling; `max_repair_cycles = 0` still disables repairs.

## Back up and restore

Back up the configured data root and, when task work must be retained, the Forge work/repository root. Do not copy Codex authentication into the source repository or audit reports.

For a consistent SQLite copy, stop the service or use SQLite's backup facility; include `forge.db`, `forge.db-wal`, and `forge.db-shm` if copying a live WAL database. Preserve evidence and recovery logs with the database.

Restore to paths reflected in `forge.toml`, verify filesystem ownership, then run:

```powershell
.\forge.ps1 doctor
.\forge.ps1 status --json
.\restart-service.ps1
```

Inspect recovered active tasks before manual intervention. Do not delete a retained worktree until its database state and evidence have been reconciled.

## Update

Before updating, confirm the checkout is clean, back up persistent state, and review the changelog:

```powershell
git status --short --branch
.\update.ps1
.\forge.ps1 doctor
```

`update.ps1` performs a fast-forward-only pull and reruns bootstrap without recreating the scheduled task. It never migrates persistent state into the repository. If an update changes configuration or service scripts, schedule and verify a controlled restart.

## Recovery scenarios

### Controller stopped or VPS session ended

Use `.\restart-service.ps1`. SQLite recovery returns eligible interrupted tasks to READY while retaining their branch and worktree. Exhausted tasks become FAILED. Review `<DataRoot>\recovery.jsonl` and task evidence.

### Task requires a product decision

Run `.\forge.ps1 decision list`. `NEEDS_DECISION` tasks are not claimed, and other READY tasks continue. Forge v0.2.0 exposes decision inspection; resolution workflow should be handled deliberately against the database/task process used by the operator.

### Worktree mismatch

Record the task ID, branch, worktree path, and base commit. Use `git worktree list --porcelain` from the managed project clone and inspect both checkouts. Do not prune or delete until durable evidence is copied and task state is understood.

### Deterministic validation failure

Read the exact validation stdout/stderr in the task evidence directory. Do not rely on a model summary and do not raise retry/repair limits to force progression. Correct the project or task contract, then use the bounded retry command if attempts remain.

### Codex authentication failure

Run `codex login status` and `.\forge.ps1 doctor` as the scheduled-task account. If needed, perform `codex login` interactively once, then restart the service. Never place `auth.json` or a Codex home inside this repository.

## Logs and evidence

Raw output is authoritative. Controller/model summaries are deliberately bounded. Expected durable material includes:

- controller/service logs;
- recovery JSONL;
- per-phase prompt, events, final JSON, and stderr;
- validation command/stdout/stderr/exit code;
- review findings and repair results;
- patch and final task-result JSON;
- cumulative token usage and route/effort metadata.

Retain failed-task evidence until root cause and recovery are complete.

### Sharing evidence with an external application

Operators inspect the raw tree directly. External applications must not: it retains prompts, model
event streams, unrestricted stderr, and absolute production paths, and nothing on disk is redacted.
Point integrations at the read-only control-plane commands instead:

```powershell
.\forge.ps1 task evidence list <task-id> --json
.\forge.ps1 task evidence show <task-id> --artifact-id <artifact-id> --json
```

These publish only allowlisted artifacts — final results, validation logs, review results, patches,
repair summaries — with redaction, a 256 KiB return bound, and path-safety checks applied. They are
read-only, take no locks, and cannot block the controller from writing evidence. If a call returns
`ARTIFACT_CHANGED_DURING_READ`, the task was writing that artifact; retry. See
[the control-plane contract](CONTROL-PLANE-API.md) for classifications, limits, and error codes.

Do not grant an integration file-system access to the data root as a workaround for a missing
artifact class; add the class to the contract instead.

## Run a declared project in phases

Register the project and submit its bounded tasks before installing a plan. Tasks must be unstarted,
belong to this project, and omit `--base-commit`. List dependencies before their dependents.
Use the exact initial commit returned by `git rev-parse HEAD`, not a branch name.

```json
{
  "branch": "forge-project/pilot",
  "base_commit": "FULL_CANONICAL_COMMIT_ID",
  "phases": [
    {
      "name": "P1 vertical slice",
      "tasks": ["TASK-engine", "TASK-client"],
      "acceptance_commands": ["python -m pytest", "dotnet build"]
    },
    {
      "name": "P2 next increment",
      "tasks": ["TASK-next"],
      "acceptance_commands": ["python -m pytest"]
    }
  ]
}
```

Acceptance commands are project-specific, non-interactive checks. Configure real commands for the
available toolchain. Missing tools fail the gate; Forge never substitutes a passing stub.

```powershell
forge project plan my-project --file plan.json --json
forge project progress my-project --json
forge run --daemon
```

The controller runs declared tasks sequentially within this project, integrates exact reviewed
commits, and advances phases only after acceptance. It keeps the source checkout unchanged.
Other projects retain parallel execution. Unlisted tasks remain queued, including after plan
completion. Plan registration and resume require the controller singleton lock; stop the daemon
before changing those operational settings.

A failed task stops the project plan. Resolve its cause, use `forge task retry <id>` when attempts
remain, and use `forge project resume my-project` to enable progression again. Retry accepts only
FAILED, BLOCKED, and NEEDS_DECISION tasks; it never reopens completed or active work.
A failed gate also needs explicit resume. Resume rechecks the existing declaration and branch;
it does not bypass failed checks or change budgets. If a gate exposes a code defect after task
completion, stop and revise the declared work through a separate reviewed development change;
this version does not insert automatic gate-repair tasks or rewrite completed evidence.

Raw project diagnostics remain under `DataRoot/project-evidence/<workflow-id>`. Gate checkouts
are created under `DataRoot/gate-worktrees/gate-<gate-id>` using the full 32-hex gate UUID, without
an extra workflow directory. The compact layout reduces Windows Git/.NET path-length failures;
keep DataRoot itself short enough for the project's generated build paths. Existing checkout
destinations are refused rather than reused, even when empty. Historical directories using
`gate-worktrees/<workflow-id>/<gate-id>` and all prior logs are retained without migration.
Full workflow/gate identities and project-evidence paths remain unchanged. Checkouts are retained for operator inspection;
external consumers use `project progress`, which excludes logs and redacts credential values.
Do not check out or move the integration branch manually. Relocation is refused during a gate.
An idle relocation must preserve all recorded commits and the exact integration ref.

After a pilot finishes, submit the next bounded tasks and create `next-plan.json` with the same
branch, the completed `head_commit`, and only new phases/tasks. Run:

```powershell
forge project extend my-project --file next-plan.json --json
```

Extension archives the completed plan and starts a new immutable generation from its proven tip.
Choose task budgets before running; neither extension nor phase advancement raises them.

## Context efficiency and flexible task budgets

Forge keeps stable reviewer policy before variable task data, reports each exact binding once, and
passes fresh controller validation evidence to review. Reviewers still investigate stale or
insufficient evidence and concrete risks. Complete task contracts, raw logs, semantic review,
and final controller validation are retained. Models are not downgraded merely to fit a token count.

`limits.max_output_chars` bounds captured review output, capped at 6,000 characters across all
command summaries. Commands, result metadata, and specification text remain complete. Truncated
summaries name their raw log files. This is a context bound, not an execution budget.

For work whose token needs are not predictable, `limits.max_task_tokens = 0` disables the task
token ceiling. Attempt, repair, identical-failure, and timeout limits still apply. A positive token
ceiling is checked at phase boundaries and can be exceeded by one in-flight phase; it is not an
instantaneous provider quota. Choose that policy explicitly in the run configuration. Defaults and
public usage accounting remain unchanged.

See [OpenAI prompt caching guidance](https://developers.openai.com/api/docs/guides/prompt-caching)
for stable-prefix layout. Forge does not inject undocumented CLI cache controls. Cache availability
and actual savings depend on the provider, model, timing, and request contents; measure new tokens,
cached input, commands, and successful completion together.

## Optional local tracing

Telemetry is off by default and emits nothing until `[telemetry] enabled = true` is set in
`forge.toml`. When enabled, the controller exports `forge.task` → `forge.attempt` → `forge.phase`
spans to an OpenTelemetry Collector on the loopback interface only. Spans carry bounded
vocabularies, opaque task identifiers, and token counts — never prompts, source, commands, paths,
branches, credentials, or review prose.

Forge neither downloads nor manages a Collector. Install one yourself and start it with
`ops\otel\Start-OtelCollector.ps1`, which creates no service and no scheduled task. A missing or
unreachable Collector is not a task failure: telemetry degrades to a no-op.

See [the telemetry guide](TELEMETRY.md) for the attribute reference, the privacy guarantees, and
the rollback procedure. Model-call-level tracing is a later slice; one `forge.phase` span covers a
whole Codex invocation, which may issue several model requests internally.

## RTK pytest limitation

RTK 0.44.2 can describe a passing quiet pytest suite as if no tests were collected. Until RTK is upgraded and rebenchmarked, pytest compression is disabled:

```powershell
rtk proxy pytest ...
# or
.\.venv\Scripts\python.exe -m pytest
```

Forge's controller validation preserves raw stdout/stderr regardless, so the limitation affects convenience compression, not stored evidence.

## Release limitation

Controlled service restarts and in-progress task recovery have been validated. A full Windows OS reboot recovery test remains outstanding. Perform it only in a maintenance window with state and evidence backed up, then verify scheduled-task start, singleton behavior, queue progress, task worktree consistency, and raw evidence retention.


## Shared prerequisites for an exhausted model blocker

Use `task handoff-prerequisite` when a schema-valid, controller-sealed blocker needs a separately
scoped shared change. Keep the original task contract, frozen workflow and comparison base intact.
Admit the prerequisite to a separate registered repository/project at that exact base and let Forge
complete implementation, independent review and final validation. Gate that candidate with the
same exact commands in a source-project workflow. If the source task ran standalone, a separately
admitted evidence-only task can establish the gate workflow at its candidate; its additional files
must remain under `docs/evidence/` and leave application inputs unchanged.

Wait for both source completion and its PASSED gate, then run public `task handoff-prerequisite
--dry-run` with exact source-task/registration/candidate/workflow/gate pins. See the full command and
receipt contract in [CONTROL-PLANE-API.md](CONTROL-PLANE-API.md#hand-off-an-independently-gated-prerequisite).
Inspect the complete approved path list. Save the returned `receipt_json` as exact UTF-8 bytes in a
new immutable operator file and use the returned SHA-256 for execution. No private DB, candidate seal
file or raw event access is needed. Preflight neither reserves a repair nor changes task state.

The execution command holds the existing singleton lock through import, the unused same-attempt
repair, review, validation, integration and actual original-project phase gate. It preserves all
budgets and never exposes attempt four to another launcher. Retry the identical reason/receipt
only to resume an unstarted import/slot or finish a sealed completion; a consumed interrupted slot
cannot be replayed. Never copy source files into the failed checkout manually, change its base,
reset counters, move refs or edit original evidence. Conflicts, source drift, changed receipts or
locked-file changes need a new governed decision; the command refuses them. Inspect public task
state, the handoff command response's `data.prerequisite_handoff` attribution (repeat the identical
terminal command to inspect it without execution), and all three patch artifacts, then project progress for the
actual postintegration gate. A successful source gate is a prerequisite, not a replacement for it.
