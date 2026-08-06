# Telemetry

Codex Forge can emit OpenTelemetry traces describing how a task moved through the controller. This
is the first telemetry slice: it is **local-only**, **off by default**, and **observability-only**.
Enabling it does not change model execution, prompts, task states, budgets, retries, validation
decisions, or any public JSON contract.

## What is emitted

Three nested spans with fixed names:

```
forge.task
└── forge.attempt
    └── forge.phase
```

Span names are constant. Everything that varies is an attribute, so the number of distinct span
names a backend sees never grows with the number of tasks you run.

### `forge.task`

One orchestrated execution of a task in the controller process. Forge re-queues a retried task
rather than looping in place, so a task that retries produces one `forge.task` span per scheduling.
Correlate them with `forge.task.id`.

**No trace represents a task's complete lifetime.** A trace covers one execution inside one
controller process. It does not span retries, and it does not survive a controller restart: a task
interrupted by a restart and recovered on the next run produces a new, unrelated trace. Treat
`forge.task.id`, not the trace id, as the identity of a task over time.

| Attribute | Value |
| --- | --- |
| `forge.task.id` | Task identifier (`TASK-<hash>`), reduced to `[A-Za-z0-9._-]`, ≤64 chars |
| `forge.project.id` | Project identifier, same normalization |
| `forge.task.risk` | `low` / `medium` / `high` / `critical` / `other` |
| `forge.task.max_attempts` | Integer |
| `forge.task.outcome` | `completed`, `needs_decision`, `noncompletion`, `validation_failed`, `review_changes_required`, `budget_exceeded`, `error` |
| `error.type` | Exception class name, when a task ended in an exception |
| `error.code` | `token_budget_exceeded`, `codex_exit_nonzero`, `validation_failed`, `review_changes_required`, `codex_noncompletion`, `unexpected_error` |

The task span also carries the task's categorized token totals (see [Token usage](#token-usage)).

### `forge.attempt`

The single attempt that the enclosing task execution performs.

| Attribute | Value |
| --- | --- |
| `forge.attempt.number` | Integer, 1-based |
| `forge.attempt.kind` | `full` |
| `forge.retry.bucket` | `0`, `1`, `2`, `3+` |
| `forge.attempt.outcome` | Same vocabulary as `forge.task.outcome` |
| `forge.attempt.terminated_by` | `completion`, `codex_noncompletion`, `validation_failure`, `review_verdict`, `budget`, `exception` |

### `forge.phase`

One orchestration phase. Phase names are normalized to a bounded set:

| `forge.phase.name` | Covers |
| --- | --- |
| `implementation` | The initial Codex implementation run |
| `validation_initial` | The first validation sweep |
| `validation_repair` | A validation sweep after a repair run |
| `review` | A Codex review run |
| `review_repair` | A Codex repair run driven by review findings |
| `validation_final` | The final validation sweep before commit |
| `evidence` | Patch, commit, push, and task-result writing |
| `completion` | Knowledge capture and the terminal state write |
| `other` | Defensive fallback for any label this module does not recognize |

The repair-cycle number is **not** part of the name. It is a separate `forge.phase.cycle`
attribute, so span names stay bounded no matter how `limits.max_repair_cycles` is configured.

| Attribute | Value |
| --- | --- |
| `forge.phase.name` | One of the names above |
| `forge.phase.cycle` | Integer repair cycle, `0` for a first pass |
| `forge.agent.role` | `implementer`, `reviewer`, `validator`, `controller` |
| `gen_ai.request.model` | The configured model Forge asked for; absent on non-model phases |
| `gen_ai.request.reasoning_effort` | The configured effort; absent on non-model phases |
| `forge.phase.outcome` | `ok`, `failed`, `skipped`, `budget_exceeded`, `error` |
| `forge.phase.duration_ms` | Integer milliseconds |
| `error.type`, `error.code` | As above |

### Token usage

Where a run reported usage, both `forge.phase` and `forge.task` spans carry the categorized
counts:

