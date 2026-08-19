# Control-plane JSON contract

Codex Forge exposes a versioned JSON contract for external applications that need to inspect
controller state and submit work. External applications must use these CLI commands instead of
reading `forge.db`, depending on SQLite table details, or parsing Forge's human-readable terminal
output.

This contract covers inspection, task submission, and deletion of non-running tasks and empty
projects. It does not provide retry, cancellation,
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
.\forge.ps1 task evidence list <task-id> --json
.\forge.ps1 task evidence show <task-id> --artifact-id <artifact-id> --json
.\forge.ps1 task delete <task-id> --json
.\forge.ps1 project delete <project> --json
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
  `decision.list`, `task.add`, `task.evidence.list`, `task.evidence.show`, `task.delete`, or
  `project.delete`. Validation and submission share the `task.add` command name and are
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

Version history:

| Version | Change |
|---|---|
| `1.0` | Initial contract. The default. |
| `1.1` | `task.token_usage` gained additive reported token categories. Every `1.0` field kept its name, type, meaning, and value. Opt-in. |

**`1.0` is the default and stays the default.** Every command accepts `--schema-version`; when it is
omitted the response declares `1.0` and contains exactly the `1.0` payload. A newer version is served
only to a caller that asks for it by name, because an installed consumer validates the version it was
built against and would reject a document announcing a version it does not know — even though every
added field is additive. Upgrading Forge therefore never changes what an existing consumer receives.

A response always declares the version it was asked for, and its payload always matches that version:
a `1.0` response never carries `1.1` fields. An unsupported version is refused with
`UNSUPPORTED_SCHEMA` and exit code `2` before the command runs; it is never silently downgraded to
the default.

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
  limit. The categories are described below.
- `current_phase` and `evidence_path` are taken from the latest persisted run record. They are not
  synthesized from task state.
- `started_at` is the first persisted run start. `completed_at` is present only when Forge persisted
  task completion in the task result.
- Raw result objects, failure fingerprints, run thread IDs, authentication data, and raw subprocess
  output are not exposed. Known controller error markers receive stable safe codes and messages; other
  stored error text is represented as `TASK_ERROR` without returning the raw text.

### Token usage categories

`token_usage` totals every completed run of the task. Its shape is the one the declared schema
version defines.

Schema `1.0` — the default, and what every command emits unless `--schema-version 1.1` is passed:

```json
"token_usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}
```

Schema `1.1` — requested explicitly, adds the categories the worker reported:

```json
"token_usage": {
  "input_tokens": 1200,
  "cached_input_tokens": 900,
  "fresh_input_tokens": 300,
  "cache_write_input_tokens": 0,
  "output_tokens": 300,
  "reasoning_output_tokens": 120,
  "raw_total_tokens": 1500,
  "total_tokens": 1500,
  "cache_hit_ratio": 0.75,
  "accounting_basis": "reported_token_categories",
  "budget_basis": "fresh_input_plus_output"
}
```

Schema 1.1 is an accounting view, not a billing view: it contains no price, no universal billable-
token value, and no cache discount, because those depend on the model, plan, and date and none of
them are observable from a persisted run record.

| Field | Version | Type | Meaning |
|---|---|---|---|
| `input_tokens` | 1.0 | integer | Reported input tokens. Includes the cached portion. |
| `output_tokens` | 1.0 | integer | Reported output tokens. Includes the reasoning portion. |
| `total_tokens` | 1.0 | integer | `sum(input_tokens + output_tokens)`. Raw traffic, informational. Unchanged meaning and value. |
| `cached_input_tokens` | 1.1 | integer | The subset of `input_tokens` served from the prompt cache. |
| `fresh_input_tokens` | 1.1 | integer | `sum(max(0, input_tokens - cached_input_tokens))` per run. |
| `cache_write_input_tokens` | 1.1 | integer | Reported input tokens written into the cache. |
| `reasoning_output_tokens` | 1.1 | integer | The subset of `output_tokens` spent on reasoning. |
| `raw_total_tokens` | 1.1 | integer | `sum(input_tokens + output_tokens)`, always equal to `total_tokens`. Traffic, not budget. |
| `cache_hit_ratio` | 1.1 | number | `cached_input_tokens / input_tokens`, `0.0` when no input was reported, always within `[0.0, 1.0]`. |
| `accounting_basis` | 1.1 | string | `"reported_token_categories"`. |
| `budget_basis` | 1.1 | string | `"fresh_input_plus_output"` — what `token_limit` is compared against. |

`cached_input_tokens` and `reasoning_output_tokens` are subtotals of `input_tokens` and
`output_tokens`, so neither is ever added into a total; adding them would double-count. Only
`input_tokens` and `output_tokens` are summed into `total_tokens` and `raw_total_tokens`, and both
versions report the same `input_tokens`, `output_tokens`, and `total_tokens` for the same task — 1.1
adds detail, it does not restate the total.

Those raw sums are traffic, not budget. `token_limit` is compared against new work only —
`fresh_input_tokens + output_tokens`, as `budget_basis` states. A cached input read consumes zero
task budget: it is inside `input_tokens` and outside `fresh_input_tokens`. `reasoning_output_tokens`
is not added on top of `output_tokens`, and `cache_write_input_tokens` is not added on top of the
fresh-input term, because the reported contract already carries it inside `input_tokens`.

