# Engineering Lessons — Codex Forge

Purpose: preserve Forge-specific engineering laws learned from real factory failures so Claude and Codex do not repeat them.

This file complements `AGENTS.md`, `docs/ARCHITECTURE.md`, and `docs/CONTROL-PLANE-API.md`.
It is not a changelog. Code, schemas, and tests remain executable truth.

## Read order for implementation work

1. Read `AGENTS.md`.
2. Read `CLAUDE.md` when using Claude Code.
3. Read `docs/ARCHITECTURE.md` and the relevant section of `docs/CONTROL-PLANE-API.md`.
4. Read this file.
5. Inspect the owning code/schema/tests before changing behavior.

## 1. Forge owns governed execution

- Forge owns task persistence, implementation, review, repair, validation, and task/evidence truth.
- External clients, including Shared Chat, must use supported Forge contracts.
- External clients do not open or mutate `forge.db` directly.
- Do not create a second execution authority in a client.

## 2. Task contracts must be complete and lossless

- Forge can enforce only the specification truth it actually receives.
- Architecture decisions and constraints are binding requirements, distinct from acceptance criteria.
- Scope/out-of-scope define implementation boundaries.
- Validation commands are deterministic evidence requirements.
- Preserve exact contract text through persistence and prompts.
- Contract changes must be explicit and tested across the boundary.

## 3. Tests do not redefine requirements

- Candidate-authored tests are evidence.
- Green tests cannot override unmet acceptance criteria or binding requirements.
- Review must cover the authoritative task contract.
- Conformance must fail closed on missing, duplicate, contradictory, paraphrased, or unevidenced reports.

## 4. Only validated reviewer output is semantic review

- Raw reviewer output is untrusted.
- A semantic `ReviewResult` exists only after successful parse and schema validation.
- Nonzero exit, missing output, malformed JSON, and schema-invalid output are infrastructure/protocol failures.
- Do not launch repair without a valid semantic `changes_required` verdict.
- Exit code 0 alone is not semantic success.
- Schema-valid bytes are necessary, not sufficient: a review that launched a command and never
  recorded its outcome asserted a verdict on evidence its own run never produced, so command
  closure is part of admission and an unclosed review is refused as infrastructure, whichever way
  the verdict fell.

## 5. Infrastructure failure is not implementation failure

- Preserve `REVIEW_INFRASTRUCTURE_BLOCKED` ownership through retries and loop detection.
- Loop detection is detail, not a replacement failure class.
- Review transport/protocol failures must never become candidate-quality findings.
- Retry identity must use controller-owned stable facts, not provider prose or volatile identifiers.

## 6. Provider text is evidence, not control-plane state

- Provider messages, request ids, paths, stderr, and malformed outputs belong in raw attempt artifacts.
- Durable summaries use controller-owned classifications and safe structured facts only.
- Do not use provider-generated strings in failure fingerprints.
- Do not duplicate raw provider output into database error fields or summary evidence.

## 7. Evidence is governed history

- Attempt evidence is immutable.
- Terminal evidence must describe authoritative final `base..candidate` Git state, not merely the last repair delta.
- Task deletion removes operational registry state, not evidence.
- Retained evidence must remain readable through supported evidence commands.
- A live task row always wins over retained filesystem evidence for the same task id.
- Multiple retained evidence roots for an absent task must fail closed as ambiguous.

## 8. Evidence paths are not casual filesystem paths

- Treat stored paths as data, not authority.
- Re-prove containment and identity before reading/removing.
- Avoid symlink/reparse traversal.
- Do not expose raw filesystem paths in structured errors when the contract forbids it.
- Keep raw diagnostics in the appropriate raw artifact channel.

## 9. Task deletion is not cancellation

- Active task states cannot be deleted.
- Deletion and daemon claiming must serialize at the authoritative DB write boundary.
- A non-running task may be removed from operational registry state.
- Runs/decisions owned by the task are removed in FK-safe transactional order.
- Cleanup after durable DB deletion is best-effort/fail-closed and must not resurrect the task.

## 10. Preserve verification-relevant Git history

- Removing a task may remove its disposable worktree.
- Do not delete branches/refs if doing so could make retained evidence unverifiable.
- Do not run generic Git GC as part of deletion.
- User repositories and evidence roots are not disposable Forge workspace.

## 11. Forge project deletion is conservative

- A Forge project can be deleted only when it has zero task rows.
- Project deletion never implicitly cascades task deletion.
- Delete only Forge-owned disposable workspace.
- Retain user repositories, evidence, and verification-relevant history.

## 12. Namespace and path identity must fail closed

- Task ids and project names must be safe physical path components on Windows.
- Case aliases, trailing-dot/space aliases, reserved device names, and unsafe components must not silently create multiple physical identities.
- Validation and collision checks belong at authoritative insertion boundaries.
- Do not let a new live task adopt retained evidence from a prior task generation.

## 13. External cleanup must prove ownership

Before removing a worktree or workspace:
- prove the target is a plain owned directory;
- verify it lies inside the expected Forge-owned root;
- protect repositories, other workspaces, live task checkouts, config roots, and foreign registrations;
- reconcile Git worktree registration state;
- fail closed when repository inventory is unavailable or ambiguous.

## 14. Identity must be explicit

- Task ids are exact, not fuzzy.
- Project identity is exact.
- Attempt identity is different from task identity.
- Evidence artifact identity must be deterministic and stable.
- Do not infer identity from names, chronology, or “latest”.

## 15. One transition from untrusted bytes to trusted state

For every external tool/model result, identify the exact transition:

untrusted bytes
-> parse/validate
-> trusted typed state
-> controller decision

Anything failing before the typed state boundary is infrastructure/protocol failure.

## 16. Test seams and adversarial races

Do not stop at happy-path unit tests.

Test:
- task claim vs delete;
- delete vs worktree cleanup;
- retained evidence after task/project deletion;
- task-id reuse and ambiguity;
- malformed reviewer output;
- duplicate/contradictory conformance;
- multi-attempt final evidence;
- filesystem alias/symlink/reparse cases;
- cleanup partial success;
- DB transaction rollback on conditional-delete mismatch.

## 17. Contracts should remain small

- Prefer existing CLI + JSON envelopes over new services.
- Prefer argv over shell strings.
- Prefer additive fields over parallel schemas.
- Do not introduce an HTTP Forge control plane merely because a client needs a new command.
- Avoid new taxonomies when an existing stable error class is sufficient.

## 18. Documentation and change discipline

- API/contract changes require tests and matching contract documentation.
- Historical evidence is not rewritten to make a new implementation easier.
- Keep `AGENTS.md` concise and authoritative for repo engineering law.
- Keep `CLAUDE.md` thin and pointed at `AGENTS.md`.
- Runtime `forge/{slug}` branches are Forge artifacts; developer work uses `agent/*` branches.
- Stage explicit paths, use PRs, and do not push directly to `main`.

## 19. Completion gates

Run the repository’s canonical completion gates:

- `python -m pytest`
- `python -m ruff check .`
- `python -m compileall -q src tests`
- `git diff --check`

`python -m pytest` includes MANIFEST integrity coverage.

## 20. Simplicity rule

Before adding machinery, ask:

1. Can the existing task/controller authority own it?
2. Can the existing CLI/JSON contract be extended?
3. Can the current DB transaction boundary serialize it?
4. Can an existing typed error/result express it?
5. Can obsolete code be deleted instead?

Prefer a smaller fail-closed design over a broader framework.