`gen_ai.usage.input_tokens`, `gen_ai.usage.cached_input_tokens`,
`forge.usage.fresh_input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.usage.reasoning_tokens`, `forge.usage.raw_total_tokens`,
`forge.usage.cache_hit_ratio`, `forge.usage.accounting_basis`, `forge.usage.budget_basis`.

These come from the same `codex_forge.usage` normalization the control-plane JSON contract and the
token budget use. Telemetry performs no token arithmetic of its own, so a span can never disagree
with what `forge task show --json` reports for the same task. `forge.usage.budget_basis` records
that the budget still counts raw input + output, unchanged by this slice.

### Resource attributes

`service.name` (from configuration), `service.version` (the installed distribution version, when it
can be read), and `deployment.environment` (from the bounded `environment` setting). Nothing else.
The SDK's environment-variable and host-detector resource sources are deliberately bypassed.

## Known limitation: no model-call granularity

A `forge.phase` span covers one whole `codex exec` invocation. **One such invocation may issue many
model requests internally, and Forge cannot currently see them.** Consequences:

- Phase usage attributes are the total the worker reported for the entire invocation, not
  per-request figures.
- `gen_ai.request.model` names the model Forge *asked* for, not the model that served each internal
  request.
- Span counts do not tell you how many model calls a task made.

Per-model-request tracing is a later slice. Do not build dashboards that assume one phase span
equals one model call.

## Privacy and redaction guarantees

Telemetry carries bounded vocabularies, opaque identifiers, and integers. It does not carry:

- prompts or any model input or output text;
- source code, diffs, or patch content;
- commands, stdout, stderr, or log content;
- absolute paths, worktree locations, branch names, or commit hashes;
- repository URLs;
- credentials, tokens, or request headers;
- environment variables;
- review prose or findings text.

How this is enforced:

- **Exception messages are never recorded.** Forge exception text routinely embeds stderr tails and
  absolute worktree paths. Spans record `error.type` (the class name) and a bounded `error.code`
  instead. `record_exception` is disabled, so no exception event is created, and the span status is
  set without a description.
- **Configuration comes only from `forge.toml`.** Endpoint, transport security, timeout, resource,
  sampler, and batch sizing are all passed explicitly, so the SDK never consults the environment for
  them — and the resource is built with `Resource(...)`, not `Resource.create(...)`, so
  `OTEL_RESOURCE_ATTRIBUTES` and the host detectors cannot attach host detail to a Forge resource.
  OTLP request *headers* are the single setting that cannot be passed explicitly: the exporter
  resolves them as `headers or environ.get(...)` and an empty tuple is falsy, so it falls through to
  the environment regardless of what is passed. Forge therefore suppresses exactly
  `OTEL_EXPORTER_OTLP_TRACES_HEADERS` and `OTEL_EXPORTER_OTLP_HEADERS` for the duration of exporter
  construction and restores them exactly — a variable absent beforehand is absent afterwards. No
  other environment variable is touched, your `OTEL_SDK_DISABLED` kill switch is still honored, and
  child processes inherit your original configuration.
- **Identifiers and risk are bounded before emission.** A task id may be operator-supplied (`--id`,
  or `id:` in an inbox YAML file) and the inbox importer accepts `risk:` without validation, so both
  are normalized: `forge.task.risk` collapses to `low`/`medium`/`high`/`critical`/`other`, and
  identifiers are reduced to `[A-Za-z0-9._-]` and capped at 64 characters. A path or a sentence
  placed in either field cannot reach a span.
- **The endpoint must be loopback.** A non-loopback host, an `https://` scheme, embedded userinfo,
  a path, or a query string is rejected when the configuration is read. This slice supports no
  credentials and no headers at all.
- **Attribute values are length-capped** at 120 characters as a backstop.
- **Warnings are sanitized and bounded.** Initialization and export failures are reported as
  `telemetry <stage> unavailable (<ExceptionClass>)` — never the message, because exporter and
  socket errors embed the endpoint and OS errors embed paths. At most five are retained.