Values a run reported as missing, null, negative, boolean, non-numeric, or otherwise unusable are
counted as `0`. No category and no derived field can be negative, and an unrecognized member of a
persisted usage record is ignored rather than added to a total.

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
- List values (acceptance criteria, architecture decisions, constraints, validation commands, tags,
  dependencies) are normalized element-wise and de-duplicated, preserving first-occurrence order.
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
characters `\ / : " | ? *`, must not begin with `-` or `.`, must not end with `.` (Windows strips
trailing dots, so `TASK-1.` would alias `TASK-1`'s directories), and must not be a reserved
Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, with or without an
extension). The same rules are enforced on the plain `task add` path, not only on `--json`
submissions, and one shared Windows-safe component validator applies these rules to task ids and
project names alike. Entries in `dependencies` name other tasks and must satisfy the same rules. Forge does not require a dependency to exist yet; a task with
dependencies is created in `BACKLOG` and becomes `READY` once every dependency completes.

### Specification contract fields

Beyond the objective and acceptance criteria, a submission may carry the rest of the
implementation-binding specification:

- `--decision` (repeatable) — an architecture decision. Binding: implementation must satisfy it
  exactly as written, and the independent review must confirm each one verbatim before the task
  can complete.
- `--constraint` (repeatable) — a constraint, with the same binding force as a decision.
- `--scope` (single value) — what the task may change.
- `--out-of-scope` (single value) — what the task must not touch.

All four are optional. Decisions and constraints follow the acceptance-criteria limits (up to 50
entries of up to 1,000 characters); scope and out-of-scope follow the objective limit (20,000
characters, multi-line). The text is stored, fingerprinted, and rendered into worker prompts
verbatim — never summarized. Decisions and constraints are not copied into acceptance criteria:
acceptance criteria state what must be demonstrably true, while decisions and constraints bind how
the work is done. Tasks submitted before these fields existed remain readable and behave exactly
as before.

### Idempotency

Submission is safe to retry. Forge computes a fingerprint over the material content of a request —
project, title, objective, acceptance criteria, architecture decisions, constraints, scope,
out-of-scope, validation commands, tags, dependencies, risk, priority, and maximum attempts — and
compares it against any task already stored under the same identifier.

| Situation | Result |
|---|---|
| The identifier is unused | The task is created and `data.created` is `true`. |
| The identifier holds a task with the same fingerprint | No write occurs, the existing task is returned, and `data.created` is `false`. |
| The identifier was supplied by the client and holds a different request | `TASK_ID_CONFLICT` |
| The identifier was derived and holds a different request | `DUPLICATE_TASK_ID` |
| The identifier names retained evidence of a deleted task, or differs only by case from a live task in the same project | `TASK_ID_COLLISION` |
| The target project's stored name is not a safe path component, or another registered project aliases its Windows physical name | `PROJECT_UNSAFE` |

The collision check shares one immediate transaction with the insert, so two concurrent
submissions with case-variant identifiers cannot both pass the check and both persist. The same
transaction re-reads the target project's registration and refuses with `PROJECT_UNSAFE` when
its workspace and evidence namespaces cannot be proven safe — a legacy path-shaped stored name,
or a second registered project whose name aliases the same Windows physical directory. The guard
runs on every submission path: the inbox importer, plain `task add`, and JSON submission,
including dry-run previews. Its details carry only backend-minted project ids, never a
path-shaped stored name.

Order matters for acceptance criteria, architecture decisions, constraints, and validation
commands, because validation commands run in the order given and stop at the first failure, and
the binding fields are contract text whose identity must never drift. Tags and dependencies are
unordered, so resubmitting them in a different order is still the same request.

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
      "architecture_decisions": ["Report rows are ordered by report id."],
      "constraints": ["Python standard library only."],
      "scope": "The report export path.",
      "out_of_scope": "No changes to report ingestion.",
      "validation_commands": ["python -m pytest"],
      "tags": [],
      "dependencies": [],
      "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
      "token_limit": 2000000,
      "branch": null,
      "worktree": null,
      "planned_branch": "forge/task-001-add-deterministic-report-export",
      "planned_worktree": "D:\\CodexForge\\worktrees\\sample-service\\task-001-a3b4d261fc",
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
      "architecture_decisions": ["Report rows are ordered by report id."],
      "constraints": ["Python standard library only."],
      "scope": "The report export path.",
      "out_of_scope": "No changes to report ingestion.",
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
      "planned_worktree": "D:\\CodexForge\\worktrees\\sample-service\\task-001-a3b4d261fc",
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
- The `task` object is the `task show` object with `acceptance_criteria`, `architecture_decisions`,
  `constraints`, `scope`, `out_of_scope`, `validation_commands`, `tags`, `dependencies`,
  `priority`, `planned_branch`, and `planned_worktree` added. Every field documented for
  `task show` is present and carries the same meaning.
- `branch` and `worktree` report persisted state and are therefore `null` until the controller
  prepares the isolated worktree. `planned_branch` and `planned_worktree` are the deterministic
  values the controller will use. The worktree directory name folds a hash of the exact task id
  into the lossy slug, so two distinct ids can never derive one checkout directory. Submission
  never creates a branch, a worktree, or a directory.
