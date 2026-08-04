# Codex Forge

Codex Forge is a Windows-first controller for long-running, non-interactive Codex engineering work on an isolated development VPS. It stores a durable task queue in SQLite, gives each task its own Git branch and worktree, runs implementation and independent review as separate Codex phases, validates results outside the model, bounds retries and usage, commits successful work locally, and retains raw evidence outside the source repository.

Release: **v0.2.0**. License: [MIT](LICENSE).

> [!WARNING]
> Forge's default `danger-full-access` sandbox and `approval_policy = "never"` are intended only for a dedicated, isolated development or simulation VPS. Do not use this configuration on a personal workstation, a host containing unrelated credentials, or a machine connected to production deployment, live brokerage, live order-routing, or financial-account systems.

## What Forge proves instead of trusting

- Every task runs on a task-specific branch in an isolated Git worktree.
- The controller, not the coding worker, executes configured validation commands and preserves exact exit codes and raw output.
- Independent review runs separately after implementation and validation.
- Validation repair and review repair are bounded by attempts, identical-failure detection, timeouts, repair limits, and a token budget.
- Successful tasks receive a local commit plus a durable evidence package.
- Interrupted active tasks are recovered from SQLite after controller restart.
- A blocked or decision-bound task does not prevent an unrelated READY task from running.
- Codex runs without an interactive terminal and with approval policy `never`.

See [Architecture](docs/ARCHITECTURE.md) for the complete control flow and trust boundaries.

## Requirements

- Windows 11 or Windows Server 2022 or later
- Windows PowerShell 5.1 or PowerShell 7
- An isolated development VPS and an account allowed to create a scheduled task
- Network access during bootstrap for missing tools
- An OpenAI account authorized for Codex CLI
- Git; Node.js LTS and Codex CLI are installed by bootstrap when absent

WinGet is optional. Bootstrap can use signed vendor installers and portable tooling when WinGet is unavailable. Forge creates a private Python environment in `.venv`; production commands and tests do not rely on a global `python` executable after bootstrap.

## One-command bootstrap

From an elevated PowerShell session in the cloned repository:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\bootstrap.ps1
```

Bootstrap detects storage drives, creates `.venv`, installs Forge and development checks, writes the machine-local `forge.toml`, initializes durable storage, configures non-interactive Codex defaults, runs tests and lint, and registers the `CodexForge` S4U scheduled task. If Codex is not authenticated, the official one-time login flow is the only interactive setup step.

Explicit storage roots are supported:

```powershell
.\bootstrap.ps1 -ForgeRoot "D:\CodexForge" -DataRoot "E:\CodexData"
```

For a validation-only installation without Task Scheduler:

```powershell
.\bootstrap.ps1 -SkipScheduledTask -SkipCodexLogin
```

After installation:

```powershell
.\forge.ps1 doctor
.\forge.ps1 status --json
```

## Repository and persistent-state boundaries

The Git repository contains controller source, tests, schemas, scripts, templates, and documentation only. Persistent or machine-specific state must live outside it:

```text
<ForgeRoot>\repositories\       managed project clones
<ForgeRoot>\worktrees\         isolated task worktrees
<DataRoot>\forge.db             durable queue and run state
<DataRoot>\evidence\            prompts, JSONL, raw logs, patches, results
<DataRoot>\inbox\               YAML task intake
<DataRoot>\knowledge\           reusable task patterns
<DataRoot>\logs\                controller and service logs
<DataRoot>\codex-logs\          Codex logs
```

`forge.toml`, `.venv`, databases, logs, evidence, worktrees, sessions, `.codex`, authentication files, and machine-local overrides are excluded by `.gitignore`. Never copy a user Codex home or a data root into this repository. Back up the data root separately; do not commit it.

`forge.example.toml` contains configurable Windows examples. Bootstrap writes the active values to the ignored `forge.toml`.

## Configuration

The recommended v0.2.0 policy is:

```toml
[codex]
executable = "codex"
sandbox = "danger-full-access"
approval_policy = "never"
extra_args = []
disabled_features = ["apps", "browser_use", "browser_use_external", "goals", "image_generation", "memories", "multi_agent", "plugins", "skill_search"]

[models]
implementation = "gpt-5.6-terra"
review = "gpt-5.6-terra"
escalation = "gpt-5.6-sol"

[reasoning]
implementation = "medium"
review = "medium"
escalation = "high"

[workers]
max_parallel = 2
poll_seconds = 15

[limits]
task_timeout_minutes = 180
max_repair_cycles = 2
max_identical_failures = 2
max_task_tokens = 2000000
max_output_chars = 12000
```

Model identifiers must remain valid for the installed Codex CLI and authenticated account. Routine inventory, queue work, validation, and summaries are local and model-free. Terra/medium handles normal implementation and review; Sol/high is reserved for explicit high/critical risk or escalation.

## Register a project

Clone through Forge:

```powershell
.\forge.ps1 project add sample-service `
  --url https://github.com/OWNER/sample-service.git `
  --default-branch main `
  --validation "python -m pytest"
```

Or register an existing local Git repository:

```powershell
.\forge.ps1 project add sample-service `
  --repo "D:\Projects\sample-service" `
  --default-branch main `
  --validation "python -m pytest"
