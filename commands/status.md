---
description: Show Permafrost's live DeepSeek cache hit rate and dollar savings
allowed-tools: Bash(curl:*), Bash(*/cli/permafrost:*)
---

Fetch the live Permafrost proxy stats and report them to the user.

Run this and interpret the JSON:

!`curl -fsS "http://127.0.0.1:${PERMAFROST_PORT:-8787}/permafrost/stats" 2>/dev/null || echo '{"error":"proxy not reachable — start it with: permafrost up"}'`

Summarize for the user in 3-4 lines: the cache **hit rate**, the **hit/miss token** split, the **cost so far vs. the all-miss baseline** (how much Permafrost saved), and the number of **prefix resets** (how many times the cache anchor changed — ideally 0). If the proxy isn't reachable, tell them to run `permafrost up`.
