# Codex Forge

Codex Forge is a Windows-first autonomous engineering controller for an isolated VPS. It runs Codex non-interactively with zero command approvals, isolates every task in a Git worktree, validates changes, repairs failures, requests an independent review, commits successful work, stores evidence, and moves to the next task.

It is intentionally a control plane rather than a giant prompt file.

## What it does

- Uses `codex exec --json` with structured JSON Schema outputs.
- Runs with `approval_policy = "never"` and `danger-full-access` on the isolated VPS.
- Keeps a durable SQLite task queue.
- Creates one Git branch and worktree per task.
- Runs deterministic repository validation outside the model.
- Feeds exact failures back into bounded repair loops.
- Runs a separate read-only reviewer before completion.
- Commits successful tasks and writes a binary-capable patch artifact.
- Saves raw Codex JSONL, stderr, test logs, summaries, usage, decisions, and reusable patterns.
- Imports YAML tasks dropped into an inbox.
- Starts at Windows startup and logon through a non-interactive S4U scheduled task with bounded restart settings.
- Installs and initializes RTK for command-output reduction.
- Attempts to add/install Token Optimizer without making it a hard dependency.

## VPS assumptions

- Windows 11 or Windows Server. WinGet is optional; the bootstrap falls back to signed installers and portable tools when WinGet is unavailable.
- The VPS is an isolated development machine where unrestricted Codex access is acceptable.
- Trading projects are limited to tool-building, simulation, Market Replay/Playback, historical or recorded data, offline analysis, and local test environments. No live broker connection, live trading credential, or order-routing service is installed.
- Two disks are preferred:
  - On a C:/D: machine, Forge automatically places active work under `C:\CodexForgeWork` and durable data under `D:\CodexData`.
  - On a D:/E: machine, it uses `D:\CodexForge` and `E:\CodexData`.

Paths remain configurable, and the detected choices are printed before installation.

## Publish once to your private GitHub repository

This package cannot create a remote repository without your GitHub authorization. From the extracted folder on any trusted machine, run:

```powershell
.\publish-to-github.ps1 -Repository codex-forge
```

The script installs GitHub CLI when needed, uses your authenticated GitHub account, creates a private repository, and pushes the initial commit. Your VPS can then use normal `git clone` and `git pull`.

## First installation

Run PowerShell as the VPS user that will own the Codex authentication:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\bootstrap.ps1
```

`bootstrap.ps1` installs or configures:

- Git
- a private Python 3.12 runtime through uv when system Python is absent
- Node.js LTS
- OpenAI Codex CLI
- ripgrep
- fd
- jq
- ast-grep
- RTK
- the Codex Forge virtual environment
- zero-prompt Codex configuration
- the SQLite database and storage directories
- a Windows scheduled task named `CodexForge`

Bootstrap opens the official Codex authentication flow only when the VPS is not already signed in. It works without WinGet and uses `npm.cmd` explicitly to avoid PowerShell execution-policy conflicts. That one-time sign-in is the only interactive setup step; bootstrap then completes the token tooling, registers the daemon, and starts it.

### Different disk paths

The bootstrap detects fixed drives automatically. Override them only when needed:

```powershell
.\bootstrap.ps1 -ForgeRoot "C:\CodexForgeWork" -DataRoot "D:\CodexData"
```

### Install without Task Scheduler

```powershell
.\bootstrap.ps1 -SkipScheduledTask
```

## Register a project

Clone through Forge:

```powershell
.\forge.ps1 project add market-analyzer `
  --url https://github.com/YOUR_ACCOUNT/market-analyzer.git `
  --default-branch main `
  --validation "dotnet test --nologo"
```

Or register an existing local repository:

```powershell
.\forge.ps1 project add market-analyzer `
  --repo "D:\Projects\market-analyzer" `
  --default-branch main `
  --validation "dotnet test --nologo"
```

Add the provided `templates/project-AGENTS.md` as `AGENTS.md` in the project and tailor only the project-specific rules.

## Add work

### CLI

```powershell
.\forge.ps1 task add `
  --project market-analyzer `
  --title "Create the Add-On shell" `
  --objective "Create the initial NinjaTrader Add-On shell and analyzer window. Do not add execution." `
  --acceptance "The project compiles using the documented workflow." `
  --acceptance "No live orders can be placed." `
  --validation "dotnet test --nologo" `
  --tag csharp `
  --tag ninjatrader
```

### Inbox

Copy `templates/task.yaml`, edit it, and place it in:

```text
E:\CodexData\inbox\
```

The daemon imports it automatically. Processed and failed files move into their own subdirectories.

## Autonomous task lifecycle

```text
READY
  -> isolated branch/worktree
  -> Codex implementation
  -> controller validation
  -> bounded repair loop
  -> independent read-only Codex review
  -> review repair loop
  -> final validation
  -> commit + patch + evidence
  -> COMPLETED