- `state` is `READY`, or `BACKLOG` when the request declares dependencies. Submission does not run
  the task; the existing controller loop picks it up.
- `token_limit` is the active configured task limit, not a per-task value. A configured limit of `0`
  means the controller does not enforce a token budget. The limit is compared against
  `token_usage.fresh_input_tokens + token_usage.output_tokens`, as `token_usage.budget_basis`
  states — not against `token_usage.total_tokens`, which is raw traffic. The controller checks that
  quantity at phase boundaries, so one in-flight phase can carry a task past the limit before the
  next check stops it.

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

## Task delete

`task delete <task-id>` removes one non-running task and every registry row it owns (its runs and
decisions) in a single transaction. The task identifier must match exactly; there is no prefix or
fuzzy matching. A task in an active execution state (`IMPLEMENTING`, `VALIDATING`, `REVIEWING`,
`REPAIRING`) is refused with `TASK_ACTIVE`; deletion is not cancellation. Retained evidence and the
task's branch are never deleted. The task's disposable worktree checkout is removed after the
database deletion commits.

```json
{
  "schema_version": "1.0",
  "command": "task.delete",
  "success": true,
  "data": {
    "deleted": true,
    "task_id": "TASK-1042",
    "state": "FAILED",
    "evidence_retained": true,
    "evidence_path": "C:\\forge\\evidence\\demo\\TASK-1042",
    "worktree_removed": true
  },
  "error": null
}
```

- `state` is the state the task held at the moment it was deleted.
- `evidence_retained` is always `true`: deletion never touches the evidence tree. `evidence_path`
  is the retained evidence root of the deleted task, or `null` when it cannot be derived. Deletion
  removes registry state only: `task evidence list` and `task evidence show` keep serving the
  retained artifacts under `evidence_path`, reporting `task_registered: false` with `task_state`
  and `attempt_count` `null`. If retained evidence for the same task id exists under more than one
  project, the evidence commands fail closed with `EVIDENCE_AMBIGUOUS` rather than guessing.
- `worktree_removed` is `true` only when both the checkout and any Git registration are
  verifiably gone, and `false` when there was nothing on disk to remove — or when, by the time
  the post-commit cleanup ran, the task id or the checkout path (persisted or derived) was owned
  by a live task row again. The removal runs under a registry write claim, so a reused identity
  always keeps its worktree. Cleanup only ever deletes a proven disposable checkout: a plain
  directory that resolves strictly inside the deleted task's own project workspace directory and
  overlaps — by equality, ancestry, descent, or resolved alias under Windows-normalized canonical
  comparison — nothing in the protected set: every registered repository, every other project's
  workspace, every live task's effective checkout, and the Forge configuration roots (evidence,
  database, repositories, inbox, knowledge, and the configuration file itself). A registered
  legacy project whose name is not a safe path component makes ownership unprovable, so cleanup
  fails closed instead of skipping its state. Git registrations are matched by resolved canonical
  path, so a checkout addressed through a directory symlink still finds and clears the real
  registration. A stored path that fails that proof — or a
  Git operation that fails or hangs — is a `WORKTREE_CLEANUP_FAILED` refusal, never a
  fall-through deletion. Registered removal and pruning are checked operations: a locked worktree
  fails closed with its directory and registration intact, a registration list that cannot be
  read fails closed, and completion is re-verified against both the filesystem and a fresh
  registration listing. A missing repository is never read as "unregistered": when registration
  state cannot be verified, an on-disk checkout is left untouched and the cleanup fails closed.
  Deleting an on-disk checkout additionally requires every other registered repository to
  testify about ownership: a checkout registered to another repository — or to more than one —
  is never touched, and a required repository that is unavailable fails the cleanup closed,
  so a checkout owned by a currently unreachable repository is never raw-deleted. The branch is
  never deleted and no garbage collection ever runs.
- If the database deletion commits but the worktree cannot be removed, the response is a
  `WORKTREE_CLEANUP_FAILED` failure whose `details` carry `deleted: true`; the task stays deleted
  and is not resurrected.
- Structured error `details` for `task delete` carry `task_id` only when the id is a safe path
  component. A path-shaped legacy or requested identifier is omitted from `details` entirely and
  reported on stderr only, so structured output never echoes a path-shaped value.

## Project delete

`project delete <project>` removes one registered project, addressed by exact id or exact name,
and only when it has no task rows. A project that still has tasks is refused with
`PROJECT_NOT_EMPTY` and the task count; tasks are never deleted implicitly. Deletion removes only
the project's disposable worktree directory. Retained evidence and the project's repository —
cloned or user-supplied — are never deleted.

An identifier that names one project's id and a different project's name is refused with
`PROJECT_AMBIGUOUS`; a destructive command never guesses, and everywhere else an exact id match
deterministically wins over a name match. Workspace cleanup follows the same laws as task
cleanup: the stored project name must be a safe path component (`project add` refuses unsafe
names and names differing only by case from an existing project, which would alias one
directory), the workspace must prove it is disposable inside the workspace root, and the removal
runs under a registry claim so a name registered again — exactly or as a case variant — keeps its
workspace. A live task's effective checkout (persisted or derived), any other project's
workspace, any registered repository, or any Forge configuration root (evidence, database,
repositories, inbox, knowledge, configuration file) overlapping the workspace makes it
non-disposable and fails the cleanup closed, as does a registered legacy project whose name is
not a safe path component. A stored name or path that fails the proof is a
`PROJECT_CLEANUP_FAILED` refusal whose details identify the project by its id rather than echoing
a path-shaped name.

