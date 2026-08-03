# Security policy

## Supported release

Security fixes are currently applied to the latest `0.2.x` release line.

## Deployment boundary

Codex Forge deliberately runs Codex non-interactively with `approval_policy = "never"`. Its recommended isolated-VPS configuration uses `danger-full-access`. Git worktrees provide engineering isolation, not an operating-system sandbox.

Operate Forge only on a dedicated development or simulation VPS that contains no unrelated personal secrets, production credentials, live brokerage access, live order-routing services, or production-deployment authority. Restrict network and account access at the host boundary.

## Secrets and persistent state

- Never commit `forge.toml`, `.codex`, `auth.json`, credentials, environment files, databases, logs, sessions, evidence, or task worktrees.
- Keep the Codex home and Forge data root outside the source checkout.
- Use the same dedicated Windows account for Codex authentication and the scheduled task.
- Treat prompts, JSONL events, raw command output, patches, and task objectives as potentially sensitive evidence.
- Review task repositories independently; Forge's private repository status does not make managed project content safe to disclose.

The repository `.gitignore` blocks common local-state and credential paths, but ignore rules are not a secret-management system. Scan tracked content and Git history before every publication.

## Prohibited scope

Do not add or operate live broker connectivity, live trading credentials, order placement/routing, withdrawals, production deployment, credential harvesting, or unrelated-repository access. Simulation, replay/playback, historical-data recording, offline analysis, GUI automation, and local tests are in scope.

## Reporting a vulnerability

Do not open a public issue containing exploit details or secrets. Use the private repository's security-advisory channel, or contact the repository owner through an agreed private channel. Include affected version, impact, reproduction steps, and a redacted diagnostic bundle. Never attach authentication files or raw secret values.

## Response priorities

Credential exposure, isolation bypass, unauthorized remote push, retry/loop escape, evidence deletion, task-state corruption, and command exit-code falsification are treated as high priority. Preserve evidence, stop the scheduled task if containment requires it, rotate affected credentials outside Forge, and avoid rewriting history until a clean remediation plan exists.
