# Autonomous project rules

## Operating mode

- Work autonomously. Do not ask whether to run commands, edit files, test, or continue.
- Make reasonable reversible decisions from repository conventions.
- Stop only for a genuine product decision, missing credential, paid action, or unavailable external runtime.

## Scope and safety

- Stay inside the assigned task and worktree.
- Do not access live brokerage credentials or place live trades.
- Do not deploy, publish releases, force-push, or rewrite shared Git history.
- Do not modify protected legacy paths unless the task explicitly allows them.

## Efficiency

- Search for symbols and small regions before opening entire files.
- Use `rg`, `fd`, `ast-grep`, and RTK-backed commands when available.
- Preserve exact compiler/test errors when debugging; summarize successful noisy output.
- Do not reread unchanged files without a concrete reason.

## Completion

- Run authoritative validation.
- Repair failures within scope.
- Report exact missing evidence when platform-only validation cannot run.