Before any Git inventory or removal reads through the workspace, the workspace itself is proven
disposable — a plain, non-reparse directory strictly inside the workspace root and disjoint from
every protected path — and its physical identity is pinned and re-proven before every Git
removal and before the final directory deletion; a symlinked or junction workspace fails closed
without touching its target. Git worktree registrations are then inventoried by resolved
canonical path across every registered repository. A checkout inside the workspace that is
registered to the project's own repository is removed only through checked Git operations — a
locked leftover checkout fails the cleanup closed with its directory and registration intact —
and the owner's registrations whose checkouts are already gone are pruned, with the inventory
re-read afterwards. A checkout registered to another repository fails the cleanup closed without
being touched, and a repository that cannot testify — required but unavailable or unlistable —
fails the cleanup closed too, because unverifiable registration state is never "nothing
registered". Project deletion therefore never deletes a registered checkout raw and never leaves
stale Git registration metadata behind.

A workspace already missing on disk is still reconciled before success is reported: stale
registrations of the project's own repository under the missing path are pruned and verified
gone — a locked stale registration fails the cleanup closed — and a foreign registration under
the missing path fails closed. A repository directory that no longer exists holds no
registration metadata and needs no check.

```json
{
  "schema_version": "1.0",
  "command": "project.delete",
  "success": true,
  "data": {
    "deleted": true,
    "project": "demo",
    "project_id": "project-3f2a1b4c5d",
    "evidence_retained": true,
    "repository_removed": false,
    "workspace_removed": true
  },
  "error": null
}
```

If the registry deletion commits but the worktree directory cannot be removed, the response is a
`PROJECT_CLEANUP_FAILED` failure whose `details` carry `deleted: true`; the project stays deleted.

## Task evidence

Two read-only commands expose the evidence a task produced, so an external application can display
validation results, independent reviews, patches, and final results without reading Forge's file
system or `forge.db`.

```powershell
.\forge.ps1 task evidence list <task-id> --json
.\forge.ps1 task evidence show <task-id> --artifact-id <artifact-id> --json
```

Both accept `--schema-version` like every other command. When supplied it must be `1.0` or `1.1`;
any other value fails with `UNSUPPORTED_SCHEMA`. These two payloads are identical in both versions,
since neither carries `token_usage`. Both retain human-readable output
when `--json` is absent: `list` prints one tab-separated line per artifact, and `show` prints the
artifact header followed by its content.

These commands never write. They do not create, retry, cancel, reconcile, or alter a task, and they
do not modify the database or any evidence file.

Both commands resolve the task through its registry row while one exists. Once the task row has
been deleted, the retained evidence tree becomes the read authority: Forge scans exactly one level
beneath the evidence root for a project directory holding a plain directory named exactly by the
task id, under the same path-safety rules as every other evidence access — files, links, and unsafe
names are ignored. One match serves the retained evidence; no match is `TASK_NOT_FOUND`; more than
one match fails closed with `EVIDENCE_AMBIGUOUS` rather than guessing between projects. A live
registry row always wins, so retained evidence left by a deleted task never shadows a newer task
that reuses the same identifier.

The retained-evidence scan pins each directory it enumerates by physical identity and canonical
containment and re-verifies the pin after enumeration, so a directory swapped and left swapped
fails the read closed. A full rename-away-and-restore race completed entirely within one
enumeration window is an explicitly accepted local-filesystem limitation: closing it would
require enumeration through a held directory handle, which Windows does not offer portably. The
scan is hardened against, not proof against, a local writer racing it.

### Evidence manifest

`task evidence list` returns a manifest of every artifact the public allowlist recognizes. A task
that has not produced evidence yet is a success, not a failure: `evidence_available` is `false` and
`artifacts` is empty.

```json
{
  "schema_version": "1.0",
  "command": "task.evidence.list",
  "success": true,
  "data": {
    "task_id": "TASK-1042",
    "project": "demo",
    "task_registered": true,
    "task_state": "COMPLETED",
    "evidence_available": true,
    "attempt_count": 2,
    "attempts_with_evidence": [0, 1],
    "artifact_count": 11,
    "availability": {
      "final_result": true,
      "validation_result": true,
      "review_result": true,
      "patch": true,
      "repair_summary": true,
      "implementation_result": true
    },
    "artifacts": [
      {
        "artifact_id": "ev_a9fb9c7eeaca90cefa3b5874075530b6",
        "artifact_kind": "implementation_result",
        "display_name": "attempt-00/implementation/implementation.final.json",
        "media_type": "application/json",
        "attempt": 0,
        "phase": "implementation",
        "original_size_bytes": 218,
        "sha256": "47af3f610e4d1e4245899f0b55eb57146d76826c73da5807414bbf77fdcfba7d",
        "modified_at": "2026-08-05T02:20:15.294367+00:00",
        "content_available": true,
        "truncated_when_read": false,
        "redaction_required": true,
        "structured": true
      }
    ],
    "limits": {
      "max_returned_bytes": 262144,
      "max_readable_bytes": 8388608
    }
  },
  "error": null
}
```