```

A task that needs a real product decision becomes `NEEDS_DECISION`; the daemon continues with other eligible work.

## Commands

```powershell
.\forge.ps1 doctor
.\forge.ps1 status
.\forge.ps1 status --json
.\forge.ps1 project list
.\forge.ps1 task list
.\forge.ps1 task list --state FAILED
.\forge.ps1 task retry TASK-001
.\forge.ps1 decision list
.\forge.ps1 run --once
.\forge.ps1 run --daemon
```

## Evidence layout

```text
E:\CodexData\evidence\<project>\<task>\attempt-01\
  implementation\
    implementation.events.jsonl
    implementation.final.json
    implementation.stderr.log
  validation-initial\
    validation-01.stdout.log
    validation-01.stderr.log
  review\
    review.events.jsonl
    review.final.json
  changes.patch
  task-result.json
```

Full logs remain on disk. Only bounded failure excerpts are returned to the next Codex repair run.

## Model routing

Defaults:

- High/critical implementation: `gpt-5.6-sol`
- Ordinary implementation: `gpt-5.6-terra`
- Ordinary review: `gpt-5.6-terra`
- High/critical review: `gpt-5.6-sol`

Routine controller inventory and validation are local, model-free operations. Follow-up reviews are scoped to prior findings, and the strongest model is reserved for high/critical risk escalation.

## Token controls

1. RTK is initialized globally for supported noisy output; known-unsafe filters use `rtk proxy` so exact results win over compression.
2. Raw controller logs are archived on disk instead of being pasted repeatedly into model context.
3. Validation summaries are capped while exact stdout/stderr remains recoverable.
4. Only task-relevant stored patterns are injected.
5. Each phase starts with a bounded contract rather than an accumulating conversational thread.
6. Independent review runs in a separate thread, protecting the implementation context.
7. Parallelism starts at two workers; raise it only after observing task quality and rate limits.

Token Optimizer is attempted during bootstrap. If the installed Codex version requires TUI installation, the marketplace is added and bootstrap prints the single remaining installation step. Codex Forge does not depend on the plugin to function.

## Safety boundary

The VPS is the isolation boundary. Within it, Codex receives unrestricted access and no approval prompts. Forge still instructs and structures tasks to prevent:

- introducing live broker connectivity, live credentials, order routing, withdrawals, or financial-account actions into simulation-only trading projects,
- production deployment,
- force-pushing or rewriting shared Git history,
- unrelated repository modification,
- infinite retries.

These are workflow policies, not interactive permission prompts.

## Updating

After this repository is hosted remotely, VPS updates are:

```powershell
.\update.ps1
```

That script performs a fast-forward-only `git pull` and reruns the idempotent bootstrap without replacing the scheduled task.

The bootstrap is idempotent: it refreshes the Python package and configuration without deleting the task database, evidence, repositories, or worktrees.

For a controlled service restart that also terminates a verified orphaned Forge daemon process tree, run:

```powershell
.\restart-service.ps1
```

## Current scope

This is a functional first release of the autonomous control plane. It is ready to onboard the NinjaTrader market-analyzer repository. Later improvements should be driven by measured runs: dashboard UI, richer dependency scheduling, task leases, notification delivery, and App Server/SDK integration.

## Repairing an interrupted bootstrap

Bootstrap is idempotent. If an earlier release stopped while creating the Python environment, replace the repository with the latest release and rerun:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\bootstrap.ps1 -SkipCodexLogin
```

The bootstrap now installs `setuptools` and `wheel` explicitly before installing Forge, checks every native command exit code, and only registers the startup task after initialization, tests, and lint validation succeed.

### Windows runtime compatibility in v0.1.3

Version 0.1.3 fixes two Windows-only bootstrap failures found on a clean VPS:

- Codex command shims and test Python scripts are launched through the correct Windows host instead of being passed directly to `CreateProcess` as Unix-style executables.
- Child validation shells automatically receive the Forge virtual environment's `Scripts` directory on `PATH`, so `python` resolves to the private Forge interpreter without requiring global Python or manual activation.
- `forge doctor` validates the interpreter actually running Forge rather than incorrectly requiring a separate global `python` command.

### Native exit-code compatibility in v0.1.4

Version 0.1.4 preserves the exact exit code returned by native validation commands executed through PowerShell. Windows PowerShell otherwise normalizes some nonzero native-process exits to `1`, which made a command returning `3` appear as `1` to Forge. The validation wrapper now captures `$LASTEXITCODE` immediately and exits PowerShell with that same value.
