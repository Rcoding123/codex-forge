# Contributing

Codex Forge is Windows-first infrastructure for autonomous engineering. Changes should be small, testable, non-interactive, and conservative about dependencies.

## Development setup

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\bootstrap.ps1 -SkipScheduledTask -SkipCodexLogin
```

If an environment already exists:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Required checks

The canonical completion gates from [AGENTS.md](AGENTS.md), run in full before requesting review:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

`pytest` includes release manifest integrity via `tests/test_manifest.py`, so a tracked-file change that has not been followed by [manifest regeneration](#release-manifest) fails the suite.

Use `rtk proxy pytest ...` rather than compressed RTK pytest output until the documented RTK 0.44.2 issue is revalidated after an upgrade.

## Release manifest

`MANIFEST.sha256` is generated, never hand-edited. After changing, adding or removing any tracked file, regenerate it and stage the result:

```powershell
git add <the files you changed>
powershell -File scripts/update-manifest.ps1
git add MANIFEST.sha256
```

The manifest covers **tracked files only**, so stage a new file before regenerating or it will be absent — this is what keeps unstaged scratch state out of a release manifest. The script writes LF and is deterministic: rerunning it with no source change leaves the file untouched, timestamp included.

## Change rules

- Preserve isolated Git worktrees for every controlled task.
- Keep SQLite and filesystem evidence authoritative; never depend on chat memory.
- Preserve exact command exit codes and full raw logs while bounding model-facing summaries.
- Keep execution non-interactive and do not weaken attempt, repair, timeout, token, Git, or evidence protections.
- Add a regression test for behavioral fixes and rerun the full suite.
- Keep runtime dependencies minimal and justify additions.
- Put project-specific behavior in the controlled project's `AGENTS.md`, not global Forge prompts.
- Do not add live brokerage, order routing, withdrawals, production deployment, or credential access.
- Do not commit local configuration, databases, logs, sessions, worktrees, evidence, or Codex authentication.

## Commits and pull requests

Use focused commits with an imperative summary. Describe the demonstrated problem, the invariant being restored, validation performed, and any migration or operational impact. Avoid generated artifacts and machine-specific paths. Security-sensitive changes should follow [SECURITY.md](SECURITY.md).

Before requesting review, regenerate the release manifest if tracked files changed and confirm `git status --ignored --short` contains only expected local state.
