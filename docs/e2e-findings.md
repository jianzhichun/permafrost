# Real Claude Code e2e — what running it actually revealed

Synthetic requests prove the transforms; only running **real Claude Code** proves
the thing works on real traffic and surfaces what real CC actually does. This is
the authoritative validation. Reproduce with
[`e2e/run_claude_code.sh`](../e2e/run_claude_code.sh) (needs a funded
`DEEPSEEK_API_KEY`).

## Setup

Headless `claude -p` on a small fix-the-bug task, fully isolated (throwaway
`CLAUDE_CONFIG_DIR` + project, dedicated proxy port), pointed at Permafrost →
`https://api.deepseek.com/anthropic`.

## Result

A 4-turn agentic task (read → edit → edit → respond), measured by the proxy:

```
requests        4
cache hit rate  66%   (41,728 hit / 21,339 miss tokens)
cost            $0.00324  vs $0.00896 all-miss  ->  64% saved
anchor resets   1
```

**64% real cost reduction on genuine Claude Code traffic.** The single anchor
reset is CC's lightweight preflight call (no tools, ~1K-token prompt) — a
different request shape from the agent loop, legitimately a different anchor.
The three agent-loop requests share one stable anchor.

## Finding 1 — DeepSeek re-renders before caching (body key order is irrelevant)

CC serializes its body as `{"model", "messages", "system", "tools", …}` — with
**`messages` first**. So the *raw JSON byte prefix* shared between consecutive
agent requests is only ~6% (the growing `messages` array sits at the front and
changes every turn). Yet the hit rate is 66%.

The only way both are true: **DeepSeek's Anthropic endpoint renders the request
into Anthropic's canonical order (`tools` → `system` → `messages`) and caches
that**, not the raw body bytes. Implications:

- The cacheable anchor really is `tools` + `system`, regardless of how CC orders
  the JSON keys — so Permafrost's tool-sort + system-stabilization is exactly the
  right lever.
- Permafrost's offline benchmark emulator models a *raw byte* prefix cache, which
  is a conservative proxy; the live numbers here are the ones to trust.

## Finding 2 — CC's billing-header nonce (`cch`)

CC injects a telemetry block as the **first system block**:

```
x-anthropic-billing-header: cc_version=2.1.170.acc; cc_entrypoint=sdk-cli; cch=bcc4d;
```

The `cch=` value is a **per-request nonce** (bcc4d → a8245 → 050b1 …) at the
front of `system`. Permafrost's `stabilize_metadata` pins it to a constant. This
dropped the proxy's observed anchor resets from **3 → 1** (cleaner diagnostics,
and protection for any byte-sensitive gateway). Honestly: on DeepSeek's endpoint
specifically it did **not** change the measured hit rate — DeepSeek tolerated the
nonce — but neutralizing a per-request nonce in the prefix is the correct thing
to do and makes `/permafrost:doctor` report real anchor changes instead of noise.

## Finding 3 — freeze+delta engages on real CC

`env_frozen: true` on real traffic: CC's environment/context block is pinned into
the cached anchor. In this session the env didn't change (`env_delta_lines: 0`),
so it cost zero per turn — exactly the intended steady state. Freeze's larger win
is long sessions where `git status` changes between turns; there it sends only
the changed lines instead of the whole block.

## Honest ceiling

66% on a 4-turn task is near the practical maximum: the preflight and the first
agent turn are unavoidably cold, and each turn adds genuinely new content (tool
results, the new user turn) that must be processed. Longer sessions amortize the
cold start and trend higher. The point Permafrost proves here: it keeps the big
`tools`+`system` anchor (~85 KB / ~21 K tokens of real CC prompt) stable enough
that DeepSeek serves it from cache turn after turn — a 64% bill cut for free.
