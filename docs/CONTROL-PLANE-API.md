# Control-plane JSON contract

Codex Forge exposes a versioned JSON contract for external applications that need to inspect
controller state and submit work. External applications must use these CLI commands instead of
reading `forge.db`, depending on SQLite table details, or parsing Forge's human-readable terminal
output.

This contract covers inspection and task submission. It does not provide retry, cancellation,
reconciliation, merge, push, daemon control, HTTP, or named-pipe operations, and it never starts
task execution — a submitted task waits for the existing controller loop.

## Supported commands

```powershell
.\forge.ps1 status --json
.\forge.ps1 project list --json
.\forge.ps1 task list --json
.\forge.ps1 task list --state FAILED --json
.\forge.ps1 task show <task-id> --json
.\forge.ps1 decision list --json
.\forge.ps1 task add <task arguments> --dry-run --json
.\forge.ps1 task add <task arguments> --json
```

The same commands retain their existing human-readable output when `--json` is absent. `task add`
without `--json` and without `--dry-run` behaves exactly as it always has, including its verbatim
handling of the submitted text and its `Added <id> [<state>] <title>` line.

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
- `command` is the stable dotted command name: `status`, `project.list`, `task.list`, `task.show`,
  `decision.list`, or `task.add`. Validation and submission share the `task.add` command name and are
  distinguished by `data.dry_run`.
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

## Task validation and submission

`task add` accepts the same arguments in all three modes:

| Mode | Invocation | Writes to the database |
|---|---|---|
| Human | `task add ...` | Yes |
| Validation | `task add ... --dry-run --json` | No |
| Submission | `task add ... --json` | Yes, at most once per `task_id` |

Validation resolves everything submission resolves — the project, the task identifier, the
normalized request, the applied defaults, and any conflict with an existing task — and then stops
without writing. A request that validates cleanly is a request that submission will accept, barring
a concurrent change to controller state between the two calls.

`--dry-run` may also be used without `--json`; it then prints a single human line and writes
nothing. Both `--dry-run` and `--json` apply the normalization and validation rules below, which are
stricter than the historical human-readable path.

### Normalization

Submitted text is normalized before it is stored or hashed, so that a client retry that differs only
in insignificant ways is recognized as the same request:

- Text is Unicode NFC-normalized and stripped of leading and trailing whitespace.
- List values (acceptance criteria, validation commands, tags, dependencies) are normalized
  element-wise and de-duplicated, preserving first-occurrence order.
- `risk` is lowercased.
- Text that renders as blank — empty, whitespace only, or composed only of invisible formatting
  characters — is rejected rather than stored.
- Control characters and bidirectional override characters are rejected in every field.

`data.normalized_request` reports exactly what Forge resolved. `data.defaults_applied` reports the
values Forge resolved for fields the client did not supply, and always includes the derived `state`
and the configured `token_limit`.

### Task identifiers

`--id` is optional. When it is omitted, Forge derives a stable identifier from the project,
title, and objective, and reports `normalized_request.task_id_source` as `derived`; when it is
supplied, the source is `client`.

A client-supplied identifier must be 1–64 characters, must contain at least one letter or digit,
must not contain whitespace, control characters, `..`, path separators, or the Windows-reserved
characters `\ / : " | ? *`, and must not begin with `-` or `.`. Entries in `dependencies` name other
tasks and must satisfy the same rules. Forge does not require a dependency to exist yet; a task with
dependencies is created in `BACKLOG` and becomes `READY` once every dependency completes.

### Idempotency

Submission is safe to retry. Forge computes a fingerprint over the material content of a request —
project, title, objective, acceptance criteria, validation commands, tags, dependencies, risk,
priority, and maximum attempts — and compares it against any task already stored under the same
identifier.

| Situation | Result |
|---|---|
| The identifier is unused | The task is created and `data.created` is `true`. |
| The identifier holds a task with the same fingerprint | No write occurs, the existing task is returned, and `data.created` is `false`. |
| The identifier was supplied by the client and holds a different request | `TASK_ID_CONFLICT` |
| The identifier was derived and holds a different request | `DUPLICATE_TASK_ID` |

