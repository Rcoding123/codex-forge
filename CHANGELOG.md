# Changelog

All notable changes to Codex Forge are documented here. This project follows semantic versioning.

## [Unreleased]

### Fixed

- Review phases no longer run under a sandbox that cannot launch a command. Codex enforces the
  `read-only` and `workspace-write` sandboxes on Windows with a restricted token, which cannot reach
  the non-interactive window station a service-style logon owns, so every reviewer command — down to
  `pwd` — aborted with `STATUS_DLL_INIT_FAILED` (`0xC0000142`, reported as `-1073741502` or
  `3221225794`) before running. Implementation and repair phases were unaffected because they use the
  configured `danger-full-access` sandbox, which uses no restricted token. `CodexRunner` now probes
  whether the sandbox can launch a process, using the same command construction and worker
  environment a phase uses, and only a recognized process-launch failure changes the mode.

### Changed

- A review that could not inspect anything is reported as `REVIEW_INFRASTRUCTURE_BLOCKED` rather than
  as a `changes_required` verdict. It starts no repair phase, makes no further model call, retains
  the failing commands and exit codes in `review-infrastructure-block.json` next to the review
  evidence, and fails the task closed — passing validation does not substitute for the independent
  review that did not happen. New bounded telemetry vocabulary:
  `review_infrastructure_blocked` and `review_infrastructure`.

### Added

- Optional local OpenTelemetry tracing: `forge.task` → `forge.attempt` → `forge.phase` spans with
  bounded phase names, categorized token usage from the shared usage normalizer, and a loopback-only
  OTLP/gRPC exporter. Disabled by default; a `forge.toml` without `[telemetry]` is unchanged.
- `ops/otel/collector-config.yaml` and PowerShell helpers that run an already-installed Collector
  locally. Nothing downloads a Collector, and the pipeline exports only through the debug exporter.
- `docs/TELEMETRY.md` covering emitted attributes, privacy guarantees, verification, and rollback.
- Gate 6 field-inventory harness (`tests/otel_inventory.py`) and its versioned approval manifest
  (`ops/otel/claude-field-approvals.yaml`). The harness inventories every field that survives the
  certified privacy pipeline, reducing dynamic values to bounded classes and printing a static value
  only through an explicit allowlist. It declares no Collector component, so its only input is the
  debug exporter's post-privacy output. Every distinct unknown schema identifier gets its own entry,
  rendered exactly when it passes a strict safe-identifier policy and fingerprinted with SHA-256 when
  it does not, and each run emits a `candidate-manifest.yaml` a reviewer can merge without reading
  the Collector log. **Gate 6 has not returned a PASS verdict**: a supervised run was made and
  `human-operator` approved the 29 fields it observed, but the confirming run against the approved
  manifest is outstanding, and the harness refuses a PASS verdict until it has one.
- Gate 7 Codex telemetry-canary foundation: a pure Codex child-environment builder that removes
  inherited `OTEL_*`, `TRACEPARENT`, and `TRACESTATE` while adding nothing and preserving `CODEX_HOME`
  exactly; a pure builder for the proposed per-invocation `-c otel` override vector (logs exporter
  `none`, prompt logging off, OTLP/HTTP binary traces and metrics to a loopback Collector, and the two
  canonical correlation attributes); and `tests/otel_codex_canary.py`, the approval-gated harness that
  would run one minimal Codex request and report the `forge.codex.telemetry-canary/1` result. **Gate 7
  has since passed**: one supervised codex-cli 0.146.0 request on 2026-08-07 had its `-c` override
  accepted under `--strict-config`, put 224 spans and 96 metric points through the certified pipeline,
  correlated them by `forge.invocation.id`, accepted no log record, and balanced every count. Normal
  Forge execution is unchanged and native Codex telemetry remains disabled outside the harness.
