# Codex Forge repository instructions

- This repository is the autonomous controller, not a project being controlled by it.
- Keep runtime dependencies minimal and Windows-first behavior intact.
- Never weaken the invariant that tasks run in isolated Git worktrees.
- Simulation, Market Replay/Playback, historical-data recording, GUI automation, and local test execution are allowed.
- Never add live broker connectivity, live credentials, order routing, withdrawals, or production-deployment capabilities.
- Controller state must be durable in SQLite and evidence files; do not rely on chat memory.
- All command execution must remain non-interactive.
- Preserve raw logs on disk, but keep model-facing summaries bounded.
- Use RTK for supported noisy commands, but fall back to the exact direct read-only command if RTK rejects it.
- With RTK 0.44.2, run pytest through `rtk proxy pytest ...`; do not trust compressed passing-suite output.
- Add or update tests for controller behavior.
- Run `python -m pytest` and `python -m ruff check .` before completion.