Telemetry failure never fails a task. An import error, an unreachable Collector, or an exporter
fault degrades to "no spans"; orchestration control flow does not observe telemetry state.

## Enable it

Telemetry is an optional install extra. Without it installed, Forge runs exactly as before.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[telemetry]"
```

Then add to `forge.toml`:

```toml
[telemetry]
enabled = true
service_name = "codex-forge"
endpoint = "http://127.0.0.1:4317"
protocol = "grpc"
exporter = "otlp"
sample_ratio = 1.0
environment = "local"
```

Every key is optional and every value above is the default except `enabled`. A `forge.toml` with no
`[telemetry]` section behaves exactly as it did before this feature existed.

Validation is strict and fails when the configuration is read, not at runtime:

| Setting | Rule |
| --- | --- |
| `enabled` | Must be a boolean. `"yes"` and `1` are rejected |
| `service_name` | Non-empty, at most 64 characters, `[A-Za-z0-9._-]` only |
| `endpoint` | `http` scheme, loopback host (`127.0.0.1`, `localhost`, `::1`), explicit port, no userinfo, no path, no query |
| `protocol` | `grpc` |
| `exporter` | `otlp` |
| `sample_ratio` | Number in `[0.0, 1.0]`. Sampling is parent-based, so a ratio below 1.0 drops whole traces rather than shredding individual ones |
| `environment` | At most 32 characters, `[A-Za-z0-9._-]` only; empty omits `deployment.environment` |

An **unknown key is an error**, so a typo in a privacy-relevant setting cannot silently resolve to a
default you did not choose.

## Start the local Collector

Forge does not download, install, or manage a Collector. Install the
[OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-releases)
distribution yourself, then:

```powershell
.\ops\otel\Start-OtelCollector.ps1
# or point at a specific binary
.\ops\otel\Start-OtelCollector.ps1 -CollectorPath 'C:\Tools\otelcol-contrib\otelcol-contrib.exe'
# or set it once
$env:OTELCOL_PATH = 'C:\Tools\otelcol-contrib\otelcol-contrib.exe'
```

The helper resolves the binary from `-CollectorPath`, then `OTELCOL_PATH`, then `PATH`, and fails
with all three locations listed when none has it. It creates no Windows service and no scheduled
task; stop the Collector with Ctrl+C.

`ops/otel/collector-config.yaml` listens on `127.0.0.1:4317` (gRPC) and `127.0.0.1:4318` (HTTP),
applies a memory limiter and a batch processor, and exports **only** through the `debug` exporter.
There is no external backend and no network egress. Adding an exporter would send Forge trace data
off the host.

Check the configuration with the installed binary:

```powershell
.\ops\otel\Start-OtelCollector.ps1 -ValidateOnly
```

The repository's own test suite checks the same file structurally without a Collector:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_otel_collector.py
```

## Verify spans

1. Start the Collector in one terminal.
2. Set `enabled = true` in `forge.toml`.
3. Run the controller in another terminal: `.\forge.ps1 run --once`.
4. Watch the Collector console. With `verbosity: detailed` the debug exporter prints each span's
   name, span/parent IDs, and attributes.

You are looking for a `forge.task` span with no parent, a `forge.attempt` whose parent is that
task's span ID, and `forge.phase` spans parented to the attempt — all sharing one trace ID. Spans
appear in batches up to five seconds after the phase ends, and the controller flushes on exit.

If nothing appears, the Collector is probably not running. Forge does not report that as a task
failure by design; check the process and the port rather than the task.

## Disable and roll back

Set `enabled = false` in `forge.toml`, or delete the `[telemetry]` section entirely. Both are
immediate on the next controller start and require no other change — the tracing SDK is never
imported when telemetry is disabled.

To remove the dependency as well:

```powershell
.\.venv\Scripts\python.exe -m pip uninstall opentelemetry-sdk opentelemetry-api opentelemetry-exporter-otlp-proto-grpc
```

Forge runs normally with the packages absent, even with `enabled = true`: initialization fails
closed to a no-op and records one bounded warning.