- Gate 8 Codex field inventory: `ops/otel/codex-field-approvals.yaml`, the approval manifest pinned to
  codex-cli 0.146.0 under Collector 0.158.0, alongside `tests/otel_codex_evidence.py` (the Gate 7
  run's post-redaction surface, distilled to a table of names and bounded descriptors) and
  `tests/otel_codex_inventory.py` (the Codex manifest loader and the Gate 8 verdict). The Gate 6
  parser, classifier, safe-identifier policy, comparison, and forbidden-output scan are reused
  unchanged; `otel_inventory.load_manifest` and `build_document` gained schema, gate, and verdict
  parameters so a second agent can share them without borrowing Claude's approvals. **Gate 8 has
  passed** on the recorded Gate 7 evidence, with no second Codex request: 381 fields observed, 372
  approved, 9 refused, none UNKNOWN or UNAPPROVED, no redacted key surviving, and a zero unresolved
  remainder. Every Codex span event is refused — Codex names an event after the Rust source location
  that emitted it, `keep_keys` cannot reach an event name, and no Collector 0.158.0 processor rewrites
  one. Nothing is enabled by the approval, and Forge persisted usage remains the authoritative basis
  for accounting, budgets, retries, and task state.

### Changed

- The per-task token budget is enforced against new work only: `fresh_input_tokens + output_tokens`,
  centralized in `codex_forge.usage.budget_used_tokens`. Cached input reads consumed the configured
  budget before this change even though the provider had already served them, so a task doing a small
  amount of new work could be stopped by its own prompt cache. `reasoning_output_tokens` and
  `cache_write_input_tokens` are still never added on top, because the reported contract carries each
  inside the parent count already being charged. `token_usage.budget_basis` now reports
  `fresh_input_plus_output`; `total_tokens` and `raw_total_tokens` keep their raw
  `input_tokens + output_tokens` traffic meaning and remain informational, so schema 1.0 is unchanged.
  Enforcement still happens at both phase boundaries — before a phase starts and after a completed
  phase is recorded — and one in-flight phase can still overshoot the remainder, because neither the
  runner nor codex-cli 0.146.0 exposes a per-request token cap.

## [0.2.0] - 2026-08-02

### Added

- Durable recovery of interrupted active tasks while preserving worktree/branch state.
- Crash-released controller singleton locking.
- Identical-failure loop detection, cumulative task token budgets, and durable model/effort usage.
- Scoped follow-up review, retry failure context, and retained phase prompts.
- Authenticated `forge doctor` checks.
- Controlled verified service restart script and hardened S4U scheduled-task policy.
- Release manifest verification and expanded behavioral regression suite.
- Publication, security, contribution, architecture, operations, and sanitized audit documentation.
- Reproducible global RTK fallback and raw-pytest policy during bootstrap.

### Changed

- Corrected Codex CLI global-option ordering and Windows shim argument handling.
- Preserved exact native validation exit codes and UTF-8 output.
- Propagated the private Forge Python environment to workers.
- Made routine implementation/review use the balanced route at medium effort; strongest routing is explicit escalation only.
- Disabled unrelated worker features and bounded raw-output summaries while retaining full logs.
- Synchronized generated configuration and idempotent bootstrap behavior.
- Disabled RTK pytest compression pending an upgrade because RTK 0.44.2 can misreport passing quiet suites.

### Fixed

- Twenty-two defects demonstrated by the final production-readiness audit; all were corrected or safely mitigated and regression-covered.

### Known limitations

- Full Windows OS reboot recovery has not yet been exercised; controlled scheduled-task restart recovery passed.
- RTK pytest compression remains disabled until upgraded and rebenchmarked.

## [0.1.4]

- Preserved exact native-process exit codes through the PowerShell validation wrapper.

## [0.1.3]

- Corrected Windows command-shim invocation and private-Python PATH propagation.

## [0.1.0]

- Initial Windows-first controller, SQLite queue, worktree execution, validation, review, commit, and evidence workflow.
