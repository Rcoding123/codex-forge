# v0.2.0 audit summary

## Verdict

**PASS** — the final Windows production-readiness audit completed on 2026-08-02.

The audit exercised the real controller, queue, Codex worker path, isolated Git worktrees, controller-side validation, independent review, repair, local commit, evidence generation, loop and budget controls, and scheduled-service restart recovery. It did not rely on README claims as proof.

## Validated results

| Gate | Result |
|---|---:|
| Forge doctor, including unattended Codex auth | PASS |
| Unit tests at audited snapshot | 35/35 PASS |
| Ruff, compilation, TOML/schema parsing, SQLite integrity, PowerShell syntax | PASS |
| Windows command runner matrix | 14/14 PASS |
| Real queued end-to-end smoke task | PASS |
| Interactive approval requests | 0 |
| Isolated branch/worktree and unchanged main checkout | PASS |
| Independent validation/review/repair/final commit/evidence | PASS |
| Controlled scheduled-task restart and state recovery | PASS |
| Blocked task does not freeze READY queue | PASS |

The smoke task deliberately included a validation defect and a blocking review finding. Forge repaired both within configured bounds, revalidated independently, produced a clean local task commit, and retained evidence tied to the actual commit.

## Corrections included in v0.2.0

Twenty-two demonstrated defects were corrected and regression-covered:

1. Codex approval option ordering.
2. Windows reasoning-effort argument escaping.
3. Missing executable false-success handling.
4. UTF-8 command output handling.
5. Private-Python propagation to service workers.
6. Interrupted active-task recovery.
7. Controller singleton enforcement.
8. Non-interactive scheduled-task identity, restart, and working-directory policy.
9. Verified daemon process-tree shutdown.
10. Identical deterministic failure detection.
11. Manual retry attempt enforcement.
12. Per-task cumulative token budget.
13. Durable reasoning-effort recording.
14. Removal of unused/ambiguous model routes.
15. Medium reasoning for routine review.
16. Removal of unrelated worker capabilities.
17. Read-only RTK fallback policy.
18. Safe mitigation for incorrect RTK pytest compression.
19. Idempotent bootstrap configuration and service behavior.
20. Unattended authentication in doctor.
21. Failure-aware retries, scoped follow-up review, and retained prompts.
22. Release-manifest integrity verification.

## Model and token observations

Normal implementation/review used the balanced model at medium effort. High effort was used only after a blocking finding; the strongest route and multi-agent execution were not used for the medium-risk smoke. Local inventory and deterministic validation used no model.

The adversarial smoke consumed approximately 1.31 million total tokens across forced defects, two deliberate budget stops, repair, and completion. That figure is a stress/audit measurement, not a normal-task target. A two-million-token task ceiling was retained for production configuration and should be monitored with real workloads.

## Limitations

- RTK 0.44.2's passing-pytest compressor is unreliable. Active guidance requires raw proxy pytest output until an upgrade is rebenchmarked.
- Scheduled-task restart recovery was proven, but a full Windows operating-system reboot recovery test remains outstanding.
- `danger-full-access` with zero approvals depends on the VPS being a dedicated isolation boundary.
- Model identifiers depend on installed Codex CLI/account availability and must be revalidated when tooling changes.

The detailed audit evidence contains machine-local paths and operational data and is intentionally retained outside this publication repository.