- `task_registered` is `true` while the task's registry row exists. For a deleted task served from
  retained evidence it is `false`, and the registry-derived fields `task_state` and `attempt_count`
  are `null` — registry facts are never fabricated from disk. Every filesystem-derived field is
  populated normally either way.
- `attempt_count` is the controller's attempt counter for the task; `attempts_with_evidence` lists
  the attempts that actually left artifacts on disk. They differ while an attempt is still running.
- `phase` is the phase or validation group the artifact belongs to, and is `null` for artifacts that
  belong to the attempt as a whole. A repair phase and the validation run that follows it share one
  group directory, so `phase` identifies the group rather than a single controller step.
- `display_name` is the artifact's path relative to the task's evidence root. It is structural, not
  a production absolute path.
- `modified_at` is the file's modification time in UTC. Forge does not report a creation time,
  because it is not recorded consistently across platforms.
- `sha256` covers the artifact's complete original bytes. It is `null` when the artifact is larger
  than `max_readable_bytes`, or when the file changed while the manifest was being built — Forge
  reports no hash rather than one that describes no version of the file. When a hash is reported,
  `original_size_bytes` and `modified_at` describe the same verified snapshot the hash was
  computed from.
- `content_available` is `false` when the artifact is too large to read or holds binary content.
- `truncated_when_read` predicts whether `show` will truncate, from the raw size alone. It is a
  hint: redaction runs before truncation, so a redacted artifact can come back complete.
- `redaction_required` is `true` for every public artifact: redaction is unconditional, not
  conditional on a scan result.
- Ordering is deterministic: by attempt number, then by relative path.

### Artifact classification

The contract is an explicit allowlist keyed on the controller's own evidence layout, not on file
extensions. An artifact Forge does not positively recognize is excluded.

| `artifact_kind` | Source file | Media type | Structured |
|---|---|---|---|
| `final_result` | `attempt-NN/task-result.json` | `application/json` | yes |
| `patch` | `attempt-NN/changes.patch` | `text/x-diff` | no |
| `implementation_result` | `attempt-NN/implementation/implementation.final.json` | `application/json` | yes |
| `review_result` | `attempt-NN/review[-N]/review[-N].final.json` | `application/json` | yes |
| `repair_summary` | `attempt-NN/{validation,review}-repair-N/*.final.json` | `application/json` | yes |
| `validation_log` | `attempt-NN/<group>/validation-NN.{stdout,stderr}.log` | `text/plain` | no |

Excluded from the public contract:

| Excluded | Reason |
|---|---|
| `<phase>.prompt.txt` | Raw model prompt, which embeds controller paths and prior failure text. |
| `<phase>.events.jsonl` | Unrestricted JSONL model event stream, including thread identifiers. |
| `<phase>.stderr.log` | Unrestricted model process stderr, not classified as safe output. |
| Anything else under the evidence root | Not positively recognized by the allowlist. |
| Anything outside the task's evidence root | Refused by the path-safety checks below. |

Also excluded, by construction rather than by filter: authentication material, credential files,
cookies, tokens, raw process environments, environment dumps, session and thread identifiers, and
controller configuration. None of these are classified as public, so none can be selected.

A `validation_log` is a declared validation command's captured output. Both its streams are
published because the command exists only because an operator registered it, and the failing stream
is usually the one that explains a failure. This is deliberately distinct from a model process's own
`<phase>.stderr.log`, which stays excluded.

### Artifact identifiers and path safety

An `artifact_id` is a deterministic, opaque identifier: `ev_` followed by 32 hex characters derived
from a SHA-256 over the task identifier and the artifact's relative path. The same artifact of the
same task always yields the same identifier; the identifier discloses no path information and is not
valid for a different task.

External callers never supply a file-system path. `show` recomputes the manifest and matches the
supplied identifier against it — the identifier is matched, never decoded into a path. Consequently:

- Path traversal, absolute paths, UNC paths, and alternate-data-stream names fail the identifier
  shape check and return `ARTIFACT_NOT_FOUND`.
- An excluded artifact is indistinguishable from a missing one, so `show` cannot be used to probe
  for withheld files.

In addition, Forge:

1. Derives the task evidence root from controller state only, and proves the derived root is inside
   the configured evidence root. The root is additionally re-verified immediately before every
   read: the project and task directories must be plain directories — never links or reparse
   points — and the root must still resolve strictly inside the evidence root, so a directory
   swapped after discovery is refused rather than followed.
2. Canonicalizes the evidence root and the candidate file, and confirms containment both when the
   manifest is built and again immediately before reading — against directory identities and
   canonical roots captured at verification, never against a freshly resolved task root. The
   verified chain is re-checked before every enumeration, before every open, and again before a
   response is returned, and the opened file must be the identical file discovery saw, so a task
   directory swapped for a link at any point is refused rather than served.
3. Inspects directory entries without following links, and refuses symlinks and Windows reparse
   points, so a link that escapes the evidence root is never traversed or served.
