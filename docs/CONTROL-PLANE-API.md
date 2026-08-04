# Control-plane JSON contract

Codex Forge exposes a versioned, read-only JSON contract for external applications that need to
inspect controller state. External applications must use these CLI commands instead of reading
`forge.db`, depending on SQLite table details, or parsing Forge's human-readable terminal output.

This checkpoint is inspection-only. It does not provide task submission, retry, cancellation,
reconciliation, merge, daemon control, HTTP, or named-pipe operations.

## Supported commands

```powershell
.\forge.ps1 status --json
.\forge.ps1 project list --json
.\forge.ps1 task list --json
.\forge.ps1 task list --state FAILED --json
.\forge.ps1 task show <task-id> --json
.\forge.ps1 decision list --json
```

The same commands retain their existing human-readable output when `--json` is absent. `task show`
is new in this checkpoint and supports both a concise human form and the JSON contract.

## Envelope

Every JSON response is one UTF-8 JSON document with this envelope:

```json
{
  "schema_version": "1.0",
  "command": "task.list",
  "success": true,
  "data": {},
  "error": null
}
```

- `schema_version` identifies the public contract, not the Forge package version or database schema.
- `command` is the stable dotted command name: `status`, `project.list`, `task.list`, `task.show`, or
  `decision.list`.
- `success` is `true` only when the operation succeeded.
- `data` contains command data on success and is `null` on failure.
- `error` is `null` on success and contains a structured error on failure.

Consumers must ignore unknown fields. Object member order and whitespace are not contractually
significant.

### Schema versioning policy

The version uses `major.minor` numbering.

- Additive fields and new enum values may be introduced in a later `1.x` version. Existing fields will
  retain their meaning and type within major version 1.
- Removing or renaming a field, changing its type or established meaning, or otherwise requiring a
  consumer update requires a new major version.
- Optional information remains present as JSON `null` when Forge does not persist a value. Consumers
  must not infer a value from a missing operational artifact.
- New read-only commands may be added without changing existing command payloads.

## Output streams and exit codes

With `--json`, stdout contains exactly one JSON document. Forge does not write banners, progress text,
warnings, tables, or a second JSON value to stdout. Operational diagnostics may be written to stderr;
consumers must parse stdout independently.

Exit codes are:

| Exit code | Meaning |
|---:|---|
| `0` | The command succeeded and `success` is `true`. |
| `1` | An operational command failure occurred and `success` is `false`. |
| `2` | Invocation, configuration, or requested-resource failure and `success` is `false`. |

Do not treat valid JSON alone as success. Check both the process exit code and `success`.

## Status

`forge status --json` retains the pre-contract `tasks`, `open_decisions`, and `database` top-level
fields. The same values also appear under `data`; the envelope fields are additive.

```json
{
  "schema_version": "1.0",
  "command": "status",
  "success": true,
  "data": {
    "tasks": {
      "BACKLOG": 0,
      "READY": 2,
      "IMPLEMENTING": 0,
      "VALIDATING": 0,
      "REVIEWING": 0,
      "REPAIRING": 0,
      "COMPLETED": 8,
      "BLOCKED": 0,
      "FAILED": 1,
      "NEEDS_DECISION": 0
    },
    "open_decisions": 0,
    "database": "C:\\CodexData\\forge.db"
  },
  "error": null,
  "tasks": {
    "BACKLOG": 0,
    "READY": 2,
    "IMPLEMENTING": 0,
    "VALIDATING": 0,
    "REVIEWING": 0,
    "REPAIRING": 0,
    "COMPLETED": 8,
    "BLOCKED": 0,
    "FAILED": 1,
    "NEEDS_DECISION": 0
  },
  "open_decisions": 0,
  "database": "C:\\CodexData\\forge.db"
}
```

Task-state count keys are always present, including zero counts.

## Project list

```json
{
  "schema_version": "1.0",
  "command": "project.list",
  "success": true,
  "data": {
    "projects": [
      {
        "project_id": "project-a1b2c3",
        "name": "sample-service",
        "repository_path": "D:\\CodexForge\\repositories\\sample-service",
        "default_branch": "main",
        "validation_commands": ["python -m pytest"],
        "fetch_policy": "automatic",
        "commit_policy": "automatic",
        "push_policy": "disabled",
        "created_at": "2026-08-04T14:10:00+00:00",
        "updated_at": "2026-08-04T14:10:00+00:00"
      }
    ]
  },
  "error": null
}
```

