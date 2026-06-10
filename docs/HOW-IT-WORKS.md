# How Permafrost works

## One picture

```
  Claude Code  ──Anthropic /v1/messages──▶  Permafrost proxy  ──▶  DeepSeek
   (unchanged)        (localhost:8787)         align_request()     /anthropic
                                                     │
                                       freezes tools+system bytes
                                       records real hit/miss tokens
```

Claude Code already speaks the Anthropic Messages API. DeepSeek exposes an
Anthropic-compatible endpoint (`https://api.deepseek.com/anthropic`). So
Permafrost needs **no protocol translation** — it is a passthrough whose only
job is to make the cache-relevant bytes of each request stable, then stream the
upstream reply back verbatim.

## The cache anchor

DeepSeek caches a request's prefix from byte 0, in fixed-size units (its live
granularity measures ~64 tokens; Reasonix's probe confirms this). Render order
is `tools` → `system` → `messages`, so the reusable part of every request is the
**anchor** = `tools` + `system`. If two requests share a byte-identical anchor,
DeepSeek serves all of it from cache at ~1/50th the price. Permafrost's entire
design is: *keep the anchor byte-identical across turns.*

It tracks the anchor as a fingerprint — `sha256(canonical(tools, system))[:12]`
— and surfaces every time it changes via `/permafrost/doctor`. A healthy session
shows **0 anchor resets**.

## The pipeline (`proxy/permafrost_align.py`)

Each request runs through `align_request(body, mode)`:

| Step | Function | Mode | Effect |
|---|---|---|---|
| Strip cache markers | `strip_cache_control` | safe, aggressive | Removes every `cache_control` key (DeepSeek ignores them; their positions drift) |
| Sort tools | `sort_tools` | safe, aggressive | Deterministic `(name, canonical-json)` order, immune to MCP arrival timing |
| Relocate volatile | `relocate_volatile` | aggressive | Moves env/context blocks (date, git status, UUIDs, hashes) out of the anchor onto the latest turn — same content, later position |
| Canonical serialize | `canonical_dumps` | all | Compact separators, `ensure_ascii=False`, stable byte output |

`safe` mode does only the provably-lossless transforms (re-ordering and
serialization). `aggressive` adds relocation, which **moves** content within the
request but never drops it — the model still sees the date and the git status,
just after the cached history instead of ahead of it. `off` is a measurement
baseline (no changes, but still records cache stats).

## Modes and when to use them

- **`aggressive`** (default) — for real coding sessions, where `git status`
  changes every turn. The only mode that drives anchor resets to 0 when CC ships
  a live environment block.
- **`safe`** — if you want zero semantic re-ordering and your system prompt has
  no volatile content (or you've already moved it out yourself).
- **`off`** — to measure your un-aligned baseline and prove the savings.

## Measuring cache activity

DeepSeek and Anthropic report cache usage under different field names;
`normalize_usage` folds both into `{input, hit, miss, output}`:

- DeepSeek: `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
- Anthropic: `usage.cache_read_input_tokens` / `input_tokens`

The proxy sniffs these from both non-streaming JSON and streaming SSE
(`message_start` carries input/cache usage; `message_delta` carries output), and
keeps a session running total: hit rate, dollar cost vs. an all-miss baseline,
and the count of prefix resets. `GET /permafrost/stats` returns it; the
statusline renders it as `❄ 88% cache hit · $0.12 saved`.

## Cold-anchor coalescing

A frozen anchor only helps if the cache for it actually exists. DeepSeek writes
its cache **asynchronously** — a prefix becomes readable only after the request
that wrote it has begun streaming (our live probe saw a repeat 4s later still
miss, and a 6s-later one hit). Claude Code's `Task` tool fans out subagents in
**parallel**: N requests that share one brand-new anchor fire at once, and
because none of them can read a cache the others are still writing, all N pay the
cold-miss price.

The coalescer (`Coalescer` in `permafrost_proxy.py`) collapses that burst:

1. The first request on an **unseen** anchor becomes the **leader** and goes
   straight through (it warms the cache).
2. Concurrent **same-anchor** requests become **followers** and block on a
   per-anchor gate.
3. When the leader's response **starts streaming** (first upstream byte), the
   gate opens and the followers proceed — now reading the warm prefix. This is
   the documented "fire one, await the first token, then fan out the rest"
   pattern, enforced at the proxy.
4. Once the leader finishes a usable (`<400`) response, the anchor is marked
   **warm** and all future requests pass through with no wait.

Guarantees: a **lone** request is its own leader and is never delayed; a follower
never waits past `PERMAFROST_COALESCE_TIMEOUT_S` (deadlock guard); a leader that
can't reach the upstream drops the anchor so the next request retries cleanly.
`PERMAFROST_COALESCE_SETTLE_MS` adds an optional pause after release for the
async write to settle. Honest caveat: releasing at the leader's first byte means
the *very first* follower may still partially miss if DeepSeek's write hasn't
landed; the rest of the burst, and every later burst on the now-warm anchor, hit.

## Why a proxy and not just hooks

A plugin's hooks and commands never touch the request bytes — only a process at
`ANTHROPIC_BASE_URL` can rewrite `tools`/`system`/serialization. So Permafrost is
a proxy *and* a plugin: the proxy owns the wire, the plugin owns the env
hardening, the diagnostics, and the statusline. See
[cache-busters.md](./cache-busters.md) for the full list of what it's defending
against.

## Provenance

The techniques are drawn from two open-source projects, generalized into a
provider-agnostic passthrough:

- **DeepSeek-Reasonix** — a DeepSeek-native agent tuned around the prefix cache.
  Source of: deterministic tool sorting + schema canonicalization, freezing the
  system prefix and *riding the turn tail* for volatile state, the
  `prompt_cache_hit_tokens` accounting, and the "anchor fingerprint" diagnostic.
- **Headroom** — a context-compression proxy. Source of: the proxy interception
  pattern, byte-faithful canonical serialization, session-sticky `anthropic-beta`
  merging, and the `ENABLE_TOOL_SEARCH` finding. Headroom's `CacheAligner` is
  *detector-only* (it warns about volatile content); Permafrost's aggressive mode
  takes the next step and **relocates** it.
