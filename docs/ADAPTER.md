# Native Codex adapter

The adapter runs **one caller assignment and exits**. It supplies focused
context to native Codex, retains raw output, executes explicit caller validation,
and returns a compact JSON handoff. It has no daemon, database, queue, automatic
review, repair loop, model routing, commit, merge, or worktree creation/deletion.
The existing `forge` controller and its evidence stay intact.

## Run it

From the installed checkout on Windows:

```powershell
.\forge-adapter.ps1 run --request C:\Jobs\assignment.json
.\forge-adapter.ps1 capabilities --request C:\Jobs\assignment.json
```

The equivalent Windows module entrypoint is
`.venv\Scripts\python.exe -m codex_forge.adapter`. The `forge-adapter` console
entrypoint becomes available after an editable installation or bootstrap.

For the single-assignment adapter on Linux (Python 3.11+, Git and native Codex
CLI **0.151.0**, on x86-64 or ARM64):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/forge-adapter capabilities --request /home/user/jobs/assignment.json
.venv/bin/forge-adapter run --request /home/user/jobs/assignment.json
# Equivalent module invocation:
.venv/bin/python -m codex_forge.adapter capabilities --request /home/user/jobs/assignment.json
```

Use Linux absolute paths in `workspace.path`, `artifact_root` and validation
argv; for example `/home/user/jobs/example-worktree`, `/home/user/job-evidence`
and `/home/user/project/.venv/bin/python`. Create and supply the linked worktree
yourself. Sign in through the native `codex login` flow using your ChatGPT
subscription; the adapter verifies that method in its sanitized child
environment. It never installs, copies or changes authentication. If you set
`CODEX_HOME`, supply an absolute path; otherwise Linux configuration audit and
native authentication both use the child `HOME` and its `.codex` directory.
The launch environment explicitly pins those home descriptors without changing
the parent environment or credentials.

Native Linux `codex` ELF binaries work directly. The pinned official npm
installation is supported too: a PATH symlink to `bin/codex.js` is resolved by
reading package metadata and locating `vendor/<target>/bin/codex` in the
matching optional platform package (or bundled vendor directory). The Node
launcher is never executed. This matches the
[pinned official launcher layout](https://github.com/openai/codex/blob/rust-v0.151.0/codex-cli/bin/codex.js).
Unknown launchers, package versions and architectures are refused. Native CLI
version, disabled extensions, sandbox policy and subscription authentication
still pass the same preflight checks after resolution.
The payload must stay within its selected resolved package directory. Normal
package-manager links to that package are supported; escaping payload links are
refused. The qualified executable's SHA256 is checked again when building the
launch arguments. This check assumes a trusted, stable installation; it is not
an immutable OS executable binding or a package-signature verification.
The selected native binary and any npm launcher must live outside the editable
worktree, including when reached through symlinks or relative PATH entries.
Workspace-supplied executables are refused before version or authentication
probes can execute them. Relative lookup may still select a trusted installation
outside the worktree; no ambient adapter PATH fallback is used.

No API key, SDK, proxy or cloud-provider fallback is used. `--executable`
selects a native `codex.exe` on Windows or `codex` on Linux, without arbitrary
extra arguments. `--request -`
reads stdin. SIGINT/SIGTERM (and SIGBREAK on Windows) request cancellation and
owned process-tree cleanup.

This Linux support covers the adapter Python/console entrypoint. Windows
bootstrap, scheduled services and the legacy autonomous controller are outside
this port. Restricted native jobs depend on sandbox support on the Linux host.
For a trusted job on an isolated development host, set the request's
`policy.sandbox` to `"danger-full-access"`. Forge starts native Codex with
approval policy `never` and records the selected mode in the launch arguments.
The assignment's Git audit still checks visible edits after execution.
The host account determines which files and commands the process can access.
For offline adapter checks, use `.venv/bin/python -m pytest tests/test_adapter_*.py`;
these fixtures execute harmless local subprocesses and use mocked model output,
not inference. Windows controller/service tests do not certify a Linux daemon.

`capabilities` performs version, configuration and native authentication
inspection, without model inference. It still requires a valid job request to
evaluate the requested policy. Its JSON report excludes the launch environment
and authentication response. A refused policy yields a typed error.

## Request: adapter/1

The request is a closed JSON object; unknown fields and versions are rejected.
The executable validation is in `adapter/contract.py`; published JSON Schemas
are packaged in `src/codex_forge/schemas/adapter-*.schema.json` and available
through `importlib.resources.files("codex_forge").joinpath("schemas", name)`
after wheel or editable installation. The adapter's generated native-output
schema is checked against that packaged final schema by the contract tests.

```json
{
  "contract_version": "adapter/1",
  "caller_id": "operator",
  "job_id": "example",
  "attempt_id": "a1",
  "role": "implementation",
  "workspace": {
    "path": "C:\\Jobs\\example-worktree",
    "base_revision": "<exact 40- or 64-character Git HEAD hash>",
    "expected_state": "clean"
  },
  "task": {
    "objective": "Implement the requested function.",
    "acceptance": ["The supplied authoritative gate passes."],
    "constraints": ["Do not change the validation command or commit."],
    "scope": ["src/example.py"],
    "out_of_scope": ["Other modules"]
  },
  "policy": {
    "sandbox": "workspace-write",
    "allowed_paths": ["src/example.py"],
    "tool_network": "unspecified",
    "search": false,
    "mcp": false,
    "plugins": false
  },
  "context": [],
  "model": "<caller-selected available model>",
  "effort": "medium",
  "deadline_s": 900,
  "artifact_root": "C:\\JobEvidence",
  "validation": {
    "authoritative": true,
    "commands": [{"argv": ["C:\\Python\\python.exe", "-m", "pytest", "tests"], "timeout_s": 120}]
  }
}
```

Supply an **existing linked Git worktree**, not a primary clone or subdirectory.
The adapter never creates it. HEAD must equal the full base hash, and the dirty
state must match. A dirty workspace additionally requires `expected_files`, an
exact map of Git-visible paths (plus `.git`) to full-file SHA256 hashes. A clean
workspace may supply these pins too. Job/attempt IDs are safe Windows path
components. Source symlinks, reparse points and submodules are rejected; current
inventory limits are 20,000 paths and 512 MiB of source.
Assume-unchanged, skip-worktree, sparse and unmerged index states are refused
because they can hide stale worktree contents from normal clean-state checks.

The caller must prevent concurrent editing of its assigned worktree. This
adapter provides no global workspace lock or snapshot-isolated filesystem.

`context` selects file excerpts: each entry has a contained relative `path`,
complete-file `sha256`, `max_bytes`, and optionally both `start_line` and
`end_line` (inclusive, one-based). All pins are checked even if the excerpt is
omitted. The task's strings and lists remain intact. An oversized contract is
rejected rather than clipped. Excerpts are reference data, not new authority.

Optional controls and defaults:

| Field | Default | Meaning |
| --- | ---: | --- |
| `context_bytes` | 24000 | Aggregate excerpt content bytes |
| `request_bytes` | 64000 | Complete assembled prompt bytes |
| `handoff_chars` | 8000 | Combined returned semantic text; 0 disables text cap |
| `output_bytes` | 8388608 | Combined raw model stdout/stderr and monitored final-file cap |
| `required_capabilities` | `[]` | Unknown or unverified required capabilities reject before inference |

Handoff lists retain at most eight entries, with omission metadata. Blockers
and decisions take priority and retain their slots when text is clipped. This
is **post-generation** reduction for the next consumer, not a generation-token
cap. Raw final output stays in evidence. Large validation output is summarized
around diagnostic lines, with full raw logs retained. Small output stays intact.

## Execution and evidence

Only the tested native CLI version is accepted. Preflight sanitizes a child
environment, classifies native login status as ChatGPT subscription, audits
configuration sources, and constructs closed arguments with forced ChatGPT
login, native provider, explicit sandbox/approval, disabled external extensions,
fresh ephemeral execution, JSON events and a final-output schema. Global
credentials/configuration are never rewritten. Unknown auth or policy fails
before inference; no API or provider fallback occurs.

On Windows every execution starts suspended, enters a kill-on-close Job Object,
then resumes. Ownership failure prevents execution. Deadline, SIGINT and output
overflow stop the owned tree; normal exit also cleans lingering descendants.
The adapter reports whether cleanup was verified. Standalone POSIX process
groups are lifecycle control, not confinement against hostile `setsid()` code.

Native final-output files are sampled while the process runs. One filesystem
write can exceed the configured cap before detection and termination; this is
not an OS storage quota. The adapter does not claim a native hard token/turn
cap, strict no-tools execution, per-file read jail or complete network isolation.
Required command-network denial is currently refused rather than guessed.
Inference/authentication traffic remains necessary. Selected context and allowed
edit paths are not access controls. Installed policy rules are preserved.

Evidence is created exclusively under `artifact_root/job_id/attempt_id` outside
the worktree and adapter repository. An existing attempt is rejected without
overwriting its evidence. The folder contains request, prompt, selected-context
metadata, source hashes, final schema, raw events/stderr/final bytes, validation
logs and the result. These artifacts may contain private code and are intended
for the trusted caller; there is no new public evidence-disclosure service.
On Linux, newly created job and attempt directories use mode 0700, so raw files
inside the attempt are not accessible to other unprivileged users through that
directory. Existing caller-owned parent directories are not reconfigured.
Source hashes use exact bytes, without line-ending or path-case normalization.

The edit audit hashes tracked and nonignored untracked source plus `.git`.
Allowed paths use case-sensitive Python `fnmatch` matching on slash-separated
paths; `*` can cross `/`. Ignored files and paths outside the worktree are not
audited. An audit is evidence, not a sandbox. Caller-owned HEAD and worktree
bindings must remain unchanged. No changes are silently reverted.
Semantic index entries and flags, current branch identity and local Git
configuration are also pinned; staging, skip-worktree flags or branch changes
are rejected even when worktree bytes and HEAD's commit are unchanged.

Validation commands use exact argv, run once in order, and stop at the first
failure. These are **trusted host commands**, not OS-sandboxed commands. The
caller must choose and protect authoritative tests; candidate-written tests
do not replace that specification. The model cannot choose a different gate.
The job deadline covers preflight, model execution and validation; remaining
time bounds each validation process. Preflight probes have their own short
limits and may finish after a very short requested job deadline, but inference
will not start after that deadline.

## Result and exit status

Every valid run returns `contract_version: "adapter/1"`, caller identities and
separate transport, protocol, semantic, validation, edit-audit, usage and error
fields. Semantic status is the model's claim; `factory_acceptance` is always
`not_determined`.

`completed` requires clean transport and valid final schema, no model blockers
or pending decisions, an in-scope edit audit, every expected authoritative
validation command passing, and unchanged source after the last edit and gate.
No gate, a nonauthoritative gate or an incomplete model claim yields `partial`.
Other statuses are `rejected_preflight`, `failed_transport`, `failed_protocol`,
and `validation_failed`. Missing/malformed output is a protocol error, not a
semantic `blocked` answer. Partial raw output is retained; there is no automatic
regeneration or retry.
Successful protocol admission also requires a terminal completed turn and
closure of every started model item; valid JSON alone is insufficient.

Validation states distinguish `not_requested`, `not_run`, `incomplete`, `passed`,
`failed` and `stale`. A gate that changes source is stale, even with exit code 0.
CLI exit 0 means adapter `completed`; exit 2 means a reported noncompletion.
For `capabilities`, exit 0 means the requested preflight succeeded, without an
inference run or a validated coding result.

Usage retains each observed `turn.completed` event. Cached input and reasoning
output are subsets, not extra totals. Each count is known, unknown or invalid;
missing is never zero. Multiple events remain unaggregated until delta versus
cumulative semantics are established. Failed/partial usage is retained where
available. These numbers do not establish subscription allowance or dollars.

## Deferred capabilities

Review, resume and proposal/application lanes are explicitly unavailable in
this version. They need separate transport/trust/capability tests before use.
There is no hidden fallback to the legacy controller. No matched model benchmark
or percentage token-savings claim accompanies this implementation. Factory
architecture, routing, workflows, dashboard and model training remain outside
this adapter change.