```

Add a project-specific `AGENTS.md`; `templates/project-AGENTS.md` is a starting point. Keep project rules in that repository rather than expanding Forge's global instructions. Remote pushing is off unless a project is explicitly registered with `--push`.

## Submit work

Add a task through the CLI:

```powershell
.\forge.ps1 task add `
  --project sample-service `
  --id TASK-001 `
  --title "Add deterministic report export" `
  --objective "Implement CSV export for the existing report model." `
  --acceptance "Existing and new tests pass." `
  --acceptance "Malformed output paths return a clear error." `
  --validation "python -m pytest" `
  --risk medium `
  --max-attempts 3
```

Alternatively, copy `templates/task.yaml`, edit it, and place it under `<DataRoot>\inbox`. The daemon moves processed and rejected files into dedicated subdirectories.

External applications submit the same task through the versioned JSON contract. Validate first, then
submit; both accept the arguments shown above:

```powershell
.\forge.ps1 task add --project sample-service --id TASK-001 --title "..." --objective "..." --dry-run --json
.\forge.ps1 task add --project sample-service --id TASK-001 --title "..." --objective "..." --json
```

`--dry-run` resolves and validates the request without writing anything. Submission is idempotent by
`task_id`: retrying an identical request returns the existing task with `"created": false` instead of
creating a second one, and reusing an identifier for a different request is a structured conflict. See
[Control-plane JSON contract](docs/CONTROL-PLANE-API.md) for the envelope, field list, and error codes.

Useful commands:

```powershell
.\forge.ps1 project list
.\forge.ps1 task list
.\forge.ps1 task list --state FAILED
.\forge.ps1 task retry TASK-001
.\forge.ps1 decision list
.\forge.ps1 run --once
.\forge.ps1 status --json
```

External applications must use the versioned [Control-plane JSON contract](docs/CONTROL-PLANE-API.md)
instead of parsing terminal tables or reading `forge.db` directly. The contract covers inspection and
task submission; it does not start, retry, cancel, merge, or push anything.

## Task lifecycle and evidence

```text
READY
  -> task branch + isolated worktree
  -> implementation
  -> controller validation
  -> bounded validation repair
  -> independent read-only review
  -> bounded review repair
  -> final controller validation
  -> local commit + patch + task-result.json
  -> COMPLETED
```

A genuine product ambiguity becomes `NEEDS_DECISION`; exhausted or deterministic failure becomes `FAILED`. Raw phase events, stderr, validation stdout/stderr, prompts, review output, token usage, patches, and the final result remain under `<DataRoot>\evidence\<project>\<task>`.

## Updating and recovery

For a repository with a configured remote:

```powershell
.\update.ps1
```

The update is fast-forward-only and reruns idempotent bootstrap without replacing the scheduled task or deleting durable state. Review release notes and back up the data root before updating.

For a controlled controller restart:

```powershell
.\restart-service.ps1
```

The script stops only a verified Forge daemon process tree, starts the scheduled task, and requires it to reach `Running`. On startup, the controller recovers interrupted task state from SQLite and retains the associated branch/worktree. See [Operations](docs/OPERATIONS.md) for service inspection, backup, recovery, and troubleshooting.

## Troubleshooting

- `forge doctor` reports `codex_auth` missing: run `codex login` as the same Windows account used by the scheduled task, then rerun doctor.
- The scheduled task is not running: inspect Task Scheduler history and `<DataRoot>\logs`, then run `.\restart-service.ps1`.
- A task is stuck in an active state after a crash: restart the service once and inspect `recovery.jsonl` plus the task's branch/worktree before retrying.
- Validation differs from an interactive shell: use the exact raw validation logs; Forge prepends its private Python `Scripts` directory to child `PATH`.
- RTK reports no passing pytest tests: do not trust RTK 0.44.2's pytest summary. Pytest compression is disabled pending an RTK upgrade; use `rtk proxy pytest ...` or the Forge private Python directly and retain raw output.
- A task exhausts attempts or needs a decision: inspect the durable task error, evidence directory, and `decision list`; do not raise limits merely to hide a deterministic failure.

## Security and operational scope

Forge is for source-code engineering, tests, local simulation/replay, historical-data tooling, and other development-only work. It must not be extended with live brokerage connectivity, live credentials, order routing, withdrawals, production deployment, or unrelated host access. Read [SECURITY.md](SECURITY.md) before operating or contributing.

## Validated release status

The final production-readiness audit on 2026-08-02 passed:

- 35/35 unit tests and all static checks passed.
- A real queued smoke task proved isolated implementation, independent validation, review, repair, local commit, and evidence generation.
- Interactive approval count was zero.
- Controlled service restart recovery and blocked-task queue isolation passed.
- 22 demonstrated controller/service/configuration defects were fixed and regression-covered.

The sanitized result is in [Audit Summary](docs/AUDIT-SUMMARY.md). Two limitations remain explicit: RTK 0.44.2 pytest compression is disabled until upgraded and rebenchmarked, and full Windows OS reboot recovery has not yet been exercised (controlled scheduled-task restart recovery has).

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q src tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md), and [docs/OPERATIONS.md](docs/OPERATIONS.md).

`publish-to-github.ps1` is an explicit operator action that creates and pushes a repository; bootstrap and normal Forge operation never invoke it.
