---
description: Explain how to launch Claude Code through the Permafrost proxy
---

Explain to the user how to route Claude Code through Permafrost so DeepSeek's cache hits.

Key point first: **`ANTHROPIC_BASE_URL` is read once when `claude` starts**, so the *current* session cannot be re-routed — this takes effect on the next launch.

Give them the two options:

1. **One-shot wrapper** (nothing persisted):
   ```
   permafrost wrap -- [any claude args]
   ```
   This starts the proxy if needed, sets `ANTHROPIC_BASE_URL` + `ENABLE_TOOL_SEARCH=true` for that child process only, and execs `claude`.

2. **Persistent** — copy the `env` block from `settings.example.json` into `~/.claude/settings.json`, set `ANTHROPIC_API_KEY` to their DeepSeek key, run `permafrost up`, then start `claude` normally.

Remind them the upstream defaults to `https://api.deepseek.com/anthropic` and can be changed with `PERMAFROST_UPSTREAM`. Offer to write the settings block for them if they confirm the path.
