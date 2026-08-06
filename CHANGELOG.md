# Changelog

All notable changes to Codex Forge are documented here. This project follows semantic versioning.

## [Unreleased]

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
  the Collector log. **Gate 6 has not passed**: no supervised inventory run has been made, the
  manifest approves nothing, and the harness refuses a PASS verdict until a human is named in it.

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
