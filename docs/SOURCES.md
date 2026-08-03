# Design sources

The implementation follows the current Codex documentation for:

- non-interactive `codex exec`, JSONL event output, structured output schemas, and isolated-run safety;
- configuration fields for approval policy, sandbox mode, lifecycle features, logs, goals, memories, and multi-agent support;
- separate subagents/threads for noisy independent work;
- RTK's native Windows and Codex initialization path;
- Token Optimizer's Codex marketplace integration.

Review official documentation and third-party release notes before major upgrades because CLI and plugin interfaces continue to evolve.
