# Claude Code — repository entrypoint

`AGENTS.md` is the authoritative Forge engineering contract for this
repository. Read it before any repository work. If instructions conflict,
`AGENTS.md` wins for Forge repo engineering law.

- Code, schemas, and tests outrank all descriptive documentation.
- Deeper design: `docs/ARCHITECTURE.md` and `docs/CONTROL-PLANE-API.md`.
- Durable engineering lessons: `docs/ENGINEERING_LESSONS.md`. Read it before
  architecture or implementation work; `AGENTS.md` carries the read order.
- Navigation: `docs/AGENT_CONTEXT.md` maps subsystems to owning files. It is
  an index with no authority. In a continuing session, do not reread the
  large authority documents in full when already read and unchanged; consult
  the index and open only task-relevant sections and files.

Canonical completion gates:

    python -m pytest
    python -m ruff check .
    python -m compileall -q src tests
    git diff --check

Development uses `agent/*` branches plus a PR; a human merges. Never push
directly to `main`.