Policy values are `automatic` or `disabled`. Validation commands are returned for inspection, but
Forge replaces environment-assignment values, recognized credential-argument values, and URL
credentials with `[REDACTED]`.

## Task list and task show

`task list` returns `data.tasks` plus the applied `state` filter. A missing filter is represented by
`null`. `task show` returns the same task object under `data.task`.

```json
{
  "schema_version": "1.0",
  "command": "task.show",
  "success": true,
  "data": {
    "task": {
      "task_id": "TASK-001",
      "project": "sample-service",
      "project_id": "project-a1b2c3",
      "title": "Add deterministic report export",
      "objective": "Implement CSV export for the existing report model.",
      "state": "VALIDATING",
      "risk": "medium",
      "attempt_count": 1,
      "max_attempts": 3,
      "token_usage": {
        "input_tokens": 1200,
        "output_tokens": 300,
        "total_tokens": 1500
      },
      "token_limit": 2000000,
      "branch": "forge/task-001",
      "worktree": "D:\\CodexForge\\worktrees\\sample-service\\TASK-001",
      "current_phase": "implementation",
      "error_code": null,
      "error_message": null,
      "evidence_path": "C:\\CodexData\\evidence\\sample-service\\TASK-001\\attempt-01\\implementation",
      "created_at": "2026-08-04T14:10:00+00:00",
      "updated_at": "2026-08-04T14:12:00+00:00",
      "started_at": "2026-08-04T14:11:00+00:00",
      "completed_at": null
    }
  },
  "error": null
}
```

Field notes:

- `project` is the registered project name. It is `null` only if durable task state references a
  missing project.
- `token_usage` totals completed persisted run records; `token_limit` is the active configured task
  limit.
- `current_phase` and `evidence_path` are taken from the latest persisted run record. They are not
  synthesized from task state.
- `started_at` is the first persisted run start. `completed_at` is present only when Forge persisted
  task completion in the task result.
- Raw result objects, failure fingerprints, run thread IDs, authentication data, and raw subprocess
  output are not exposed. Known controller error markers receive stable safe codes and messages; other
  stored error text is represented as `TASK_ERROR` without returning the raw text.

## Decision list

The current command lists open decisions, preserving existing CLI semantics.

```json
{
  "schema_version": "1.0",
  "command": "decision.list",
  "success": true,
  "data": {
    "decisions": [
      {
        "decision_id": 12,
        "task_id": "TASK-001",
        "state": "OPEN",
        "question": "Which export delimiter is required?",
        "reason": null,
        "options": ["Comma", "Tab"],
        "recommendation": "Comma",
        "created_at": "2026-08-04T14:13:00+00:00",
        "resolved_at": null,
        "resolution": null
      }
    ]
  },
  "error": null
}
```

Decision resolution is not part of this checkpoint. `resolved_at` and `resolution` therefore remain
`null` for the open collection.

## Errors

Expected and unexpected failures use the same envelope. Python tracebacks are never included in the
JSON response.

```json
{
  "schema_version": "1.0",
  "command": "task.show",
  "success": false,
  "data": null,
  "error": {
    "code": "TASK_NOT_FOUND",
    "message": "Task was not found.",
    "details": {
      "task_id": "TASK-missing"
    }
  }
}
```

This response exits with code `2`. Stable current error codes are `TASK_NOT_FOUND`,
`INVALID_ARGUMENT`, `CONFIG_ERROR`, and `COMMAND_FAILED`. More codes may be added compatibly.

## Security and compatibility expectations

The contract is an explicit allowlist. It excludes credentials, API keys, authentication-file
contents, cookies, tokens, full environment-assignment values, raw sensitive subprocess output,
failure fingerprints, model event streams, and private database implementation details. Free-form
public task and decision fields are returned as persisted; operators must not place secrets in titles,
objectives, questions, or options.

The SQLite schema is an internal persistence format and may change independently of this contract.
Reading `forge.db` directly can bypass redaction, couple an application to private fields, and produce
inconsistent reads. External applications—including Shared Chat integrations—must invoke the
documented `--json` commands and must not open `forge.db` directly.
