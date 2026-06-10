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

## Finding 4 — cache identity includes the client's header fingerprint

Discovered while building keepalive, through a chain of live probes:

1. Replaying a real CC request **byte-identically** 8s later, but carrying only
   the auth headers → **0% hit**.
2. Replaying that replay (same reduced headers) → **99.8% hit**.
3. Replaying a real CC request byte-identically **with CC's full header set**
   (user-agent, accept, x-stainless-\*, …) → **99.9% hit (20,864/20,880)**.

So two requests only share DeepSeek cache identity when both the rendered
request *and the client header fingerprint* match. Consequences: Permafrost's
keepalive replays the last request **unchanged in body and headers**; and any
sidecar tooling that talks to the same endpoint with different headers (scripts,
SDKs) will never warm or reuse the agent's cache.

Along the way we also falsified two cheaper designs, on the live API:

- **Anchor-only pre-warm** (tools+system + placeholder message): 0% hit — the
  warm request's prefix is shorter than any persisted cache unit, and DeepSeek
  only hits when the request "fully matches a cache prefix unit". Cross-session
  disk pre-warm was therefore removed.
- **Parameter-modified replay** (`max_tokens: 1`, `stream: false` to save output
  cost): 0% hit against the original. The replay must be unchanged; the price of
  a keepalive is one regenerated reply at hit-price input — still ~50× cheaper
  than letting the prefix go cold.

## Finding 5 — per-session and per-lineage accounting on real traffic

Two sequential CC sessions through one proxy: per-session buckets read cleanly
(session 1 cold: 50% hit; session 2: 66%), and lineage-bucketed churn detection
reported **zero false anchor resets** — the preflight call (no tools) and the
agent loop are separate lineages, so their alternation no longer pollutes the
churn signal. Within both lineages, transitions = 0 across all 7 requests.

## Honest ceiling

66% on a 4-turn task is near the practical maximum: the preflight and the first
agent turn are unavoidably cold, and each turn adds genuinely new content (tool
results, the new user turn) that must be processed. Longer sessions amortize the
cold start and trend higher. The point Permafrost proves here: it keeps the big
`tools`+`system` anchor (~85 KB / ~21 K tokens of real CC prompt) stable enough
that DeepSeek serves it from cache turn after turn — a 64% bill cut for free.
