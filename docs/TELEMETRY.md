# Telemetry

Codex Forge can emit OpenTelemetry traces describing how a task moved through the controller. This
is the first telemetry slice: it is **local-only**, **off by default**, and **observability-only**.
Enabling it does not change model execution, prompts, task states, budgets, retries, validation
decisions, or any public JSON contract.

## Scope: Forge spans only

Everything on this page describes **Forge's own** spans, and that slice is **certified** — it is
implemented, tested, and safe to enable.

The native OpenTelemetry support inside Claude Code and Codex is a different thing entirely. It has
been **audited but is not enabled**, and nothing in this repository turns it on. Both agents emit
prompts, responses, tool content, and account identity by default, so enabling them is gated behind
[the shared agent telemetry contract](AGENT-TELEMETRY-CONTRACT.md), which defines the trust
boundaries, correlation identifiers, mandatory redaction, token semantics, and the ordered rollout
gates any such slice must pass.

**Collector redaction is now implemented and certified** (contract Gates 2 and 3). The Collector
deletes identity and content from every telemetry surface and keeps only an explicitly approved
vocabulary — see [Collector redaction](#collector-redaction) and
[Synthetic certification](#synthetic-certification-gate-3) below. That work changes what the
Collector would do *if* an agent signal ever arrived. **It enables nothing**: native Claude Code and
Codex telemetry remain disabled.

**The Gate 4 primitives now exist too, and they are not wired.** The correlation identifier, the
trace-context extraction, and the Claude child-environment builder are implemented and tested in
`src/codex_forge/agent_telemetry.py`, and no production code path calls any of them — see
[Agent correlation primitives](#agent-correlation-primitives-gate-4).

**The supervised Claude canary gate (contract Gate 5) has passed.** A supervised Claude Code 2.1.223
run under the span accounting settled with a **zero unresolved remainder**: 3 spans accepted, 1
exported, 2 deliberately filtered, 0 refused, 0 send-failed. Two consequences follow, and they point
in opposite directions:

- **Forge → Claude interaction-span correlation is certified.** The one exported span was
  `claude_code.interaction`, correlated to the Forge phase that started it.
- **Claude request-level trace attribution is blocked under Collector 0.158.0.** Both
  `claude_code.llm_request` spans carried span links, which no processor at that version can redact,
  so `filter/unredactable` dropped them whole. That is the fail-closed filter working as designed —
  not certified, not available, and not pending.

See [The certified canary outcome](#the-certified-canary-outcome). **This enables nothing**: native
Claude telemetry stays disabled, and Gate 6 — inspecting every safely exported Claude field — has not
started.

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

Per-model-request tracing is a later slice, governed by
[the shared agent telemetry contract](AGENT-TELEMETRY-CONTRACT.md). Do not build dashboards that
assume one phase span equals one model call.

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
with all three locations listed when none has it. An explicit `-CollectorPath` always wins, needs no
`OTELCOL_PATH`, need not be on `PATH`, and may contain spaces. It creates no Windows service and no
scheduled task; stop the Collector with Ctrl+C.

Whichever source supplies it, the path must name an existing file carrying a Windows PE header.
Directories, `.cmd`/`.bat`/`.ps1` and other interpreted files, and non-executables renamed to `.exe`
are refused rather than handed to the shell — `PATHEXT` otherwise lets an `otelcol-contrib.cmd`
planted earlier on `PATH` run as a script under your identity. More than one `otelcol-contrib` on
`PATH` is refused as ambiguous instead of silently taking whichever comes first; name the one you
want with `-CollectorPath` or `OTELCOL_PATH`.

`ops/otel/collector-config.yaml` listens on `127.0.0.1:4317` (gRPC) and `127.0.0.1:4318` (HTTP),
exposes the Collector's own metrics on `127.0.0.1:8888`, and exports **only** through the `debug`
exporter. There is no external backend and no network egress. Adding an exporter would send Forge
telemetry off the host.

Two pipelines are declared, and they are identical in shape:

| Pipeline | Receivers | Processors | Exporters |
| --- | --- | --- | --- |
| `traces` | `otlp` | `memory_limiter` → `filter/unredactable` → `transform/privacy` → `batch` | `debug` |
| `metrics` | `otlp` | `memory_limiter` → `filter/unredactable` → `transform/privacy` → `batch` | `debug` |

There is **no `logs` pipeline**, deliberately. It is the second of the two independent barriers in
[contract section 5](AGENT-TELEMETRY-CONTRACT.md#5--signal-policy), and the one that fails closed:
with no logs pipeline the OTLP receiver never registers `/v1/logs` at all, so a log record is
refused at the door rather than accepted and then redacted.

The `metrics` pipeline exists because agent token counters carry no trace context and can only be
joined to a phase by `forge.invocation.id`. Forge itself emits no metrics today; receiving the
signal is a prerequisite for certifying that the join is safe, not an invitation to build on it.

Check the configuration with the installed binary:

```powershell
.\ops\otel\Start-OtelCollector.ps1 -ValidateOnly
```

The repository's own test suite checks the same file structurally without a Collector:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_otel_collector.py
```

## Collector redaction

Forge's own spans never contain anything sensitive, because Forge never constructs the value. That
is not true of agent telemetry: both agents emit prompts, responses, tool content, and account
identity by default, and Claude attaches `user.email`, `organization.id`, and `user.id` with **no
configuration gate at all**. For those signals the Collector is the enforcement point, so
`ops/otel/collector-config.yaml` declares a mandatory privacy processor that runs on every pipeline,
**before `batch` and before any exporter**.

### Two layers

`transform/privacy` applies two OTTL layers, in this order:

1. **Deletion floor.** `delete_matching_keys` with an anchored alternation covering the contract's
   minimum deletion set verbatim: `user.email`, `organization.id`, `user.id`, `user.account_uuid`,
   `user.account_id`, `session.id`, `ccr.session.id`, `workspace.host_paths`, `user_prompt`,
   `system_prompt`, `system_prompt_preview`, `user_system_prompt`, `assistant_response`, `response`,
   `tool_input`, `tool_parameters`, `tool_output`, `payload`, `content`, `error`, `error.message`,
   `url.full`, `url.query`. The pattern is anchored at both ends, so `error` is deleted while
   `error.type` and `error.code` survive.
2. **Allowlist.** `keep_keys` with the approved vocabulary — the three resource attributes, the
   whole certified Forge span contract, `gen_ai.request.*`, the `gen_ai.usage.*` and `forge.usage.*`
   token counts, `gen_ai.token.type`, `forge.phase.duration_ms`, the `*.outcome` fields that carry
   success status, `error.type`, `error.code`, and the two correlation attributes
   `forge.invocation.id` and `forge.telemetry.contract.version`. Anything else is dropped whether or
   not this file has ever heard of it.

Layer 2 is what survives a vendor upgrade that adds a field carrying content: a new attribute is
denied because it is unreviewed, not exported because nobody forbade it. Layer 1 is defence in depth
beneath it, and holds even if the allowlist is later widened by mistake.

`error_mode: propagate` makes the processor **fail closed**. If a statement errors, the batch is
rejected rather than passed downstream unredacted.

### Exact redaction surfaces

Redaction is configured per signal *and* per context. This is not redundancy — a `resource` context
declared under `trace_statements` does **not** apply to metric resources, so a traces-only
configuration lets identity attributes ride out on a metric resource untouched. That was verified
against the real Collector, not assumed.

| Signal | Context | Covers |
| --- | --- | --- |
| `traces` | `resource` | Trace resource attributes — where Claude's ungated identity attributes live |
| `traces` | `scope` | Instrumentation-scope attributes |
| `traces` | `span` | Span attributes |
| `traces` | `spanevent` | Span-event attributes |
| `metrics` | `resource` | Metric resource attributes — a separate surface from the trace resource |
| `metrics` | `scope` | Instrumentation-scope attributes |
| `metrics` | `datapoint` | Metric data-point attributes, for every data-point type |

The contract names four surfaces (span, span event, metric, resource). `scope` is a fifth carrier
that none of those four names covers, so it is protected here as well.

One shared vocabulary is applied to all seven contexts rather than a per-surface allowlist, so no
surface can end up protected less than another by an editing slip.

### Two carriers that cannot be redacted, and are refused instead

Span **links** and metric **exemplars** each carry their own attribute map. Collector 0.158.0
exposes no OTTL `spanlink` or `exemplar` context and no way to empty either slice, so no redaction
processor can reach them — certification confirmed hostile values placed there survive both
`transform` and the `redaction` processor untouched.

Since they cannot be redacted, `filter/unredactable` denies the carrier:

| Signal | Condition | Effect |
| --- | --- | --- |
| `traces` | `Len(span.links) > 0` | A span carrying any link is dropped whole |
| `metrics` | `Len(datapoint.exemplars) > 0` | A data point carrying any exemplar is dropped whole |

The filter is selective, not a blanket drop: sibling spans and data points without links or
exemplars pass through and are redacted normally. Forge emits neither, so nothing is lost today.

**An agent slice enabled later must know that a span carrying a link will not be exported at all.**
Denying an unreviewable carrier is the fail-closed reading of
[contract section 10](AGENT-TELEMETRY-CONTRACT.md#10--version-and-drift-policy); it is recorded here
rather than left to be discovered from a gap in a trace.

That warning has since been collected on. The supervised Claude canary showed every
`claude_code.llm_request` span carrying links and being dropped whole — the reason Claude
request-level trace attribution is blocked. **The filter stays exactly as it is.** Recovering those
spans would mean exporting an attribute map nothing can inspect, which is the one trade this
repository will not make for coverage. A **future Collector version may be reevaluated** if it gains
safe span-link attribute processing — an OTTL `spanlink` context or an equivalent that can empty the
slice — and such a version re-enters at Gate 3 like any other drift event.

## Synthetic certification (Gate 3)

The certification proves redaction by running a **real** Collector, not by reading the YAML. It
starts the installed binary on temporary loopback ports with the repository's own configuration,
pushes OTLP payloads carrying deliberately hostile values on every surface, and asserts on what the
debug exporter printed *after* processing.

The payloads are entirely synthetic. Nothing in the certification calls Claude, Codex, a Forge task,
`C:\CodexData\forge.db`, or any service off the loopback interface, and nothing downloads a
Collector. That is the point of Gate 3: redaction is proven *before* any real agent identity data
has ever been received.

**Certified against OpenTelemetry Collector Contrib `0.158.0`.** A Collector upgrade is a drift
event under [contract section 10](AGENT-TELEMETRY-CONTRACT.md#10--version-and-drift-policy); the
version check is a test, and it fails rather than warns.

### Run it

```powershell
$env:OTELCOL_PATH = 'C:\Tools\OpenTelemetry\otelcol-contrib\0.158.0\otelcol-contrib.exe'
.\.venv\Scripts\python.exe -m pytest tests/test_otel_redaction_certification.py
```

The binary is located through `OTELCOL_PATH` (or `FORGE_OTELCOL_PATH`) only — `PATH` is deliberately
not searched, because a certification run must name the binary it certifies. Without one, these
tests **skip with that message** and the structural certification in `tests/test_otel_collector.py`
still runs in full.

The harness never binds a non-loopback interface, never uses the production ports (4317/4318/8888),
and always stops and reaps the Collector, including when an assertion fails part-way through.

### What the hostile payload contains

Injected on all seven contexts above — trace resource, trace scope, span, span event, metric
resource, metric scope, and both a sum and a histogram data point — plus a span link and a metric
exemplar on their own carriers:

- every key on the minimum deletion set, each carrying a distinct sentinel value;
- sixteen keys that are **not** on any deletion set (`claude.prompt.v3`,
  `gen_ai.completion.0.content`, `vcs.repository.url`, `process.command_line`,
  `http.request.header.authorization`, `exception.message`, and so on), carrying the same class of
  content — these prove the allowlist, not the denylist;
- sentinel values covering a fake email address, fake account and organization IDs, absolute Windows
  and Unix paths, a repository URL, branch and commit values, prompt text, system-prompt text,
  assistant response text, source code, a tool command with an embedded token, tool output,
  validation output, review prose, an environment-variable value, credential-like text, an error
  string, and a URL query string;
- alongside them, the bounded fields that must survive, so the run proves the configuration is not
  simply deleting everything.

Each sentinel is tagged with the surface it was injected on, so a leak names the surface that leaked
it rather than merely "somewhere in the payload".

### How to interpret a pass

A pass means, for that Collector version and that configuration:

- no sentinel value appears anywhere in the processed output, on any of the twelve injected
  surfaces;
- the linked span and the exemplar-bearing data point are absent entirely, while their unlinked
  siblings survive with their approved attributes intact;
- no forbidden attribute key appears;
- no unknown attribute key appears — unreviewed fields were dropped, not passed through;
- `forge.invocation.id` survives on every surface, so the phase-to-metric join still works;
- the approved token, phase, duration, outcome, and `error.type`/`error.code` attributes survive,
  and so do span status and metric values;
- `/v1/logs` returns **404** — there is no logs pipeline to accept it;
- the Collector started, accepted both payloads with HTTP 200, and exited **0**.

Two results make the pass meaningful rather than vacuous:

- **The negative control.** The same binary, payloads, and harness, with only the privacy processor
  removed, leaks every sentinel on every surface. A harness that cannot see a leak proves nothing by
  not seeing one.
- **The fail-closed matrix** in `tests/test_otel_collector.py`. Certification fails if the privacy
  processor is removed, ordered after `batch`, or missing from either pipeline; if a `logs` pipeline
  appears; if any exporter other than `debug` is declared, wired or not; if any receiver or the
  internal metrics endpoint moves off loopback; if any **single** entry is removed from the deletion
  set; if the allowlist is widened to admit a forbidden key or narrowed below the Forge span
  contract; if any surface's statements are deleted; if `filter/unredactable` is dropped from a
  pipeline or either of its conditions removed; or if either processor's `error_mode` is weakened. A
  configuration that references a privacy processor it does not declare is rejected by the Collector
  itself.

A failure is not a flaky test. Read which surface and which key it names.

### What a pass does not prove

- **It is bound to Collector 0.158.0.** Another version is drift, and the version check fails rather
  than warns.
- **The allowlist is not yet the real agent vocabulary.** The approved token-type dimension is
  `gen_ai.token.type`; the dimension names Claude and Codex actually emit have not been observed
  under Forge. Whatever they turn out to be will arrive **dropped**, which is the intended default,
  and adding them is a reviewed change at Gate 6 or Gate 8 — not a convenience edit.
- **"Fails closed" is enforced in two different places.** The Collector itself refuses to start only
  when a pipeline references a processor that is not declared. A hand-edited configuration that
  removes the privacy processor *and* its pipeline references would start and export unredacted;
  what catches that is this test suite plus the `MANIFEST.sha256` hash of the file. Run the tests
  after editing the Collector configuration, not only after editing Python.
- **It says nothing about what an agent emits.** Gate 3 proves the Collector removes what it is
  shown. Gates 6 and 8 still require inspecting **every** attribute a real agent emits, because the
  risk is the attribute nobody expected.

### Rolling the Collector changes back

The Collector configuration is a plain file with no state behind it.

```powershell
git checkout 668767b -- ops/otel/collector-config.yaml MANIFEST.sha256
```

Then restart the Collector (Ctrl+C and re-run `.\ops\otel\Start-OtelCollector.ps1`). Forge is
unaffected either way: nothing in the controller reads this file, and telemetry failure never fails
a task.

Reverting removes the `metrics` pipeline and the privacy processor together. That is safe **only
while native Claude and Codex telemetry stay disabled**, which is the state this repository ships in.
Do not revert it as a prerequisite for enabling an agent exporter; the privacy processor is the
reason those gates can be approached at all.

## Span accounting for a canary

### The problem this solves

The first supervised Claude Code 2.1.223 canary returned a clean-looking result — Claude exited 0,
the response marker was exact, there were no telemetry warnings, twelve metric points were accepted
and twelve exported — and one number that was not clean:

| Reading | Value |
| --- | --- |
| spans accepted by the OTLP receiver | 6 |
| spans exported by the debug exporter | 4 |
| spans refused by the receiver | 0 |

Two spans were accepted and did not come out the other end. **Which two, and why, is not recoverable
from that run**, because at least two different mechanisms produce exactly those three numbers:

- `filter/unredactable` dropping two spans that carried links, which is the configuration working as
  designed; and
- reading `otelcol_exporter_sent_spans` before the `batch` processor's five-second interval elapsed,
  which is a measurement error and not a drop at all.

Both are reproducible on this machine, and both are indistinguishable from the three numbers above.
The obvious way to tell them apart — printing the two spans that went missing — is precisely what
[the privacy policy](#privacy-and-redaction-guarantees) forbids, since the spans under suspicion are
the ones carrying the one attribute map no processor can redact.

A later run measured the mechanism directly and balanced
([the certified outcome](#the-certified-canary-outcome)). That does not reach back and explain the
table above: this run's counters were never accounted, so its two missing spans stay unattributed.
It is superseded, not solved — which is the distinction this whole section exists to hold.

### What the accounting reports

`tests/otel_canary.py` reads five counters from the Collector's own loopback telemetry endpoint
instead of two, and requires them to add up:

```
receiver accepted = exporter sent + deliberately filtered + refused + send-failed + unresolved
```

A nonzero **unresolved remainder** fails the canary. So does a set of counters that never settled, a
Collector that did not exit cleanly, and any finding from the privacy check — which runs on every
path, including the failing ones, and is always included in the report.

The counters are read from the shipped `service.telemetry.metrics.level: basic`; the accounting needs
no change to the Collector's internal telemetry configuration. `otelcol_receiver_accepted_spans`,
`otelcol_receiver_refused_spans`, `otelcol_exporter_sent_spans`, and
`otelcol_processor_filter_spans_filtered` are asserted present by the test suite at that level. The
send-failure counter has no series until something fails, so an absent one is read as zero.

`otelcol_processor_filter_spans_filtered{filter="filter/unredactable"}` is the reading that was
missing from the first canary. It is what separates a deliberate drop from an unexplained one, and
it was available all along.

### The bounded span-name diagnostic

Totals still cannot say *which* spans went missing. A temporary certification configuration adds two
`count` connectors that convert spans to bare counters — one fed straight off the receiver, one at
the end of the certified traces pipeline — so a run can report:

```
by name, accepted      claude_code.interaction=1, claude_code.llm_request=2, claude_code.tool=1, ...
by name, exported      claude_code.interaction=1, claude_code.llm_request=1, ...
not exported, by name  claude_code.llm_request=1, claude_code.tool=1
```

The bucket is carried by the **metric name**, drawn from a closed vocabulary written in
`tests/otel_certification.py` — `forge.task`, `forge.attempt`, `forge.phase`,
`claude_code.interaction`, `claude_code.llm_request`, `claude_code.tool`,
`claude_code.tool.execution`, `claude_code.subagent.spawn`, `claude_code.hook`,
`claude_code.compaction`, and `other`. **Any name outside that list is counted as `other`**, so an
agent-controlled span name is never published.

Nothing from the span itself survives the conversion. Three independent layers enforce that:

| Layer | What it denies |
| --- | --- |
| the connectors declare **no `attributes` block** | a counter with no dimensions cannot carry a span attribute, link, or event |
| `filter/diagnostic_vocabulary` | any metric name outside the closed vocabulary, including defaults a future Collector adds |
| `transform/diagnostic_scrub` | the resource, scope, and data-point attribute maps a `count` connector inherits from the spans it counted |

Layer 3 is load-bearing rather than belt-and-braces, and
`tests/test_otel_span_accounting.py` proves it by removing it and showing the attributes reappear.
The diagnostic emits metric names, counts, and the span timestamps a counter carries — no trace id,
span id, link, attribute, event, exception, identity, session, invocation id, model name, path, URL,
or content.

The breakdown and the Collector's own counters are two independent measurements of the same two
numbers, so the report **requires them to agree**. A run whose buckets do not sum to
`otelcol_receiver_accepted_spans` and `otelcol_exporter_sent_spans` fails, because a breakdown that
disagrees with the counter cannot be used to attribute a remainder.

### Why it is not in the shipped Collector configuration

The `traces/diagnostic_accepted` pipeline **deliberately omits `filter/unredactable`** — a pipeline
that drops the spans is a pipeline that cannot count them. That bypass is safe only because the
pipeline terminates in a counter, and it would not be safe with any exporter that can print a span.
So it is built in test code from the shipped file, run for one supervised canary, and never written
back. `ops/otel/collector-config.yaml` is unchanged.

`test_the_diagnostic_deviates_from_the_certified_pipeline_in_exactly_the_known_ways` enumerates every
difference between the diagnostic configuration and one that passes Gate 3. An edit that relaxes the
*measured* pipeline, or points the bypass at a real exporter, fails that test.

### Running it

The harness starts a Collector and then calls an agent. It **refuses to launch an agent without a
named human approval**, and the test suite never gives it one — every test drives it with synthetic
OTLP instead.

```powershell
$env:OTELCOL_PATH = 'C:\Tools\OpenTelemetry\otelcol-contrib\0.158.0\otelcol-contrib.exe'
.\.venv\Scripts\python.exe tests\otel_canary.py `
    --workdir C:\CodexData\canary\run2 `
    --approved-by '<the human approving this run>' `
    --agent-command 'claude -p "Reply with exactly CANARY_OK and nothing else." --allowed-tools ""'
```

The harness builds the child environment through `agent_telemetry.build_claude_child_environment`
and points it at its own ephemeral loopback port, so the run cannot be answered by a Collector the
operator already has running, and no second child-environment builder exists to review.

### The certified canary outcome

A supervised Claude Code 2.1.223 canary has run under this accounting and balanced. **Contract Gate 5
is passed.**

| Reading | Value |
| --- | --- |
| receiver accepted | 3 |
| exporter sent | 1 |
| deliberately filtered | 2 |
| receiver refused | 0 |
| exporter send-failed | 0 |
| **unresolved remainder** | **0** |
| counters settled | yes |

The bounded diagnostic named every one of them:

```
by name, accepted      claude_code.interaction=1, claude_code.llm_request=2
by name, exported      claude_code.interaction=1
not exported, by name  claude_code.llm_request=2
```

The privacy checks passed, the Collector exited 0, and Claude exited 0.

**What this certifies.** Forge → Claude **interaction-span correlation**. One
`claude_code.interaction` span was accepted and exported through the certified pipeline, correlated
to the Forge phase that started it. The correlation machinery in
[`agent_telemetry`](#agent-correlation-primitives-gate-4) has now been exercised against a real Claude
process rather than a dictionary.

**What this blocks.** Claude **request-level trace attribution**, under Collector 0.158.0. Both
`claude_code.llm_request` spans carried span links, so `filter/unredactable` dropped them whole. The
two numbers that were ambiguous in the first run are now separated by a counter rather than by
argument: `otelcol_processor_filter_spans_filtered{filter="filter/unredactable"}` read 2, and the
remainder read 0.

This is a deliberate drop, and the vocabulary matters. Request-level attribution is **blocked** — it
is not certified, not available, and **not pending**, because nothing is waiting on a queue for it. A
future Collector version **may be reevaluated** if it gains safe span-link attribute processing; that
version would re-enter at Gate 3, and only a passing certification could change this. The filter is
not weakened in the meantime.

**What it does not settle.** Everything else:

- **Gate 6 has not started.** It remains incomplete unless **every** safely exported Claude resource,
  span, and metric field has been inspected and approved. One span reaching an exporter is not an
  inspection of what that span carried.
- **Nothing is enabled.** `ops/otel/collector-config.yaml` is unchanged, native Claude and Codex
  telemetry stay disabled, and no production path emits agent telemetry.
- **No authority moved.** Forge persisted usage stays authoritative, native metrics stay
  observational evidence only, and accounting, budgets, retries, and task state are unchanged.
- **Codex is untouched.** No Codex canary has run; Gates 7–8 are where they were.

## Agent correlation primitives (Gate 4)

`src/codex_forge/agent_telemetry.py` implements the correlation and child-process helpers that
[contract Gate 4](AGENT-TELEMETRY-CONTRACT.md#11--rollout-gates) requires, and
`tests/test_agent_telemetry.py` proves them. Five statements, all of them current:

- **The Gate 4 primitives exist but are not wired.** No module in `codex_forge` imports
  `agent_telemetry`, no production code path calls any function in it, and `CodexRunner.run()`
  builds the same process arguments and the same child environment it built before this slice. Both
  facts are asserted by tests, not merely intended.
- **Native Claude Code and Codex telemetry remain disabled.** This slice adds no exporter, no
  agent-side setting on any process Forge starts, and no CLI override.
- **A supervised Claude canary has passed; no Codex canary has run.** The certified run went through
  the harness, which builds its child environment with `build_claude_child_environment`, so that
  helper and the `TRACEPARENT` it places have now been exercised against a real Claude Code 2.1.223
  process. Claude **interaction-span** correlation under `forge.phase` is certified by it;
  **request-level** attribution is blocked, because the `claude_code.llm_request` spans carry links
  and are deliberately filtered. Being exercised by a test harness is not the same as being wired:
  no production path calls this module. See
  [The certified canary outcome](#the-certified-canary-outcome).
- **Claude requires the certified Collector redaction before any runtime use.** Claude attaches
  `user.email`, `organization.id`, and `user.id` with **no configuration gate at all**, so no child
  environment can suppress them; the Collector's privacy processor is what removes them. Enabling a
  Claude exporter without it running exports operator identity on the first request.
- **Codex strict-config behavior remains unresolved.** The exact `-c` nesting for telemetry
  overrides, and whether custom attributes reach *every* request span, are still open questions. A
  Codex override *builder* now exists (see [Codex telemetry canary](#codex-telemetry-canary-gate-7-foundation)),
  no production path calls it, no Codex process has accepted its output, and the Codex correlation
  mechanism stays **proposed**.

### What the module provides

| Helper | Returns |
| --- | --- |
| `new_invocation_id()` | 128 CSPRNG bits as 32 lowercase hex characters — a new value on every call |
| `is_valid_invocation_id(value)` | True only for that exact shape; uppercase, wrong length, padded, and non-string values are rejected |
| `current_traceparent()` | The W3C `traceparent` of the active Forge span, or `None` |
| `InvocationContext` | `invocation_id`, `contract_version`, `traceparent` — and nothing else |
| `build_claude_child_environment(base, context)` | A **new** dictionary; `os.environ` and `base` are left byte-for-byte unchanged |
| `build_codex_child_environment(base, endpoint=…)` | A **new** dictionary with inherited telemetry configuration removed and **nothing added** |
| `build_codex_otel_overrides(context, endpoint=…)` | An immutable `-c` argv fragment; **proposed, never accepted by Codex** |
| `validate_loopback_endpoint(value)` | The endpoint unchanged, or `ValueError` |

`AGENT_TELEMETRY_CONTRACT_VERSION` is defined once, in that module, and is the only place in
`codex_forge` that spells the contract version out. (`control_plane.DEFAULT_SCHEMA_VERSION` is also
`"1.0"` and is an unrelated number — the control-plane JSON contract's, not this one's.)

**The invocation identifier is uniformly random and nothing else.** It takes no arguments, so no task
id, project id, phase, attempt, branch, commit, path, prompt, response, model, or account identity
can reach it — not directly, not hashed, not encoded. Contract section 4 is absolute about this: a
deterministic derivative of a sensitive value is a sensitive value. It is not persisted and not
reused; a retry, a cancellation, a second phase, or a second provider each get their own.

**The traceparent comes from the official propagator.** `current_traceparent()` asks
`TraceContextTextMapPropagator` to inject into an empty carrier and returns the `traceparent` it
wrote, rather than formatting trace and span ids by hand — hand composition is how a wrong flag byte
or a truncated id reaches a child that will believe it. `opentelemetry.propagate.inject` is
deliberately *not* used: it honors `OTEL_PROPAGATORS` and includes baggage, which would let operator
key/value pairs into the header set. `tracestate` is discarded even when the active context carries
one. The function returns `None` — never an exception — when telemetry is disabled, when the SDK is
not installed, when no recording span is active, or when propagation fails for any reason.

### The exact future Claude environment

This is what `build_claude_child_environment` returns for a child that does not exist yet. Reading
it here is not enabling it.

| Variable | Value |
| --- | --- |
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` | `1` |
| `OTEL_TRACES_EXPORTER` | `otlp` |
| `OTEL_METRICS_EXPORTER` | `otlp` |
| `OTEL_LOGS_EXPORTER` | `none` |
| `OTEL_LOG_USER_PROMPTS` | `false` |
| `OTEL_LOG_ASSISTANT_RESPONSES` | `false` |
| `OTEL_LOG_RAW_API_BODIES` | `false` |
| `OTEL_LOG_TOOL_DETAILS` | `false` |
| `OTEL_LOG_TOOL_CONTENT` | `false` |
| `OTEL_METRICS_INCLUDE_SESSION_ID` | `false` |
| `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` | `false` |
| `OTEL_METRICS_INCLUDE_VERSION` | `false` |
| `OTEL_METRICS_INCLUDE_ENTRYPOINT` | `false` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://127.0.0.1:4317` |
| `OTEL_EXPORTER_OTLP_INSECURE` | `true` |
| `OTEL_EXPORTER_OTLP_HEADERS` | *(empty)* |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | *(empty)* |
| `OTEL_EXPORTER_OTLP_METRICS_HEADERS` | *(empty)* |
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | `delta` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `10000` |
| `OTEL_TRACES_EXPORT_INTERVAL` | `1000` |
| `CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS` | `2000` |
| `CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS` | `2000` |
| `OTEL_RESOURCE_ATTRIBUTES` | `forge.invocation.id=<opaque>,forge.telemetry.contract.version=1.0` |
| `TRACEPARENT` | The active phase span's header — **omitted entirely** when there is no active recording span |

Nothing else. `OTEL_RESOURCE_ATTRIBUTES` carries exactly two attributes and never a service name, a
host, a task, a project, a phase, a path, a branch, a model, or an account. The three header
variables are set **empty rather than left absent**, so no inherited value can fill them: contract
section 7 treats a header reaching an agent as evidence of an unapproved destination.

The endpoint is validated as loopback `http` with an explicit port, no userinfo, no path, no query,
and no fragment. A remote host, `https`, a credential, or a trailing path raises rather than being
accepted and redacted downstream. This check is stricter than `forge.toml`'s: `::1` is refused too,
because the Collector binds `127.0.0.1` and an IPv6 literal has not been exercised end to end.

### Inherited-configuration neutralization

The builder removes inherited agent-telemetry configuration **before** applying the block above. The
exact policy, which the tests assert against directly:

1. A key is neutralized when its name — compared **case-insensitively**, because Windows environment
   names are — either matches one of `TRACEPARENT`, `TRACESTATE`, `CLAUDE_CODE_ENABLE_TELEMETRY`,
   `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`, `CLAUDE_CODE_PROPAGATE_TRACEPARENT`,
   `CLAUDE_CODE_WORKSPACE_HOST_PATHS`, `ENABLE_BETA_TRACING_DETAILED`, `BETA_TRACING_ENDPOINT`, or
   begins with `OTEL_` or `CLAUDE_CODE_OTEL_`.
2. Neutralized means **absent** from the returned dictionary — not blanked, not defaulted. An agent
   reading a variable Forge did not anticipate finds nothing rather than something Forge guessed at.
3. The approved block is applied afterwards, so an approved key always holds its approved value no
   matter what the parent environment held.
4. **Nothing else is removed.** `PATH`, `CODEX_HOME`, the credentials the agent needs to run, and
   every unrelated operator variable survive byte-for-byte. `OTELCOL_PATH` survives — it does not
   begin with `OTEL_`.

The `OTEL_*` prefix is swept wholesale rather than enumerated on purpose. The set of variables the
SDKs read grows with every release, and contract section 10 makes an unrecognized setting
deny-by-default; enumerating them would mean a new exporter, endpoint, header, resource-attribute, or
content-logging variable arrives inherited and enabled.

### What the builder never sets

`CLAUDE_CODE_PROPAGATE_TRACEPARENT`, `CLAUDE_CODE_WORKSPACE_HOST_PATHS`,
`ENABLE_BETA_TRACING_DETAILED`, `BETA_TRACING_ENDPOINT`, `TRACESTATE`, and `CODEX_HOME`.

The first is the important one. Inbound trace context is accepted by the child; **outbound
propagation is forbidden**, because it would put Forge's internal trace and span identifiers on
Claude's requests to Anthropic. That is a hard boundary in contract section 4, not a default. The
other five publish absolute paths, enable a content-carrying beta signal with its own destination,
carry unbounded vendor text, or relocate Codex credential and session storage.

`CODEX_HOME` is the one of the six that is *not* neutralized: an operator's own value is unrelated
configuration, and contract section 7 requires unrelated processes and settings to be unaffected.
Forge neither sets it nor removes it.

## Field inventory (Gate 6)

Gate 5 answered *how many* spans survived. [Contract Gate
6](AGENT-TELEMETRY-CONTRACT.md#11--rollout-gates) asks what the survivors **carry**: every field on
every safely exported resource, scope, span, event, and metric, individually inspected and approved.

`tests/otel_inventory.py` is the harness that produces that inspection.
`ops/otel/claude-field-approvals.yaml` is where the approvals are recorded. They are deliberately two
files.

**Gate 6 has not returned a PASS verdict yet.** The harness exists, the supervised run has been made
(`claude-inventory-20260806-165526`, 29 fields observed), and on 2026-08-06 `human-operator` reviewed
those 29 fields and approved them in the manifest — which now carries a name, an `approved` review
state, and 29 APPROVED entries. What is outstanding is the confirming run: a verdict is a property of
an inventory run against the approved manifest, and the run that produced the evidence was judged
against the candidate one. Nothing outside those 29 fields was approved.

### Observed fields are not approved fields

This is the distinction the whole design turns on.

| | Observed inventory | Approval manifest |
| --- | --- | --- |
| Written by | the harness, per run | a human, once per agent/Collector version |
| Lives in | the run's output directory | `ops/otel/claude-field-approvals.yaml` |
| Says | "this field appeared, N times, carrying this class of value" | "a human is content for this field to be exported" |
| Changes when | a run observes something different | somebody reviews and decides |

A report that could edit its own approvals would not be a review. The harness never writes to the
manifest, and no test writes an approval into it.

### Where the inventory gets its data

From the `debug` exporter's own output, and from nothing else. The harness declares **no receiver, no
exporter, no connector, and no pipeline**: `inventory_configuration()` returns
`ops/otel/collector-config.yaml` plus the bounded span-name diagnostic, byte-identical to what a Gate
5 canary runs.

That is what makes "only post-privacy telemetry is inventoried" a structural fact rather than a
promise. The `debug` exporter is the terminal component of the `traces` and `metrics` pipelines,
whose processors are `memory_limiter` → `filter/unredactable` → `transform/privacy` → `batch`. There
is no receiver-to-inventory route that bypasses those, because there is no receiver-to-inventory
route at all — the module imports no `socket`, no `urllib`, no `grpc`, and no OpenTelemetry package,
which `test_the_only_input_to_the_inventory_is_the_exporters_own_output` asserts against the parsed
import list.

### What is inventoried

| Surface | Fields |
| --- | --- |
| resource | attribute key, OTLP value type, value class or approved static value, occurrences |
| scope | name, version class, attribute keys and types, occurrences |
| span | name, kind, status code, attribute keys/types/value classes, event names, event attribute keys and types, link count, occurrences |
| metric | name, description class or approved description, unit class or approved unit, data type, aggregation temporality, monotonicity, data-point attribute keys/types/value classes, exemplar count, occurrences |

Every entry that names a schema identifier also carries `identifier_status`
(`KNOWN` / `UNKNOWN_SAFE` / `UNPRINTABLE_UNKNOWN`) and `identifier_category`, plus a `fingerprint`
when the identifier could not be printed.

Trace IDs, span IDs, parent IDs, trace state, and raw timestamps are recognized by the parser so they
are not counted as unresolved, and then dropped. They are never stored and never rendered.

### Normalization: a class, not a value

A dynamic value is classified where it is read and the original is discarded. Nothing downstream —
not the JSON, not the Markdown, not an exception message — has the value to leak, because nothing
downstream was ever handed it.

`absent`, `boolean`, `integer`, `nonnegative-integer`, `double`, `nonnegative-double`,
`lowercase-hex-32`, `lowercase-hex-16`, `semantic-version`, `duration`, `timestamp-present`,
`localhost-endpoint`, `approved-enum`, `approved-static-string`, `unclassifiable`.

Two rules govern the list:

- **Only `approved-static-string` and `approved-enum` may carry a value**, and only through a
  per-key allowlist in the manifest. `service.name` prints as `claude-code` because a human wrote
  that exact string into `static_values`. The same string under a key the manifest does not
  allowlist is not printed.
- **There is deliberately no `bounded-string` class.** A free-form string matching none of the
  patterns is `unclassifiable`, which **no approval may cover** — an APPROVED field observed
  carrying an unclassifiable value is downgraded to UNAPPROVED. "It looked short and harmless" is
  how a prompt preview gets published.

Values include a metric's unit and description, under the pseudo-keys `metric.unit` and
`metric.description`. They are not identifiers and are not printed because the metric name was safe.

The forbidden-output list — prompts, responses, tool input and output, error and exception text,
email addresses, user/account/organization/session identifiers, API keys and authorization material,
file-system and repository paths, URLs and query strings, non-loopback hostnames, environment values,
trace and span and parent identifiers, raw invocation IDs, span-link data, exemplar data — is
enforced twice: by the classifier, which never produces them, and by a pattern scan that **refuses to
write** a report that contains one, substituting a stub that names the pattern without reproducing
the match.

### Unknown schema identifiers get their own entry, and are usually printed in full

A **schema identifier** names the shape of the telemetry rather than its payload. There are exactly
nine, and the list is closed:

resource attribute keys · scope names · scope attribute keys · span names · span attribute keys ·
event names · event attribute keys · metric names · metric data-point attribute keys

An unknown identifier gets **its own deterministic inventory entry**. Distinct unknowns are never
merged into a shared bucket, whether or not they can be printed — two new span names produce two
entries with their own occurrence counts.

**A reviewer never has to open the Collector log to review an identifier.** An unknown identifier is
printed exactly, provided it passes the safe-identifier policy below, and the run also writes
`candidate-manifest.yaml`: a copy-and-paste proposal of the vocabulary entries and UNAPPROVED field
entries the manifest would need. `test_the_candidate_manifest_closes_every_unknown_field` merges that
proposal back in and shows nothing is left UNKNOWN.

#### The safe-identifier policy

An identifier is printed verbatim only when **every** check passes. Each check is named in the
report's `identifier_findings` when it fires, so a refusal says which rule refused it.

| Check | Rule |
| --- | --- |
| category | one of the nine above |
| non-empty | not the empty string |
| length | ≤ 100 characters, or ≤ 200 for a scope name |
| UTF-8 | encodable as UTF-8 — no lone surrogate |
| control characters | no Unicode `Cc`, `Cf`, `Cs`, `Co`, or `Cn` code point |
| whitespace | none at all; no identifier category requires it |
| grammar | `[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*`, plus `/` segments for scope names only |
| path shape | no `\`, no leading `/`, no `~`, no drive-letter prefix |
| URL / email / credential shape | no `:`, `@`, `?`, `=`; no `sk-`-style key; no separator-free 40+ character blob |
| opaque-identifier shape | no UUID, and no run of 16+ hexadecimal characters — the shape of a trace ID, span ID, session ID, or invocation ID |
| content-bearing words | no segment word in `prompt`, `response`, `content`, `body`, `message`, `payload`, `stdout`, `stderr`, `stacktrace`, `snippet`, `secret`, `credential`, `password`, `authorization`, `cookie`, `email`, `command`, `argv`, `traceparent`, … |

Words are matched per dot-segment after splitting on `_` and `-`, so `user_prompt` is refused and
`gen_ai.usage.input_tokens` is not. `token`, `model`, `usage`, and `session` are deliberately absent
from that list: a key mentioning them is ordinary telemetry, while a key mentioning `prompt` is
announcing what its value is.

#### When an identifier fails

The entry is classed `UNPRINTABLE_UNKNOWN` and carries:

- `fingerprint` — the unsalted SHA-256 of the identifier's UTF-8 bytes, 64 lowercase hex characters
- `fingerprint_algorithm` — `sha256`
- `identifier_category` — which of the nine positions it occupied
- `identifier_findings` — the checks that failed, by name

The original string is not emitted anywhere: not in the JSON, not in the Markdown, not in the
candidate manifest, not in an exception message, and not in a log line. The entry id becomes
`<kind>/sha256-<digest>`, so two distinct unsafe identifiers still get two entries. An
`UNPRINTABLE_UNKNOWN` entry **fails Gate 6** and can never be approved, whatever the manifest says.

The fingerprint is unsalted so that two runs — and two machines — agree on it, which is what makes it
something a reviewer can track. The tradeoff is accepted deliberately: a fingerprint of a low-entropy
name is guessable, and the alternative is publishing the name.

#### Units and descriptions are values, not identifiers

A safe metric *name* buys the prose beneath it nothing. A metric's unit and description go through
the same allowlist every other value does, under the pseudo-keys `metric.unit` and
`metric.description`, and are reported as `unit_classes` / `description_classes` unless a human has
allowlisted the exact string. The candidate manifest proposes **no value of any kind** — widening
`static_values` stays a decision taken with the value in front of you.

#### Nothing is silently omitted

An unknown key, unknown span name, unknown metric name, unknown value type, or unclassifiable value
produces a visible entry with a reason and a count, and fails the verdict. A line the parser cannot
interpret becomes an `unresolved/<context>` entry, which is the report's **unresolved inventory
remainder** — the same "a nonzero remainder is not explained away" discipline the span accounting
uses. An `unclassifiable` value can never be approved: the reviewer must allowlist it or reject the
field.

### The accounting the report must balance

```
fields observed = approved + blocked + redacted + unapproved + unknown + unresolved remainder
```

plus a separate count of fields the manifest expects that the run did not observe — reported, never
treated as approved-and-absent.

### The Gate 6 verdict

`PASS` requires all of: a manifest pinned to Claude Code 2.1.223 and Collector 0.158.0; a human name
and an `approved` review state in the manifest; at least one field observed; every observed field
present in the manifest; none UNAPPROVED, BLOCKED, or REDACTED; every expected field observed; a zero
unresolved remainder; **every observed identifier printable**; zero span links and zero exemplars
reaching the inventory; a clean forbidden-output scan; a balanced span accounting; a passing canary;
and a clean exit from both the Collector and the child.

The shipped manifest carries `approved_by: human-operator` and an `approved` review state as of
2026-08-06, so the human-approval condition is met. Every other condition is still decided by the run
itself: a field this review never saw is UNKNOWN, and one UNKNOWN field is still a `FAIL`.

### The agent vector

The canonical form of the canary invocation is an **argument vector**, not a command string. A string
would have to survive PowerShell quoting and then `shlex` quoting before it became argv, and every
one of those hops is somewhere an argument can be joined to its neighbour or lost. The vector is a
constant in `tests/otel_inventory.py` (`DEFAULT_CLAUDE_ARGV`) and is handed to `subprocess` as it
stands:

```
[0]  claude
[1]  -p
[2]  Reply with exactly CANARY_OK and nothing else.
[3]  --model
[4]  sonnet
[5]  --permission-mode
[6]  dontAsk
[7]  --tools
[8]  <empty string>
[9]  --disable-slash-commands
[10] --max-turns
[11] 1
```

One shot, one turn, one pinned model, no tools, no slash commands, and a permission mode that
suppresses the prompt without granting anything. `python tests/otel_inventory.py --output-dir <dir>
--dry-run` prints this vector and the PowerShell command below, and starts nothing.

#### Verified against the CLI

Checked against Claude Code **2.1.223** by reading `claude --help`, and by parsing the complete
vector through `subprocess` with one deliberately bogus trailing flag — which makes the CLI stop at
its unknown-option check *before* the action handler, so the whole vector was proven to parse without
a model request being made. `VERIFIED_CLAUDE_FLAGS` in `tests/otel_inventory.py` records this, and
`test_every_flag_in_the_vector_is_recorded_as_verified_against_the_cli` keeps the record complete.

| Flag | Status |
| --- | --- |
| `-p` | in `--help`: "Print response and exit" |
| `--model sonnet` | in `--help`; `sonnet` named as a valid alias |
| `--permission-mode dontAsk` | in `--help`; choices are `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan` |
| `--tools ""` | in `--help`: "Use `\"\"` to disable all tools" |
| `--disable-slash-commands` | in `--help`: "Disable all skills" |
| `--max-turns 1` | **accepted but absent from `--help`** — re-verify on any version change |

`--tools` is the right flag and `--allowedTools` is not: the first restricts the available set,
the second pre-approves a list. Confusing them is how a canary ends up with every built-in tool
available, so `--allowedTools` / `--allowed-tools` are refused by name.

`--agent-command` exists as an escape hatch for naming the `claude` executable by path. A supplied
command must parse to the same vector; anything else is refused as a conflict, and
`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--allowedTools`,
`--allowed-tools`, `--disallowedTools`, `--disallowed-tools`, and every `--permission-mode` other
than `dontAsk` are refused by name.

### The later supervised run

Not part of this slice. It is a separate, supervised event that needs its own approval.

```powershell
$env:OTELCOL_PATH = 'C:\Tools\OpenTelemetry\otelcol-contrib\0.158.0\otelcol-contrib.exe'
.\.venv\Scripts\python.exe tests\otel_inventory.py `
    --output-dir 'C:\CodexData\canary\inventory1' `
    --approved-by '<the human approving this run>'
```

The agent vector deliberately does not appear on that command line. There is no command string for a
shell to mangle.

- **`--approved-by` is mandatory.** Without it the harness prints a refusal and starts nothing — no
  Collector, no child, no output directory.
- **The child runs in a temporary isolated directory** built by
  `agent_telemetry.build_claude_child_environment`, pointed at the run's own ephemeral loopback port.
  The directory holds the child's stdout — which is its reply — and is removed on every path,
  including a timeout.
- **An existing output directory is not overwritten.** `--replace-existing` replaces this harness's
  own three report files and nothing else; a directory holding anything else still stops the run.
- **The Collector and the child are reaped on every path**, including a startup timeout, an agent
  timeout, and a failure while building the report.

The run writes three files: `inventory-report.json`, `inventory-report.md`, and
`candidate-manifest.yaml`. A human reads the Markdown, merges the candidate manifest into
`ops/otel/claude-field-approvals.yaml`, decides each entry, sets `review.approved_by` and
`review.state: approved`, and re-runs. Approving a field is a data edit; it changes no executable
logic, and it needs no other file — the Collector log is not part of the identifier review.

### What Gate 6 does not change

- **Request-level Claude trace attribution stays BLOCKED** under Collector 0.158.0.
  `claude_code.llm_request` spans carry links, `filter/unredactable` drops them, and the manifest
  records that span name as `BLOCKED` — observing one after the privacy pipeline is a Gate 6 failure,
  not a new capability.
- **Forge persisted usage remains authoritative.** Nothing here feeds accounting, budgets, retries,
  task state, database schemas, or the public JSON contract.
- **Native agent metrics remain observational only.**
- **Nothing is enabled.** `ops/otel/collector-config.yaml` is unchanged, no production module imports
  the harness, and native Claude and Codex telemetry stay disabled.

## Codex telemetry canary (Gate 7 foundation)

**Gate 7 has not passed.** `tests/otel_codex_canary.py` is the harness for one supervised Codex
request that **has not been made**. No Codex process has run under it, no `-c` override has been
accepted by a real `codex --strict-config` invocation, and nothing in this section is evidence about
Codex. What exists is the apparatus and its refusals; what is missing is the run.

Codex differs from Claude in the two ways that decide the shape of this harness:

- **It is configured per invocation, not by environment variable.** Contract section 4 requires a
  `-c` override and forbids touching `~/.codex/config.toml`, which every Codex process on the machine
  reads. `--strict-config` then makes the override a real test: an unrecognized key fails the
  invocation instead of being ignored.
- **Its content-bearing events are logs.** Codex 0.146.0's own event definitions put `prompt`,
  `user.email`, and `user.account_id` on the logs signal. `otel.exporter` — the *logs* exporter — is
  therefore pinned to `none`, and the Collector still declares no logs pipeline beneath it.

### The exact `-c` override vector

Built by `agent_telemetry.build_codex_otel_overrides()` as separate argv elements, never a string:

```
-c otel.exporter="none"
-c otel.log_user_prompt=false
-c otel.trace_exporter={otlp-http={endpoint="http://127.0.0.1:<port>",protocol="binary",headers={}}}
-c otel.metrics_exporter={otlp-http={endpoint="http://127.0.0.1:<port>",protocol="binary",headers={}}}
-c otel.environment="forge-canary"
-c otel.span_attributes={"forge.invocation.id"="<32 hex>","forge.telemetry.contract.version"="1.0"}
```

`<port>` is the ephemeral loopback port the run's own Collector bound; `<32 hex>` is the invocation
identifier that run minted. Neither exists before the run starts, so neither can be typed on a
command line. `otel.tracestate` is never written — it is unbounded vendor text, and contract section 4
admits inbound trace context only. Values are TOML basic strings drawn from a bounded alphabet; a
value that would need escaping is **refused** rather than escaped, which also means no element of the
vector contains a backslash, a quote-inside-a-quote, or whitespace for a Windows shim to re-split.

#### What this vector does not yet prove

`--strict-config` acceptance is exactly what the supervised run is for. Three details are read off the
0.146.0 binary's configuration surface rather than off a successful invocation, and each is a way the
run can legitimately fail:

1. the nesting of an exporter variant — `{otlp-http={…}}` as an externally tagged table;
2. whether `headers` is a field of that variant, and whether an empty table is accepted;
3. whether `endpoint` is a **base** URL to which `/v1/traces` and `/v1/metrics` are appended, or a
   full signal URL. The base form is what the builder emits, because the exporter Codex embeds
   appends those paths when it reads a base endpoint from the environment.

Until one supervised run accepts it, none of the three may be described as settled, and the Codex
correlation mechanism stays **proposed**.

### The exact Codex vector

```
[0]  codex
[1]  --ask-for-approval      [2]  never
[3]  --sandbox               [4]  read-only
[5]  --model                 [6]  gpt-5.6-terra
[7]  -c                      [8]  model_reasoning_effort=low
[9..20]  the six -c otel overrides above
[21..38] --disable apps | browser_use | browser_use_external | goals | image_generation
                  | memories | multi_agent | plugins | skill_search
[39] exec
[40] --json  [41] --strict-config  [42] --ephemeral  [43] --ignore-user-config
[44] --skip-git-repo-check
[45] Reply with exactly CANARY_OK and nothing else.
```

Global options precede `exec` and subcommand options follow it, which is the placement `CodexRunner`
already uses. The `--disable` list is **read from `ForgeConfig`'s own default**, not retyped, so it
cannot drift from what Forge disables in ordinary runs. The prompt is the same one the Claude canary
uses, so the two gates differ in the agent and nothing else.

`python tests/otel_codex_canary.py --output-dir <dir> --dry-run` prints this vector by index with a
placeholder port and a placeholder identifier, prints the PowerShell command, and starts nothing.

Refused by name: `--dangerously-bypass-approvals-and-sandbox`, its `--yolo` alias, the removed
`--full-auto`, the `workspace-write` and `danger-full-access` sandboxes, any `--ask-for-approval`
other than `never`, any prompt other than the exact canary prompt, and any operator-supplied
`otel.*` override. `--codex-executable` names the executable by path; it is the only part of the
vector an operator may change.

### `CODEX_HOME` is preserved and never redirected

`build_codex_child_environment()` copies the environment it is handed, removes every inherited
`OTEL_*` and `CLAUDE_CODE_OTEL_*` name (compared case-insensitively), `TRACEPARENT`, and `TRACESTATE`,
and **adds nothing**. `CODEX_HOME` is not in either list:

- set by the operator → the child gets exactly that value, byte for byte;
- unset → the child has no `CODEX_HOME`.

Forge never creates, replaces, redirects, or deletes it, and never reads or writes any file under it.
`CODEX_HOME` selects credential, history, and session storage; relocating it would break login rather
than isolate telemetry. Isolation comes from `--ignore-user-config`, which skips
`$CODEX_HOME/config.toml` while still authenticating out of `CODEX_HOME`.

The removal is not decoration. Codex 0.146.0 embeds an OTLP exporter that reads the standard
`OTEL_EXPORTER_OTLP_*` variables and falls back to `http://localhost:4318`, so an inherited endpoint
or header variable would reach that exporter underneath the `-c` configuration. Forge's own Codex
configuration deliberately depends on **none** of those variables.

### What the supervised run will report

One JSON and one Markdown file under the chosen output directory, schema
`forge.codex.telemetry-canary/1`, with a section each for:

| Section | What it answers |
| --- | --- |
| canary process | Codex exit code, whether it timed out, JSONL lines, malformed lines, bounded event-type counts, whether exactly one visible assistant response came back and whether it was exactly `CANARY_OK` |
| telemetry transport | logs exporter, whether a logs pipeline is declared, log records accepted and refused, whether any log signal reached the exporter, Collector exit, whether the counts settled |
| correlation | three booleans: `forge.invocation.id` observed after redaction, its value matched the locally held one, `forge.telemetry.contract.version` observed |
| span accounting | accepted, exported, deliberately filtered, refused, send-failed, unresolved remainder, and the bounded by-name breakdown |
| metric accounting | metric points accepted, exported, refused, send-failed, unresolved remainder |
| privacy check | every check that ran, every finding, and the forbidden-output scan of the report itself |
| limitations | what a PASS still does not mean |

**The invocation identifier is never printed.** The report says the attribute was observed and that
its value equalled the one the harness is holding; it never carries the value. The rendered report is
scanned for a 32-character hexadecimal run — among nine other forbidden shapes — and a report that
matched one is replaced by a stub naming the pattern rather than written.

Gate 7 fails on any of: a nonzero unresolved span or metric remainder, an absent or non-matching
correlation attribute, any log record or logs pipeline, a malformed JSONL line, a reply that is not
exactly `CANARY_OK`, a timeout, a nonzero exit from Codex or the Collector, a privacy finding, or a
forbidden value in the report.

### The later supervised run

Not part of this slice. It is a separate, supervised event that needs its own approval, and it is the
only thing that can produce a Gate 7 `PASS`.

```powershell
$env:OTELCOL_PATH = 'C:\Tools\OpenTelemetry\otelcol-contrib\0.158.0\otelcol-contrib.exe'
.\.venv\Scripts\python.exe tests\otel_codex_canary.py `
    --output-dir 'C:\CodexData\canary\codex1' `
    --approved-by '<the human approving this run>'
```

- **`--approved-by` is mandatory.** Without it the harness prints a refusal and starts nothing — no
  Collector, no Codex, no output directory.
- **An existing output directory stops the run** before a Collector is started.
- **Codex runs in a fresh empty temporary directory** outside the repository, created under the system
  temporary directory and removed on every path including a timeout. It holds the child's JSONL, which
  contains the reply; the reply is reduced to counts inside that directory and the directory is then
  deleted. No message text leaves the harness.
- **The Collector and the Codex child are reaped on every path**, and the run is bounded by a
  five-minute timeout.

### What Gate 7 does not change

- **Gate 7 is not a field inventory.** It observes one invocation. No Codex resource, span, event, or
  metric attribute has been individually inspected or approved — that is **Gate 8**, and it needs its
  own manifest and its own supervised run.
- **Normal Forge execution remains unwired.** No production module imports these builders,
  `CodexRunner` emits no `-c otel` override and no `OTEL_*` variable, and native Codex telemetry stays
  disabled outside the harness. A test runs a real `CodexRunner.run()` against a recording executable
  and asserts both.
- **Traces and metrics are observational.** Forge persisted usage remains the authoritative basis for
  accounting, budgets, retries, and task state. Nothing here feeds any of them, and no schema or
  public JSON contract changes.
- **Native telemetry proves nothing about cost or quota.** Token counts on a span are the model's
  report of one request — not a bill, and not a remaining balance.

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
