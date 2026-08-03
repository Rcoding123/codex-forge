# Autonomous trading-tool project rules

## Operating mode

- Work autonomously. Do not ask whether to run commands, edit files, compile, test, launch approved development tools, inspect logs, or continue.
- Make reasonable, reversible engineering decisions from repository conventions and record assumptions.
- Stop only for a genuine product decision, missing required development credential, paid action, or unavailable external runtime.

## Environment boundary

- This VPS is exclusively for software/tool development and simulated testing.
- NinjaTrader Simulation, Market Replay/Playback, historical data, recorded Level I/II data, mock services, and offline test harnesses are authorized.
- There is no live broker connection, live trading credential, or order-routing service on this VPS.
- Do not introduce live broker connectivity, live account authentication, or live order routing unless a future task explicitly changes this boundary.
- Analyzer components should remain read-only unless a task specifically requires simulated order behavior.

## Scope and repository safety

- Stay inside the assigned task and Git worktree.
- Do not deploy or publish releases, force-push, or rewrite shared Git history.
- Do not modify protected legacy paths unless the task explicitly allows them.
- Preserve existing NinjaTrader/NinjaScript behavior unless the task explicitly changes it.

## Validation

- Use deterministic unit and integration tests where possible.
- Use NinjaTrader compilation, Simulation, Market Replay/Playback, and captured runtime logs as authoritative platform evidence.
- Never claim platform behavior was validated when only offline tests ran; report the exact missing runtime evidence.
- Repair failures within scope and continue automatically through bounded retry loops.

## Efficiency

- Search for symbols and small regions before opening entire files.
- Use `rg`, `fd`, `ast-grep`, and RTK-backed commands when available.
- Preserve exact compiler and runtime errors when debugging; summarize successful noisy output.
- Do not reread unchanged files without a concrete reason.