4. Refuses anything that is not a regular file: directories, devices, pipes, and sockets.
5. Rejects path components containing `:`, `/`, `\`, or NUL, and the components `.` and `..`.
6. Never executes an artifact and never opens one with an associated application.
7. Never returns a production absolute file-system path in artifact content.

### Artifact content

`task evidence show` returns one bounded, redacted artifact. For a deleted task served from
retained evidence, `task_state` is `null`; artifact resolution, content, and hashes are unchanged.

```json
{
  "schema_version": "1.0",
  "command": "task.evidence.show",
  "success": true,
  "data": {
    "task_id": "TASK-1042",
    "project": "demo",
    "task_state": "COMPLETED",
    "artifact": {
      "artifact_id": "ev_782bd8bac79b8ada58799e9e8ea86ddd",
      "artifact_kind": "final_result",
      "display_name": "attempt-01/task-result.json",
      "media_type": "application/json",
      "attempt": 1,
      "phase": null,
      "original_size_bytes": 904,
      "sha256": "ade5a7ee8a94821b1bd51c8c58debea0c963cfbdee65ac02ab3f96e23b8dc2a5",
      "modified_at": "2026-08-05T02:21:31.177431+00:00",
      "content_available": true,
      "truncated_when_read": false,
      "redaction_required": true,
      "structured": true
    },
    "content": {
      "encoding": "utf-8",
      "text": "{\n  \"status\": \"completed\",\n  \"summary\": \"Implemented the export.\"\n}",
      "representation": "sanitized_json",
      "original_size_bytes": 904,
      "returned_size_bytes": 694,
      "truncated": false,
      "redacted": true,
      "sha256": "ade5a7ee8a94821b1bd51c8c58debea0c963cfbdee65ac02ab3f96e23b8dc2a5",
      "complete": true
    },
    "structured_status": "sanitized",
    "structured_data": {
      "status": "completed",
      "summary": "Implemented the export.",
      "changed_files": ["src/app.py"],
      "validation_performed": ["python -m pytest"],
      "assumptions": [],
      "blocking_reason": null,
      "decisions_needed": [],
      "reusable_patterns": [{"trigger": "export", "lesson": "buffer writes"}],
      "review": {"verdict": "pass", "summary": "No blocking issues.", "findings": []},
      "controller_validation": [
        {"command": "python -m pytest", "exit_code": 0, "passed": true, "duration_seconds": 1.5}
      ],
      "branch": "forge/task-1",
      "commit": "abc123",
      "completed_at": "2026-08-04T10:00:00+00:00"
    },
    "formatted_text": "Status: completed\nSummary: Implemented the export.",
    "limits": {"max_returned_bytes": 262144, "max_readable_bytes": 8388608}
  },
  "error": null
}
```

#### Size limits and truncation

- Forge returns at most `limits.max_returned_bytes` (262144, 256 KiB) of content.
- Forge refuses to read an artifact larger than `limits.max_readable_bytes` (8388608, 8 MiB) and
  reports `ARTIFACT_TOO_LARGE`.
- **Order of operations: read whole, sanitize, then bound.** Forge reads the artifact completely up
  to the read limit, applies path scrubbing, redaction, and structured sanitization to the whole
  content, and only then truncates to the return limit. Truncating first would let a secret that
  straddles the return limit survive as an unmatched — and therefore unredacted — prefix.
- Because redaction runs first, replacing a secret can bring content back under the return limit.
  An artifact whose raw size exceeds `max_returned_bytes` is therefore not guaranteed to come back
  truncated. `artifact.truncated_when_read` in the manifest is a prediction from the raw size;
  `content.truncated` in a `show` response is authoritative.
- `content.original_size_bytes` is the artifact's full size on disk; `content.returned_size_bytes`
  is the UTF-8 byte length of the returned `text` after sanitization, redaction, and truncation. The
  two differ routinely and neither implies the other.
- `content.truncated` is `true` when the returned text was cut. `content.complete` is its inverse
  and exists so a consumer cannot mistake a bounded response for a whole file. Forge never reports
  complete content it did not return in full.
- Truncation happens on a byte boundary and is then trimmed back to a valid UTF-8 character
  boundary, so a multi-byte character is never split across the limit.

#### Hash semantics

`sha256` always covers the artifact's complete original bytes as they were on disk — never the
returned, truncated, sanitized, or redacted text. A truncated response therefore carries a hash that
does not match its own `text`; this is intentional, so a consumer can detect that an artifact
changed between two calls.

In a `show` response, every snapshot-dependent `artifact` field — `sha256`,
`original_size_bytes`, `modified_at`, `content_available`, and `truncated_when_read` — is
overwritten from the read itself: the modification time comes from the same open descriptor that
produced the bytes and the hash, so the artifact metadata and the returned content always
describe the same opened file, never discovery-time state.

#### Redaction semantics

Redaction is unconditional and applied line by line, so line structure and line counts survive. A
pattern can never consume a newline and join two lines. `content.redacted` reports whether anything
was replaced. Redacted values become `[REDACTED]`.

Forge redacts at minimum: API keys, bearer tokens, OAuth tokens, `Authorization` headers,
credential-style assignments (`password`, `secret`, `token`, `api_key`, `client_secret`,
`private_key`, `passphrase`, and similar names), passwords, private-key blocks, URLs carrying user
credentials, JWTs, and the known secret formats Forge already recognizes (`sk-`, `sk-ant-`, `ghp_`
and related GitHub tokens, `github_pat_`, Slack `xox*`, Google `ya29.` and `AIza`, AWS `AKIA`/`ASIA`).

Forge additionally replaces its own configured absolute roots — evidence, workspace, repository,
knowledge, inbox, data, and installation roots — with placeholders such as `<evidence-root>`, so a
log line that echoes a production path does not disclose it. Path scrubbing counts as redaction and
sets `content.redacted`.

Redaction is a presentation concern. The artifact on disk is never modified.

#### Structured artifacts

For a recognized controller document, `structured_data` holds an explicit allowlist of safe fields.
Internal absolute paths (`worktree`, `patch`, per-run `stdout`/`stderr`), thread identifiers, raw
prompts, and any unrecognized field are removed rather than masked. `formatted_text` is an optional
sanitized rendering for display.

Allowlisted fields are documented as strings, but the document is model-produced and Forge does not
re-validate it against a schema here. Every string is therefore sanitized at every depth: a nested
object or array appearing where a string was expected is still scrubbed and redacted rather than
passed through. Nesting deeper than 8 levels is dropped, and allowlisted collections are capped at
500 entries so the projection cannot outgrow the return limit.

Arbitrary JSON is never passed through. When sanitization applies, `content.text` carries the
serialized allowlisted projection — indicated by `content.representation` of `sanitized_json` — so a
field removed from `structured_data` cannot reappear through the content channel.

`structured_status` reports what happened:

| Value | Meaning |
|---|---|
| `sanitized` | The document was valid and allowlisted. `content.representation` is `sanitized_json`. |
| `invalid_json` | The file is not valid JSON. `structured_data` is `null` and the redacted raw text is returned as `redacted_text`. |
| `unsupported_shape` | Valid JSON, but not a recognized controller document shape, or nested too deeply to parse. |
| `unavailable_truncated` | The sanitized projection would exceed the return limit, so it was withheld and the redacted raw text returned instead. |
| `not_structured` | The artifact is not a structured kind (a patch or validation log). |

Because serializing a sanitized projection can expand it, Forge measures the result against
`max_returned_bytes` and falls back to `redacted_text` rather than returning JSON truncated into
invalid syntax.

#### Consistency while a task is running

Evidence is written while a task runs, so an artifact can change during inspection. Forge captures
file metadata from the same descriptor before and after reading and compares size, modification
time, and file identity. If the artifact changed, Forge returns `ARTIFACT_CHANGED_DURING_READ`
rather than a stable-looking result, and the caller may retry; `error.details.retryable` is `true`.

Inspecting a file and opening it cannot be made atomic, so Forge also proves that the descriptor it
read refers to the file it inspected, by comparing device and inode identity across the open. This
detects a regular file swapped for a link between the two steps. Where the underlying filesystem
does not report file identity, Forge reports `ARTIFACT_CHANGED_DURING_READ` rather than skipping the
check; the evidence root is expected to live on a filesystem that supplies it, such as NTFS.

Forge opens artifacts for shared reading only and never takes a lock, so inspection cannot block the
controller from writing evidence. An artifact deleted between `list` and `show` returns
`ARTIFACT_NOT_FOUND`.

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

Error envelopes preserve the requested contract schema version whenever it is a supported one —
including argument-parse, configuration, and unexpected-failure envelopes; an unsupported
requested version keeps failing closed with `UNSUPPORTED_SCHEMA` at the default version.

This response exits with code `2`. Stable current error codes are:

| Code | Exit | Meaning |
|---|---:|---|
| `TASK_NOT_FOUND` | 2 | The requested task neither exists in the registry nor has retained evidence on disk. |
| `INVALID_ARGUMENT` | 2 | The command line could not be parsed — a missing, unknown, or unparseable argument. |
| `CONFIG_ERROR` | 2 | `forge.toml` could not be loaded. |
| `COMMAND_FAILED` | 1 | An unexpected failure occurred while running the command. |
| `INVALID_TASK_REQUEST` | 2 | A submitted field was semantically invalid: empty, too long, malformed, or carrying rejected characters. |
| `PROJECT_NOT_FOUND` | 2 | The requested project is not registered. |
| `DUPLICATE_TASK_ID` | 2 | The derived task identifier already holds a different request. |
| `TASK_ID_CONFLICT` | 2 | The client-supplied task identifier already holds a different request. |
| `TASK_ID_COLLISION` | 2 | The task id names retained evidence of a deleted task, or differs only by case from a live task in the same project; either would alias one on-disk generation. |
| `PROJECT_UNSAFE` | 2 | The target project's registration cannot accept submissions: its stored name is not a safe path component, or another registered project aliases its Windows physical name. |
| `INVALID_VALIDATION_COMMAND` | 2 | A submitted validation command was empty, too long, multi-line, or attempted to chain, redirect, or substitute commands. |
| `INVALID_LIMIT` | 2 | `max_attempts` or `priority` was not an integer within its supported range. |
| `CONFIGURATION_ERROR` | 2 | Forge is configured but not initialized. Run `forge init`. |
| `DATABASE_ERROR` | 1 | The Forge database could not be read or written. |
| `EVIDENCE_NOT_FOUND` | 2 | The task exists but has recorded no evidence. |
| `EVIDENCE_UNAVAILABLE` | 2 | The task's evidence cannot be located, for example because its project is no longer registered. |
| `EVIDENCE_AMBIGUOUS` | 2 | The task id's evidence location is aliased: retained evidence exists under more than one project, a retained directory occupies the same Windows physical name, or a live case-variant task in the same project shares the directory. Reads fail closed; details name only ids and project names, never paths. |
| `ARTIFACT_NOT_FOUND` | 2 | The identifier is not a public artifact of this task, or the artifact was removed. |
| `ARTIFACT_NOT_PUBLIC` | 2 | Reserved for an artifact excluded by the public allowlist. |
| `ARTIFACT_OUTSIDE_EVIDENCE_ROOT` | 2 | A candidate resolved outside the task's evidence root and was refused. |
| `ARTIFACT_UNSAFE_TYPE` | 2 | The artifact is binary, or is not a regular file. |
| `ARTIFACT_TOO_LARGE` | 2 | The artifact exceeds `limits.max_readable_bytes`. |
| `ARTIFACT_INVALID_UTF8` | 2 | The artifact is not valid UTF-8 text. |
| `ARTIFACT_CHANGED_DURING_READ` | 1 | The artifact changed while being read. Retryable; `details.retryable` is `true`. |
| `FILESYSTEM_ERROR` | 1 | The evidence tree could not be read. |
| `UNSUPPORTED_SCHEMA` | 2 | The requested `--schema-version` is not supported. |
| `TASK_ACTIVE` | 2 | The task is in an active execution state; deletion is refused. |
| `PROJECT_NOT_EMPTY` | 2 | The project still has tasks; delete them explicitly first. |
| `PROJECT_AMBIGUOUS` | 2 | The identifier names one project's id and a different project's name; deletion refuses to guess. |
| `WORKTREE_CLEANUP_FAILED` | 1 | The task was deleted but its worktree could not be removed. `details.deleted` is `true`. |
| `PROJECT_CLEANUP_FAILED` | 1 | The project was deleted but its worktree directory could not be removed. `details.deleted` is `true`. |

An evidence failure is reported identically in both output modes: with `--json` the envelope carries
the code and the process exits nonzero; without `--json` the same `CODE: message` line goes to
stderr, stdout stays empty, and the exit code is unchanged. `ARTIFACT_NOT_PUBLIC` is reserved and
documented for completeness; Forge currently reports `ARTIFACT_NOT_FOUND` for an excluded artifact so
that the response cannot be used to confirm a withheld file exists.

A failure example, which exits with code `2`:

```json
{
  "schema_version": "1.0",
  "command": "task.evidence.show",
  "success": false,
  "data": null,
  "error": {
    "code": "ARTIFACT_NOT_FOUND",
    "message": "The artifact identifier is not a known artifact for this task.",
    "details": {
      "task_id": "TASK-1042",
      "artifact_id": "ev_00000000000000000000000000000000"
    }
  }
}
```

`INVALID_ARGUMENT` covers failures the argument parser rejects before Forge sees the request;
`INVALID_TASK_REQUEST` covers values that parse but are not acceptable. More codes may be added
compatibly, so consumers must treat an unrecognized code as a failure rather than an error.

Error `details` are structured and safe to log. They identify the offending field, index, or
identifier, and never contain submitted command text, database messages, file-system paths, or
Python exception text. Evidence failures follow the same rule: a SQLite message, an OS error string,
a raw exception, a captured stdout or stderr fragment, and the bytes of a rejected artifact are all
withheld.

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

Evidence retrieval is read-only and additive. It classifies by the controller's own layout rather
than by file extension, so a new controller output is excluded until it is explicitly allowlisted.
It withholds prompts, model event streams, unrestricted model stderr, thread and session identifiers,
credentials, and controller configuration, and it removes internal absolute paths from structured
documents instead of masking them. Evidence commands never write, never lock an evidence file, and
never block the controller.

Submission is the only write this contract performs. It inserts one task row and nothing else: it
does not fetch, clone, create branches or worktrees, run validation commands, start the controller,
or touch the file system. Both submission and validation refuse to run against a database that does
not exist yet, reporting `CONFIGURATION_ERROR` rather than creating one, so a validation call leaves
no trace whatsoever.

The SQLite schema is an internal persistence format and may change independently of this contract.
Reading `forge.db` directly can bypass redaction, couple an application to private fields, and produce
inconsistent reads. External applications—including Shared Chat integrations—must invoke the
documented `--json` commands and must not open `forge.db` directly.

The same rule applies to evidence. The on-disk evidence tree under the Forge data root—`C:\CodexData`
on the production VPS—is an internal format. It contains raw prompts, model event streams,
unrestricted process stderr, and absolute production paths, none of which pass through redaction on
disk. **Consumers must never read `C:\CodexData` directly.** Use `task evidence list` and
`task evidence show`, which are the only supported way to obtain task evidence.

Evidence layout, artifact classifications, and the set of `artifact_kind` values may gain additive
members within schema `1.0`. Consumers must treat an unrecognized `artifact_kind`,
`structured_status`, or `content.representation` as unknown rather than as an error, and must not
depend on `display_name` remaining stable across controller versions — `artifact_id` is the stable
handle within a task.