Order matters for acceptance criteria and validation commands, because validation commands run in
the order given and stop at the first failure. Tags and dependencies are unordered, so resubmitting
them in a different order is still the same request.

A client that retries after a timeout, a dropped connection, or an ambiguous failure will never
create a duplicate task: it receives either the task it created or a conflict, never a second task.
Retrying with an explicit `task_id` is the recommended pattern, because a derived identifier changes
whenever the title or objective changes.

The fingerprint is computed from persisted task fields. It is not stored, so it never becomes stale
relative to the task it describes.

### Validation commands

A submitted validation command must name one command. It must be 1–500 characters on a single line
and must not contain `;`, `|`, `&`, `` ` ``, `<`, `>`, or `$(`. Commands that chain, redirect, or
substitute other commands are rejected with `INVALID_VALIDATION_COMMAND`; the error details report
the position of the offending entry and never echo the command text.

This restriction applies to commands submitted through this contract. Validation commands already
registered on a project are unaffected.

### Validation response

```json
{
  "schema_version": "1.0",
  "command": "task.add",
  "success": true,
  "data": {
    "dry_run": true,
    "created": false,
    "would_create": true,
    "task": {
      "task_id": "TASK-001",
      "project": "sample-service",
      "project_id": "project-a1b2c3",
      "title": "Add deterministic report export",
      "objective": "Implement CSV export for the existing report model.",
      "state": "READY",
      "risk": "medium",
      "priority": 100,
      "attempt_count": 0,
      "max_attempts": 3,
      "acceptance_criteria": ["Existing and new tests pass."],
      "validation_commands": ["python -m pytest"],
      "tags": [],
      "dependencies": [],
      "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
      "token_limit": 2000000,
      "branch": null,
      "worktree": null,
      "planned_branch": "forge/task-001-add-deterministic-report-export",
      "planned_worktree": "D:\\CodexForge\\worktrees\\sample-service\\task-001",
      "current_phase": null,
      "error_code": null,
      "error_message": null,
      "evidence_path": null,
      "created_at": null,
      "updated_at": null,
      "started_at": null,
      "completed_at": null
    },
    "normalized_request": {
      "task_id": "TASK-001",
      "task_id_source": "client",
      "project": "sample-service",
      "project_id": "project-a1b2c3",
      "title": "Add deterministic report export",
      "objective": "Implement CSV export for the existing report model.",
      "acceptance_criteria": ["Existing and new tests pass."],
      "validation_commands": ["python -m pytest"],
      "tags": [],
      "dependencies": [],
      "risk": "medium",
      "priority": 100,
      "max_attempts": 3
    },
    "defaults_applied": {
      "state": "READY",
      "token_limit": 2000000,
      "risk": "medium",
      "priority": 100,
      "max_attempts": 3,
      "tags": [],
      "dependencies": []
    }
  },
  "error": null
}
```

### Submission response

A successful submission returns the same shape with `dry_run` and `would_create` set to `false`,
`created` reporting whether this call created the task, and a persisted `task` object carrying
`created_at` and `updated_at`.

```json
{
  "schema_version": "1.0",
  "command": "task.add",
  "success": true,
  "data": {
    "dry_run": false,
    "created": true,
    "would_create": false,
    "task": {
      "task_id": "TASK-001",
      "project": "sample-service",
      "title": "Add deterministic report export",
      "objective": "Implement CSV export for the existing report model.",
      "state": "READY",
      "risk": "medium",
      "max_attempts": 3,
      "token_limit": 2000000,
      "acceptance_criteria": ["Existing and new tests pass."],
      "validation_commands": ["python -m pytest"],
      "branch": null,
      "worktree": null,
      "planned_branch": "forge/task-001-add-deterministic-report-export",
      "planned_worktree": "D:\\CodexForge\\worktrees\\sample-service\\task-001",
      "created_at": "2026-08-04T14:10:00+00:00"
    },
    "normalized_request": {},
    "defaults_applied": {}
  },
  "error": null
}
```

Field notes:

- `data.created` is `true` only when this call inserted the task. A retry of an identical request
  returns `false` with the same `task_id` and `created_at`.
- The `task` object is the `task show` object with `acceptance_criteria`, `validation_commands`,
  `tags`, `dependencies`, `priority`, `planned_branch`, and `planned_worktree` added. Every field
  documented for `task show` is present and carries the same meaning.
- `branch` and `worktree` report persisted state and are therefore `null` until the controller
  prepares the isolated worktree. `planned_branch` and `planned_worktree` are the deterministic
  values the controller will use. Submission never creates a branch, a worktree, or a directory.
- `state` is `READY`, or `BACKLOG` when the request declares dependencies. Submission does not run
  the task; the existing controller loop picks it up.
- `token_limit` is the active configured task limit, not a per-task value. A configured limit of `0`
  means the controller does not enforce a token budget.

### Submission failures

```json
{
  "schema_version": "1.0",
  "command": "task.add",
  "success": false,
  "data": null,
  "error": {
    "code": "TASK_ID_CONFLICT",
    "message": "The task_id already identifies a different task request. Submit a new task_id or resubmit the original request unchanged.",
    "details": {
      "task_id": "TASK-001"
    }
  }
}
```

A failed submission never writes a partial task.

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

This response exits with code `2`. Stable current error codes are:

| Code | Exit | Meaning |
|---|---:|---|
| `TASK_NOT_FOUND` | 2 | The requested task does not exist. |
| `INVALID_ARGUMENT` | 2 | The command line could not be parsed — a missing, unknown, or unparseable argument. |
| `CONFIG_ERROR` | 2 | `forge.toml` could not be loaded. |
| `COMMAND_FAILED` | 1 | An unexpected failure occurred while running the command. |
| `INVALID_TASK_REQUEST` | 2 | A submitted field was semantically invalid: empty, too long, malformed, or carrying rejected characters. |
| `PROJECT_NOT_FOUND` | 2 | The requested project is not registered. |
| `DUPLICATE_TASK_ID` | 2 | The derived task identifier already holds a different request. |
| `TASK_ID_CONFLICT` | 2 | The client-supplied task identifier already holds a different request. |
| `INVALID_VALIDATION_COMMAND` | 2 | A submitted validation command was empty, too long, multi-line, or attempted to chain, redirect, or substitute commands. |
| `INVALID_LIMIT` | 2 | `max_attempts` or `priority` was not an integer within its supported range. |
| `CONFIGURATION_ERROR` | 2 | Forge is configured but not initialized. Run `forge init`. |
| `DATABASE_ERROR` | 1 | The Forge database could not be read or written. |

`INVALID_ARGUMENT` covers failures the argument parser rejects before Forge sees the request;
`INVALID_TASK_REQUEST` covers values that parse but are not acceptable. More codes may be added
compatibly, so consumers must treat an unrecognized code as a failure rather than an error.

Error `details` are structured and safe to log. They identify the offending field, index, or
identifier, and never contain submitted command text, database messages, file-system paths, or
Python exception text.

## Security and compatibility expectations

The contract is an explicit allowlist. It excludes credentials, API keys, authentication-file
contents, cookies, tokens, full environment-assignment values, raw sensitive subprocess output,
failure fingerprints, model event streams, and private database implementation details. Free-form
public task and decision fields are returned as persisted; operators must not place secrets in titles,
objectives, questions, or options.

Validation commands are redacted on the way out, in both `task.validation_commands` and
`normalized_request.validation_commands`. Redaction is a presentation concern only — the controller
still runs the command exactly as submitted — so a redacted response cannot be resubmitted verbatim
as an identical request.

Submission is the only write this contract performs. It inserts one task row and nothing else: it
does not fetch, clone, create branches or worktrees, run validation commands, start the controller,
or touch the file system. Both submission and validation refuse to run against a database that does
not exist yet, reporting `CONFIGURATION_ERROR` rather than creating one, so a validation call leaves
no trace whatsoever.

The SQLite schema is an internal persistence format and may change independently of this contract.
Reading `forge.db` directly can bypass redaction, couple an application to private fields, and produce
inconsistent reads. External applications—including Shared Chat integrations—must invoke the
documented `--json` commands and must not open `forge.db` directly.
