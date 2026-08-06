# Shared Agent Telemetry Contract

**Contract version:** `agent-telemetry-contract/1.0`

This contract governs how Codex Forge may observe the two coding agents it drives — Claude Code
2.1.223 and Codex 0.146.0 — using their native OpenTelemetry support. It is the output of two
completed audits and the input to every later implementation slice.

**This document authorizes nothing at runtime.** No native agent telemetry is enabled by any change
that accompanies this contract, and none may be enabled until the rollout gates in
[Section 11](#11--rollout-gates) have been passed in order. A slice that enables an agent exporter
without citing a passed gate is out of contract.

## Status vocabulary

Every claim below is tagged. Do not promote a tag without the evidence the tag requires.

| Tag | Meaning |
| --- | --- |
| **[CERTIFIED]** | Implemented in this repository today and covered by the test suite. |
| **[AUDITED]** | Observed in vendor source or documentation during the audit. Not exercised against a live agent under Forge, and therefore not proven. |
| **[UNRESOLVED]** | Named in the audit and still unknown. MUST NOT be assumed in either direction. |
| **[REQUIRED]** | A future implementation obligation created by this contract. Not yet built. |

An **[AUDITED]** capability MUST NOT be described in code comments, documentation, dashboards, or
commit messages as if it were **[CERTIFIED]**.

## Normative language

**MUST** / **MUST NOT** are absolute. **SHOULD** admits a documented exception with human approval.
**MAY** is genuinely optional. These follow RFC 2119 usage.

---

## 1 — Purpose and scope

Forge needs per-model-request visibility that it does not have today. `forge.phase` covers one whole
agent invocation, and that invocation may issue many model requests internally that Forge cannot see
(documented as a known limitation in [Telemetry](TELEMETRY.md)). Both agents now emit native
OpenTelemetry signals that would close that gap.

Both agents also emit, in their default configurations, material Forge must never export: prompts,
model responses, source-derived content, tool commands and tool output, provider errors, and account
identity. The purpose of this contract is to define the narrow subset that MAY be observed, and the
mandatory machinery that keeps everything else from leaving the host.

**In scope:** span and metric shape, correlation identifiers, trust boundaries, redaction
obligations, child-process configuration, token accounting semantics, Collector requirements, drift
policy, rollout gates, and the test matrix that gates each step.

**Out of scope for this contract version:** enabling anything. See
[Section 14](#14--non-goals) for the explicit exclusions.

### What this contract protects

Exported telemetry MUST NOT allow a reader of the telemetry backend to reconstruct: what was asked
of a model, what a model answered, what code or diff was involved, what commands ran or what they
printed, who the operator is, which account or organization paid for the call, where on disk the
work happened, which repository or branch it touched, or what any unrelated Claude/Codex process on
the same machine was doing.

---

## 2 — Trust boundaries

Four parties. Each owns a disjoint set of decisions. A slice that moves a decision across these
lines is a contract change, not an implementation detail.

### Forge owns

- `forge.task`, `forge.attempt`, and `forge.phase` spans and their attribute vocabularies.
- The agent invocation lifecycle: process construction, environment, arguments, timeout,
  cancellation, and retry.
- Correlation identifiers, including `forge.invocation.id`.
- Collector configuration in this repository (`ops/otel/collector-config.yaml`).
- Redaction policy — what must be deleted, and where.
- Persisted accounting (`codex_forge.usage` normalization, budgets, task state).
- Every enforcement decision: budgets, retries, validation verdicts, failure classification.

Forge does **not** own anything inside an agent process.

### Claude Code and Codex own

- Their native request spans, metrics, and internal instrumentation.
- Their internal session, turn, request, retry, and subagent structure and the names of the
  attributes describing it.
- The stability of those names across their own releases — which is to say, Forge has no guarantee
  of stability at all. See [Section 10](#10--version-and-drift-policy).

Forge MUST treat every agent-emitted field as untrusted input that may change, disappear, or gain a
sibling field carrying content, on any vendor upgrade.

### The Collector owns

- Local signal reception on loopback.
- **Mandatory redaction.** The Collector is the enforcement point, not merely a convenience: it is
  the last stage that sees agent data before it is batched and exported, and it is the only stage
  that sees fields Forge did not know existed.
- Routing signals to pipelines, and refusing to construct a pipeline that lacks required redaction.
- Export to approved local backends only.

### The human owns

- Telemetry enablement. No slice may flip an agent telemetry default to on.
- Supervised canary approval, per agent, separately.
- Acceptance of **any** newly exported field — including a field that merely appeared after a vendor
  upgrade. Silence is not acceptance.
- Any change to accounting authority ([Section 8](#8--token-semantics)).

---

## 3 — Canonical hierarchy

The target logical hierarchy:

```
forge.task
└── forge.attempt
    └── forge.phase
        └── agent invocation
            └── agent session or interaction
                └── turn
                    └── model request
```

The first three levels are **[CERTIFIED]** today. Everything below `forge.phase` is **[REQUIRED]**
and unbuilt.

**Not every provider can express every level as direct parentage.** This hierarchy is the *logical*
model, not a promise about span parent pointers:

- **Claude** **[AUDITED]**: non-interactive Claude accepts an inbound W3C `TRACEPARENT`, so the root
  Claude interaction span can become a genuine child of `forge.phase` in one trace. The levels
  between interaction and model request are Claude's own and may not map one-to-one onto "session /
  turn / request".
- **Codex** **[AUDITED]**: native request spans exist, but attaching them to a Forge trace depends
  on a per-invocation configuration override whose syntax and propagation are **[UNRESOLVED]**. Codex
  levels may have to be joined by attribute rather than by parentage.
- **Metrics** have no parentage at all. Metric points MUST be joined to a phase by
  `forge.invocation.id` ([Section 4](#4--correlation-contract)), never by trace structure.

Consumers MUST NOT assume that the number of spans at any level below `forge.phase` is stable, or
that a missing level means no work occurred there.

---

## 4 — Correlation contract

One provider-neutral identifier joins agent telemetry to the Forge phase that produced it.

### The identifier

**Attribute name:** `forge.invocation.id`

**Format** **[REQUIRED]**:

- Generated from at least **128 bits** of cryptographically secure randomness (e.g. `secrets`, not
  `random`).
- Lowercase ASCII, from a bounded alphabet (hex or an unpadded lowercase base32 is appropriate).
- Bounded length; the value MUST fit the existing 120-character attribute cap with room to spare.
- **MUST NOT** contain, encode, hash, or otherwise derive from: the task ID, the project ID, any
  filesystem path, branch name, commit hash, repository URL, prompt text, model response, account
  identifier, timestamp, hostname, or process ID. A deterministic derivative of a sensitive value is
  a sensitive value.
- Exactly **one new value per worker-process invocation**.
- **Never reused** across attempts, retries, cancellations, phases, or providers. Two phases in one
  attempt get two distinct values; a retried invocation gets a new value.

### Where it appears

The same opaque identifier MUST appear on both sides of the join:

1. The Forge `forge.phase` span, as a span attribute.
2. The corresponding agent telemetry — as a resource attribute where the provider allows it, and
   otherwise on the agent's spans or metric points.

### Why exporting it is acceptable

`forge.invocation.id` is exported deliberately. It is **not** an exception to the privacy policy in
[Section 6](#6--privacy-policy), because it carries no encoded operator, account, or repository
information — it is uniformly random and meaningless outside a Forge process's own memory. It is
*necessary* because the agent process exits before its telemetry is consumed, and no other durable
join survives that exit: trace context alone cannot join metrics, and every alternative join key
(task ID, path, branch) is exactly the kind of value this contract forbids exporting.

Anyone reviewing this must confirm the identifier is random, not a hash or encoding of something
sensitive. An identifier "derived from the task ID" fails this contract even if hashed.

### Claude correlation

- Forge **SHOULD** inject a valid W3C `TRACEPARENT` derived from the active `forge.phase` span into
  the child environment. **[REQUIRED]**
- Claude interaction spans **SHOULD** then appear as descendants of `forge.phase` in one trace.
  **[AUDITED]** — inbound acceptance was observed in the audit; it has not been exercised under
  Forge.
- `forge.invocation.id` **MUST** *also* be supplied as a resource attribute, so that Claude **metric
  points**, which carry no trace context, can still be joined to the phase. Trace parentage alone is
  insufficient.
- `CLAUDE_CODE_PROPAGATE_TRACEPARENT` **MUST NOT** be enabled. That setting forwards trace context
  onto Claude's outbound requests, which would send Forge's internal trace and span identifiers to
  Anthropic. Inbound context is accepted; outbound propagation is forbidden. This is a hard
  boundary, not a default.

### Codex correlation

- Forge **MUST** carry per-invocation telemetry configuration through a **per-invocation CLI
  configuration override** (`-c`). **[REQUIRED]**
- Forge **MUST NOT** modify global Codex configuration. `~/.codex/config.toml` is operator-owned and
  is read by every Codex process on the machine, including ones Forge did not start.
- Forge **MUST NOT** set `CODEX_HOME`. Relocating the Codex home directory changes credential,
  history, and session storage for the child and is not a telemetry mechanism.
- The exact `-c` strict-config syntax, and whether custom attributes propagate to *every* request
  span, are **[UNRESOLVED]**.
- Until a dedicated contract test passes (Gate 7/8), the Codex correlation mechanism **MUST** be
  described everywhere — code, docs, commit messages — as **proposed**, not active.

---

## 5 — Signal policy

| Producer | Traces | Metrics | Logs / events |
| --- | --- | --- | --- |
| **Forge** | **Allowed** — **[CERTIFIED]** today | **Deferred** — not in this contract version | **Forbidden** |
| **Claude Code** | Allowed **only after** redaction certification (Gates 3, 5, 6) | Allowed **only after** redaction certification (Gates 3, 5, 6) | **Forbidden** |
| **Codex** | Allowed **only after** runtime certification (Gates 3, 7, 8) | **Deferred** initially | **Forbidden** |

"Deferred" means: not authorized by this contract version, and requires a contract revision rather
than an implementation decision.

### Logs are forbidden through two independent barriers

Agent logs and events are the highest-risk signal in this system. Claude logs/events can carry
prompts, responses, tool details, tool output, source-derived content, plugin names, agent names,
and errors **[AUDITED]**. Codex logs can carry prompts, tool payloads, agent messages, provider
errors, account email, and account identifiers **[AUDITED]**.

Two independent barriers MUST both hold. Neither alone is sufficient, and neither may be removed
because the other is present:

1. **Agent-side exporter disabled.** `OTEL_LOGS_EXPORTER` MUST be `none` for Claude. The Codex logs
   exporter MUST remain disabled.
2. **No Collector logs pipeline.** `ops/otel/collector-config.yaml` MUST NOT declare a `logs`
   pipeline. **[CERTIFIED]** — the current configuration declares only `traces`.

Barrier 1 fails open if a future agent version changes its default or reads a variable Forge does
not neutralize. Barrier 2 fails closed: with no logs pipeline, a log record that reaches the
receiver has nowhere to go.

---

## 6 — Privacy policy

### Forbidden content

Exported telemetry MUST NOT contain any of the following, in any signal, in any attribute, at any
level:

- prompts
- system prompts
- assistant responses
- model response bodies
- source code
- diffs
- tool commands
- tool arguments
- tool output
- validation output
- review prose
- exception messages
- credentials
- authentication headers
- environment-variable values
- account email
- account ID
- organization ID
- installation ID
- absolute paths
- repository URLs
- branch names
- commit hashes
- raw task or project descriptions

This list is a restatement, for agent telemetry, of the guarantee Forge telemetry already keeps
today (see "Privacy and redaction guarantees" in [Telemetry](TELEMETRY.md)). The difference is
enforcement point: for Forge spans the guarantee holds because Forge never constructs the value; for
agent spans it holds only because the Collector deletes it.

### Ungated identity attributes

Claude attaches these identity attributes **without any configuration gate** **[AUDITED]**:

- `user.email`
- `organization.id`
- `user.id`

There is no agent-side setting that suppresses them. Removing them is therefore **mandatory
Collector-side work**, and it is the single reason Gate 3 must pass before any canary.

Additional identifiers that may appear **[AUDITED]**: `user.account_uuid`, `user.account_id`,
`session.id`, `ccr.session.id`.

### Minimum deletion set

The following attribute keys MUST be deleted wherever they appear:

```
user.email
organization.id
user.id
user.account_uuid
user.account_id
session.id
ccr.session.id
workspace.host_paths
user_prompt
system_prompt
system_prompt_preview
user_system_prompt
assistant_response
response
tool_input
tool_parameters
tool_output
payload
content
error
error.message
url.full
url.query
```

**This is a minimum safety set, not a proof of sufficiency.** It enumerates field names observed
during an audit of two specific versions. It is not evidence that a future Claude Code or Codex
release uses the same names, and it says nothing about names neither audit encountered. A field
carrying a prompt under a name not on this list is still a contract violation.

### Allowlist over denylist

Because the deletion set cannot be complete, implementations **SHOULD** use an **allowlist-based
export policy** wherever the Collector makes one practical: keep an explicitly enumerated set of
attributes and drop everything else. A denylist is correct only for what is known to be dangerous; an
allowlist is correct for what is known to be safe, and only an allowlist survives a vendor adding a
field. Where an allowlist is impractical for a given signal, the deletion set above is the floor, and
the gap MUST be recorded as a known risk.

### Where redaction applies

Redaction MUST apply to all four surfaces. A processor configured for only one of them does not
satisfy this contract:

- span attributes
- span events (including event attributes — an event body is as dangerous as a span attribute)
- metric attributes
- **resource attributes** — this is where Claude's ungated identity attributes live, and it is the
  surface most often forgotten

Redaction **MUST execute before batching and export**. A processor placed after `batch` in a
pipeline does not satisfy this contract even if it names the right keys.

---

## 7 — Child-process configuration

All agent telemetry settings **MUST** be invocation-scoped. **[REQUIRED]**

- The parent `os.environ` **MUST NOT** be mutated — not even temporarily. The controller is
  concurrent, and a temporary mutation is visible to every other thread and every unrelated child
  started during the window.
- The child environment **MUST** be constructed as a **new dictionary** and passed explicitly to the
  subprocess call.
- The implementation **MUST explicitly neutralize inherited telemetry configuration** that could
  otherwise enable an unapproved exporter, endpoint, header set, resource attribute, or content
  logging in the child. An operator's pre-existing `OTEL_*` variables, or a vendor-specific variable
  that turns on prompt logging, MUST NOT reach the agent process merely because it was present in
  the controller's environment. Neutralization MUST be explicit and enumerated, not assumed.
- **No telemetry credentials or headers are permitted.** There is no authenticated backend in this
  design, so there is nothing for a header to authenticate to; a header setting reaching an agent is
  evidence of an unapproved destination.
- **Only loopback Collector endpoints are permitted** initially.
- **Unrelated operator-launched Claude and Codex processes MUST remain unaffected.** This is the
  direct consequence of the three preceding rules plus the prohibitions on global Codex config and
  `CODEX_HOME` in [Section 4](#4--correlation-contract). If an operator runs `claude` or `codex` in
  their own shell while Forge is running, that process MUST emit exactly what it emitted before
  Forge existed.

---

## 8 — Token semantics

### Defined quantities

| Term | Meaning |
| --- | --- |
| **raw input tokens** | Input tokens as the provider reports them for a request. |
| **cached-input tokens** | Input tokens served from a prompt cache. |
| **cache-write tokens** | Tokens written into a prompt cache. Codex reports this as cache-write; Claude reports the comparable quantity as cache-creation. |
| **fresh input tokens** | Input tokens that were neither cached nor cache-written — a derived quantity, never a provider field. |
| **output tokens** | Output tokens as the provider reports them. |
| **reasoning-output tokens** | Output tokens attributed to reasoning. Codex appears to report these **[AUDITED]**; Claude does not expose a distinct count **[AUDITED]**. |
| **raw total tokens** | The total on the accounting basis in force, not a sum of every field present. |

### Arithmetic rules

- Cached input is a **subset of, or a separate provider category from**, raw input depending on the
  provider. It **MUST NOT** be blindly added to input totals.
- Reasoning output **MUST NOT** be added to output when it is already a subset of output.
- Fresh input **MUST** be derived only where provider semantics establish that `input − cached` is
  valid. Where they do not, fresh input is undefined and MUST be omitted, not guessed.
- **No derived value may become negative.** A negative result means the subset assumption was wrong;
  the correct response is to omit the field, not to clamp silently and carry on.
- Unknown, missing, or malformed values **MUST fail safely** — omit the attribute. A malformed usage
  payload MUST NOT produce a zero that a consumer will read as "no tokens used".

### No double counting

Request spans, turn spans, metrics, events, and Forge's persisted JSON may all describe **the same
usage**. They **MUST NOT** be summed together. One accounting basis **MUST** be selected explicitly
and recorded, exactly as `forge.usage.accounting_basis` and `forge.usage.budget_basis` do today
**[CERTIFIED]**.

### Accounting authority

Initial authority is unambiguous:

- **Forge persisted usage remains the authoritative task-accounting source.** **[CERTIFIED]**
- Native agent telemetry is **observational evidence only**.
- Native values **MUST NOT** change task state, budgets, retries, or failure decisions. A phase that
  a native span says was expensive is not a budget event.
- **Reconciliation is required before any authority change** (Gate 9), and the change itself
  requires separate human approval (Gate 10).

### Claude-specific notes **[AUDITED]**

- Claude cost fields are **local estimates**, computed client-side. They are not subscription
  billing, not invoiced cost, and not quota consumption. They MUST NOT be presented as spend.
- Claude does not expose a distinct reasoning-token count on the audited request spans.
- The audited request spans carry the **requested** model, not the served one. Whether any surface
  exposes the served model is **[UNRESOLVED]** ([Section 13](#13--open-questions)).
- Subscription quota is unavailable.

### Codex-specific notes **[AUDITED]**

- Cost and quota are both unavailable. Codex telemetry cannot answer "what did this cost".
- Reasoning-output tokens **appear** available on request spans. Not exercised under Forge.
- The audited request spans carry the **requested** model. Served-model visibility is
  **[UNRESOLVED]** ([Section 13](#13--open-questions)).
- **Multiple `turn.completed` records** per invocation require a separate accounting review before
  any turn-level quantity is aggregated. Summing them without that review is exactly the
  double-counting this section forbids. **[UNRESOLVED]**

Neither agent's native telemetry proves actual cost, subscription quota, the served model, or the
exact cause of context growth. Do not build any consumer that implies otherwise.

---

## 9 — Collector contract

`ops/otel/collector-config.yaml` MUST satisfy all of the following.

| Requirement | Status |
| --- | --- |
| Loopback receivers only (`127.0.0.1`), never `0.0.0.0` | **[CERTIFIED]** |
| No external exporter — nothing that opens an outbound connection | **[CERTIFIED]** |
| `debug` exporter during certification | **[CERTIFIED]** |
| `memory_limiter` processor, ahead of batching | **[CERTIFIED]** |
| `batch` processor | **[CERTIFIED]** |
| **Mandatory privacy processor, before `batch`**, on every enabled agent pipeline | **[REQUIRED]** |
| `traces` pipeline | **[CERTIFIED]** |
| `metrics` pipeline **only when explicitly needed** | **[REQUIRED]** |
| **No `logs` pipeline** | **[CERTIFIED]** |

### Fail closed

The Collector configuration **MUST fail closed** when a required redaction processor is missing from
an enabled agent pipeline. A pipeline that receives agent signals and lacks its privacy processor
MUST NOT start, and MUST NOT degrade to passing data through unredacted. This is verified by Gate 3
and by the test matrix item "missing privacy processor fails certification".

A `metrics` pipeline MUST NOT be added speculatively. It is added when a specific, approved consumer
needs it, and it carries the same mandatory privacy processor as `traces`.

---

## 10 — Version and drift policy

**Contract version:** `agent-telemetry-contract/1.0`

Implementations **MUST** record the contract version they were built against as a span attribute:

```
forge.telemetry.contract.version = "1.0"
```

This makes it possible to identify, from telemetry alone, which contract a given trace was produced
under — including after the contract has moved on.

### Drift invalidates assumptions

Upgrading **Claude Code**, **Codex**, the **OpenTelemetry libraries**, or the **Collector**
invalidates every native-field assumption in this contract until compatibility tests pass. The
audited findings here describe Claude Code 2.1.223 and Codex 0.146.0 and nothing else.

- **No wildcard trust of new fields or events.** A newly appearing attribute is not covered by
  "similar to one we already allow".
- **New signals and attributes are denied until reviewed.** The default disposition of anything
  unrecognized is deletion, not export.
- A vendor upgrade that adds a field carrying content is the expected failure mode, not a remote
  one. The allowlist recommendation in [Section 6](#6--privacy-policy) exists specifically for it.

---

## 11 — Rollout gates

This order is required. **A failure at any gate blocks every later gate**, and gates MUST NOT be run
concurrently or reordered for convenience.

| Gate | Requirement |
| --- | --- |
| **1** | Shared contract approved. |
| **2** | Collector redaction and metrics changes implemented. |
| **3** | Synthetic hostile OTLP payloads prove identity and content removal from **span, event, metric, and resource** attributes. |
| **4** | Child-process environment and correlation helpers implemented, **disabled by default**. |
| **5** | One supervised **minimal Claude canary**. |
| **6** | Inspection of **every** emitted Claude resource, span, event, and metric attribute. |
| **7** | One **separately supervised** minimal Codex canary. |
| **8** | Inspection of **every** emitted Codex resource, span, event, and metric attribute. |
| **9** | Reconciliation against existing Forge usage over representative tasks. |
| **10** | **Separate approval** before any accounting or enforcement change. |

Notes that make the ordering meaningful:

- Gate 3 uses **synthetic** payloads precisely so that redaction is proven before any real agent
  identity data has ever been received. It MUST NOT be satisfied by inspecting a real canary.
- Gate 4 delivers code that is **off by default**. Merging enablement is not part of Gate 4.
- Gates 5–6 (Claude) and 7–8 (Codex) are separate supervision events. Approving Claude does not
  approve Codex; the two agents have different identity exposure and different unresolved risks.
- Gates 6 and 8 require inspecting **every** attribute, not a sample. "We saw the ones we expected"
  does not pass — the risk is the attribute nobody expected.
- Gate 9 is evidence-gathering only. It does not change authority; Gate 10 does.

Gate 1 is the approval of this document. Everything after it is unbuilt.

---

## 12 — Required test matrix

Every item below is **[REQUIRED]** and unbuilt. Each maps to the gate that cannot pass without it.

| # | Test | Gate |
| --- | --- | --- |
| 1 | Disabled-by-default behavior — no agent telemetry configuration is emitted unless explicitly enabled | 4 |
| 2 | No parent-environment mutation — `os.environ` is byte-identical before and after an invocation | 4 |
| 3 | Exact child-environment allowlist — the child receives precisely the enumerated variables and nothing else | 4 |
| 4 | Inherited `OTEL_*` configuration neutralization — pre-existing operator variables do not reach the child | 4 |
| 5 | Invocation-ID format and uniqueness — alphabet, length, and no collision across attempts, retries, phases, providers | 4 |
| 6 | No identifiers or paths encoded in invocation IDs — the value is independent of task ID, project ID, path, branch, and commit | 4 |
| 7 | Valid W3C `TRACEPARENT` — well-formed, and derived from the active `forge.phase` span | 4 |
| 8 | Claude parentage under `forge.phase` | 5, 6 |
| 9 | **No traceparent propagation to Anthropic** — `CLAUDE_CODE_PROPAGATE_TRACEPARENT` is never enabled | 4 |
| 10 | Codex `-c` strict-config acceptance — the override is accepted and does not fail the invocation | 7 |
| 11 | Windows shim quoting — the override survives the shim without splitting or mangling | 7 |
| 12 | Collector redaction of **span attributes** | 3 |
| 13 | Collector redaction of **span events** | 3 |
| 14 | Collector redaction of **metric attributes** | 3 |
| 15 | Collector redaction of **resource attributes** | 3 |
| 16 | Missing privacy processor **fails certification** — an enabled agent pipeline without it does not start | 3 |
| 17 | No logs pipeline | 3 |
| 18 | No external exporter | 3 |
| 19 | No token double-counting — request, turn, metric, and persisted values are not summed | 9 |
| 20 | Existing Forge accounting unchanged | 9 |
| 21 | Task states and budgets unchanged | 9 |
| 22 | Cancellation and retry lifecycle — a cancelled or retried invocation produces a fresh invocation ID and leaks no state | 4 |
| 23 | Version-drift rejection — an unrecognized agent version or unrecognized attribute is denied, not passed through | 10 |

---

## 13 — Open questions

These remain unresolved. They MUST NOT be closed by assumption, and any slice that depends on one
MUST say so explicitly.

### Codex

- Exact strict-config nesting for `-c` telemetry overrides.
- Whether custom span attributes reach **all** request spans, or only some.
- Whether account identity appears on Codex **trace** resources (it is known to appear in logs).
- Served-model visibility.
- Multi-turn completion semantics — what multiple `turn.completed` records mean for aggregation.

### Claude

- Exact subagent token attribution — whether subagent usage is nested, duplicated, or separate.
- Retry token treatment — whether a retried request's tokens appear once or per attempt.
- Served-model visibility.
- Reasoning-token visibility.
- Future stability of the ungated identity field names.

### Both

- **Subscription quota remains unavailable.** Neither agent exposes it.
- **The exact cause of context growth remains unavailable.** Token counts show that context grew,
  not why.
- **Vendor upgrades may add new fields**, including fields carrying content or identity, without
  notice.

---

## 14 — Non-goals

Explicitly excluded from this contract and from every slice that implements it:

- **Prompt or response logging.** Not in any form, not truncated, not hashed, not "previews".
- **Tool-content logging** — commands, arguments, or output.
- **User analytics.**
- **Employee or operator monitoring.** This telemetry describes machine work, not people.
- **Quota prediction.**
- **Billing reconciliation.** Neither agent exposes billable cost; see
  [Section 8](#8--token-semantics).
- **Automatic enforcement changes.** No native value may alter a budget, retry, or verdict.
- **External telemetry backends.** Loopback only.
- **Global Claude or Codex configuration changes.**
- **`CODEX_HOME` changes.**
- **Logs pipelines.**
- **Dashboards** — out of scope for this slice.

---

## Related documents

- [Telemetry](TELEMETRY.md) — the certified Forge trace slice, its attributes, and how to run it.
- [Architecture](ARCHITECTURE.md) — control flow and the wider trust boundaries.
- [Control-plane JSON contract](CONTROL-PLANE-API.md) — the authoritative persisted usage this
  contract defers to.
