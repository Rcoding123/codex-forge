# Design sources

The implementation follows the current Codex documentation for:

- non-interactive `codex exec`, JSONL event output, structured output schemas, and isolated-run safety;
- configuration fields for approval policy, sandbox mode, lifecycle features, logs, goals, memories, and multi-agent support;
- separate Codex invocations for implementation and independent review;
- RTK's native Windows and Codex initialization path;
- Token Optimizer's Codex marketplace integration.

Review official documentation and third-party release notes before major upgrades because CLI and plugin interfaces continue to evolve.

- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching): keep stable
  instructions before variable task data. Forge relies on documented provider behavior rather than
  guessing Codex CLI cache settings; layout changes do not guarantee measured savings.
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode): structured output
  and JSONL execution evidence remain separate from controller validation truth.
