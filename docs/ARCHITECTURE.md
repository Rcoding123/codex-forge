# Architecture

## Control plane

The Python controller owns durable state and decides when work is allowed to advance. Codex is a worker, not the source of truth.

## Trust boundaries

- VPS: isolated host with unrestricted local Codex access.
- Project worktree: file ownership boundary for a single task.
- Controller: owns state changes, validation, Git commit, retry limits, and evidence.
- Codex worker: proposes and implements changes inside a worktree.
- Reviewer: separate read-only Codex run.

## Why controller-side validation

A model's statement that tests passed is not accepted as evidence. The controller reruns configured commands directly and stores their complete stdout/stderr.

## Why new Codex runs instead of one endless thread

Fresh bounded phases reduce context pollution and make token usage attributable. Relevant failures, review findings, and learned patterns are injected explicitly.

## Persistence

SQLite uses WAL mode. Every task, run, decision, attempt, branch, and result survives process restarts. Raw logs and patches are stored outside the repository on the durable data disk.
