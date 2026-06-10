---
description: Diagnose what is busting Claude Code's DeepSeek prefix cache
allowed-tools: Bash(curl:*)
---

Diagnose the DeepSeek prefix cache for this session.

Pull the proxy's alignment report and prefix-change history:

!`curl -fsS "http://127.0.0.1:${PERMAFROST_PORT:-8787}/permafrost/doctor" 2>/dev/null || echo '{"error":"proxy not reachable — start it with: permafrost up"}'`

Also check the two environment knobs that most often break the cache under a custom endpoint:

!`echo "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL:-<unset>}  ENABLE_TOOL_SEARCH=${ENABLE_TOOL_SEARCH:-<unset>}  PERMAFROST_MODE=${PERMAFROST_MODE:-aggressive}"`

Then explain to the user, in plain language:
- Whether the cache **anchor (tools + system) is staying frozen** or churning, and the count of resets.
- Any **volatile content** still detected in the system prefix (dates, git status, UUIDs, hashes) and what it costs.
- If `ENABLE_TOOL_SEARCH` is not `true` while `ANTHROPIC_BASE_URL` is a custom host, warn that Claude Code is likely re-inlining the full tool set every turn (a major buster) and that it must be set **before** launching `claude`.
- Concrete next steps (e.g. switch to `PERMAFROST_MODE=aggressive`, set the env var, restart the session).
