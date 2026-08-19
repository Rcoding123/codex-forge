# Claude Code — repository entrypoint

`AGENTS.md` is the authoritative Forge engineering contract for this
repository. Read it before any repository work. If instructions conflict,
`AGENTS.md` wins for Forge repo engineering law.

- Code, schemas, and tests outrank all descriptive documentation.
- Deeper design: `docs/ARCHITECTURE.md` and `docs/CONTROL-PLANE-API.md`.

Canonical completion gates:

    python -m pytest
    python -m ruff check .
    python -m compileall -q src tests
    git diff --check

Development uses `agent/*` branches plus a PR; a human merges. Never push
directly to `main`.
