<div align="center"><pre>
   ___  ___ ___ __  __  _   ___ ___  ___  ___ _____
  | _ \| __| _ \  \/  | /_\ | __| _ \/ _ \/ __|_   _|
  |  _/| _||   / |\/| |/ _ \| _||   / (_) \__ \ | |
  |_|  |___|_|_\_|  |_/_/ \_\_| |_|_\\___/|___/ |_|
        freeze the prefix · melt the bill
</pre></div>

<p align="center"><strong>A Claude Code plugin that freezes your prompt prefix so DeepSeek's automatic cache always hits.</strong></p>

<p align="center">
  cache-stable passthrough proxy · deterministic tool order · volatile-content relocation · live hit-rate statusline · zero deps
</p>

<p align="center"><strong>Every $100 of Claude API traffic runs for ~$3.20 through Permafrost + DeepSeek — same requests, live-measured. (~$8.80 without Permafrost.)</strong></p>

---

Point Claude Code at DeepSeek and you get a coding agent for cents on the dollar
— *if* DeepSeek's cache keeps hitting. It usually doesn't. Claude Code reshuffles
its tool list when MCP servers connect, bakes today's date and a live `git
status` into the system prompt, slides `cache_control` markers around, and — under
a custom endpoint — stops deferring tools and re-inlines the whole tool set every
turn. Every one of those changes the **front** of the request, and DeepSeek's
cache only hits on a prefix that matches **from byte 0**. So you quietly pay the
full (≈50×) miss price on tokens that should have been ~free.

**Permafrost** sits between Claude Code and DeepSeek and rewrites the
cache-relevant bytes of every request so the `tools + system` anchor stays
byte-identical turn after turn — then streams the reply straight back.

```
  Claude Code ──Anthropic /v1/messages──▶ Permafrost ──▶ DeepSeek /anthropic
   (unchanged)        127.0.0.1:8787     freeze prefix       (no translation —
                                          + record hits       both speak Anthropic)
```

## The math: same workload, four ways

The exact token traffic from our real Claude Code e2e run (4 requests: 41,728
cache-hit + 21,339 cache-miss input tokens, 591 output tokens), priced four ways
at current official rates:

| Same workload on… | Cost | vs. Claude w/ caching |
|---|---:|---:|
| Claude Sonnet 4.6, caching busted | $0.1981 | — |
| Claude Sonnet 4.6, native caching working (66% hit) | $0.1014 | 1× |
| DeepSeek v4-flash, bare env-var switch (cache all-miss) | $0.00896 † | **11× cheaper** |
| **DeepSeek v4-flash + Permafrost (66% hit)** | **$0.00324 †** | **31× cheaper (−96.8%)** |

† live-measured, not estimated. The 31× factors cleanly: **11× from DeepSeek's
pricing × 2.8× from Permafrost** keeping the cache hitting. Unit prices behind
it (USD/1M tokens): cache-hit input $0.30 vs **$0.0028** (107×), miss input
$3.00 vs $0.14 (21×), output $15 vs $0.28 (54×). On the Opus tier
(`claude-opus*` maps to v4-pro) the same workload goes $0.169 → $0.0099, **17×
cheaper**.

A neat cross-check: Claude Code itself priced one of our e2e requests at
`total_cost_usd: $0.0625` (its own Sonnet-rate accounting); the same request
through Permafrost → DeepSeek billed **$0.0029**.

> **Honest caveats:** (1) this compares *price for identical traffic*, not model
> quality — `deepseek-v4-flash` is not Claude Sonnet 4.6; whether the trade is
> worth it is your call. (2) It's API-metered vs. API-metered; Claude Max
> subscriptions price differently. (3) Rows 1→3 are DeepSeek's pricing doing the
> work; rows 3→4 (−64%) are what Permafrost adds on top.

## Proof

Offline benchmark against a faithful emulator of DeepSeek's prefix cache
(prefix-from-byte-0, block-quantized), replaying a 12-turn Claude-Code-shaped
conversation. `off` = no alignment; `aggressive` = the default.

| Scenario | Mode | Cache hit rate | Anchor resets | Cost (USD) |
|---|---|---:|---:|---:|
| C: realistic CC (tool churn + live git/env) | **off** | 66.4% | 11 | $0.00339 |
| C: realistic CC (tool churn + live git/env) | **aggressive** | **88.4%** | **0** | **$0.00175** |

**≈48% cheaper on identical traffic**, by driving the cache anchor from 11 resets
to 0. The per-buster breakdown (tool-order churn alone, git/env alone, both) is in
[`benchmarks/RESULTS.md`](benchmarks/RESULTS.md).

```bash
python3 benchmarks/bench.py --turns 12            # reproduce (no API key)
python3 benchmarks/bench.py --real                # + live DeepSeek probe (needs key)
```

**Live-validated** against the real `api.deepseek.com/anthropic` endpoint: a
repeated prefix returns `hit=1536 miss=77` — a **95% cache hit** on the second
identical request, confirming DeepSeek's automatic cache serves Permafrost's
canonical request shape. (See [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md).)

**Validated with real Claude Code** (not synthetic requests): headless `claude -p`
on a 4-turn fix-the-bug task, isolated, routed through Permafrost → DeepSeek —
**66% cache hit rate, 64% cost reduction**, on ~85 KB / ~21 K tokens of genuine CC
system prompt + tools. Reproduce: [`e2e/run_claude_code.sh`](e2e/run_claude_code.sh)
(needs a funded `DEEPSEEK_API_KEY`). What this run taught us — including that
DeepSeek re-renders before caching, and CC's per-request billing nonce — is in
[`docs/e2e-findings.md`](docs/e2e-findings.md).

**Validated through the proxy, too:** four real requests sent through Permafrost
with *shuffled tool order and changing `git status` each time* — exactly the
traffic that misses in `off` mode — were aligned to one frozen anchor
(`prefix_resets=0`). Once the prefix was warm, DeepSeek served **~97%** of it
from cache (a single warm request: `hit=2048`, `miss=55` → **97%**). The proxy
held the anchor stable despite the naive-client churn — including over the
`/v1/messages?beta=true` query-string path.

> "Anchor resets" = how many times the `tools+system` prefix changed bytes across
> the run. Each reset forces DeepSeek to re-read the whole prefix at full price.
> Permafrost's job is to keep it at 0.

## Quick start

```bash
# 1 — install (clone anywhere; the proxy is stdlib-only Python, no pip needed)
git clone https://github.com/jianzhichun/permafrost && cd permafrost

# 2 — launch Claude Code through Permafrost, pointed at DeepSeek
export ANTHROPIC_API_KEY=sk-your-deepseek-key
./cli/permafrost wrap            # starts the proxy, sets the env, execs `claude`
```

`permafrost wrap` sets `ANTHROPIC_BASE_URL` + `ENABLE_TOOL_SEARCH=true` **for the
child `claude` process only** — it never touches your shell or
`~/.claude/settings.json`.

**Prefer it persistent?** Copy the `env` block from
[`settings.example.json`](settings.example.json) into `~/.claude/settings.json`,
run `permafrost up`, then start `claude` normally. (Claude Code reads its env once
at launch, so these must be set *before* `claude` starts.)

### Install as a Claude Code plugin

This repo is its own plugin marketplace — two commands inside Claude Code:

```
/plugin marketplace add jianzhichun/permafrost
/plugin install permafrost@permafrost
```

(or from a shell: `claude plugin marketplace add jianzhichun/permafrost &&
claude plugin install permafrost@permafrost`)

That gives you the `/permafrost:*` commands, the SessionStart auto-start hook,
and the statusline script. The proxy itself is part of the plugin — `permafrost
wrap` / the settings block above point Claude Code at it. Update later with
`/plugin marketplace update permafrost`.

## What it does, exactly

On every `/v1/messages` request, the alignment pipeline
([`proxy/permafrost_align.py`](proxy/permafrost_align.py)):

1. **strips `cache_control`** — DeepSeek ignores the markers; their drifting
   positions are pure prefix noise.
2. **sorts tools** deterministically — so late-binding MCP servers can't reshuffle
   position 0 of the prefix.
3. **freezes the env block + emits deltas** (aggressive mode) — pins the
   first-seen environment/context block (cwd, platform, today's date,
   `git status`) into the cached anchor, then on later turns sends only the
   *lines that changed* on the tail. An unchanged env costs zero tokens per turn;
   a changed one costs only its delta — instead of re-sending the whole block
   every turn. (Set `PERMAFROST_FREEZE_ENV=0` to fall back to relocating the
   whole block off the prefix instead.)
4. **serializes canonically** — compact, UTF-8-faithful, stable bytes.

It then reads DeepSeek's `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
straight off the response and tracks your live hit rate and dollar savings.

### Cold-anchor coalescing (for parallel subagents)

DeepSeek writes its cache *asynchronously*, so when Claude Code fans out `Task`
subagents — N requests sharing one brand-new prefix, fired at once — none can
read what the others are still writing, and **all N pay the cold-miss price**.
Permafrost coalesces them: the first request on an unseen anchor goes through as
the "leader" and warms the cache; same-anchor requests are held until the
leader's response starts streaming, then released to read the warm prefix.
Single requests are never delayed (a lone request *is* the leader); only
concurrent same-anchor bursts wait, with a timeout guard against a stuck leader.
`/permafrost:doctor` reports how many followers it held. Toggle with
`PERMAFROST_COALESCE=0`.

Release timing is a measured trade-off: DeepSeek's cache write is asynchronous
(our probes: ~4s after first byte still misses, ~6s hits), so followers wait a
`PERMAFROST_COALESCE_SETTLE_MS` (default 2500) after release — the default
itself is live-validated: a 3-request cold burst under default settings had
**both followers hit in full** (settle 0 scored 0% on the same shape). For maximum hit
odds set `PERMAFROST_COALESCE_RELEASE=completion` — followers go only after the
leader's response fully streams, which is when the request-boundary cache unit
persists.

**Live-validated:** 4 concurrent cold-anchor requests to real DeepSeek went from
**0% hit (off)** — all four miss, 16,356 tokens at full price — to **73% hit
(on)**, where one leader warmed the cache and three followers read it. The cost
is latency: followers wait for the leader's first byte (plus an optional
`PERMAFROST_COALESCE_SETTLE_MS` for DeepSeek's async write to land).

### Idle keepalive (opt-in)

DeepSeek evicts cache entries that go unused, so a long think-time gap can leave
your next turn paying full miss price on the whole conversation prefix. With
`PERMAFROST_KEEPALIVE_S=<seconds>` set, the proxy replays the last request —
**unchanged, body and headers** — after that much idle time, re-reading the
whole prefix at hit price (~2% of a miss). Off by default because it fires real
billable requests autonomously. `permafrost warm` triggers the same replay
manually. Live-validated at **99.9% hit** on a real CC request replay; the
"unchanged, headers too" part is load-bearing — DeepSeek's cache identity
includes the client's header fingerprint, and replays that differ in params or
headers measurably miss (see [`docs/e2e-findings.md`](docs/e2e-findings.md)).

### Per-session & per-lineage stats, with anchor diffs

`/permafrost/stats` buckets usage by Claude Code session id, and tracks anchor
churn per *lineage* (stable system+tools ancestry) — so CC's tool-less preflight
calls don't pollute the churn signal, and multiple sessions through one proxy
stay readable.

When an anchor *does* change within a lineage, the doctor shows **exactly where
the bytes diverged** (`diverged_at_byte` plus `was`/`now` excerpts). That makes
Permafrost self-debugging against future Claude Code releases: a new volatile
pattern shows up as a readable diff in `/permafrost:doctor`, not as a
mysteriously sinking hit rate. (This is precisely how we caught CC's `cch=`
billing nonce.)

Full mechanism: [`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md).
The complete catalogue of Claude Code cache-busters it defends against —
including the "random header" worry: [`docs/cache-busters.md`](docs/cache-busters.md).

## Modes

| `PERMAFROST_MODE` | Does | Use when |
|---|---|---|
| `aggressive` (default) | strip + sort + **relocate** + canonical | real coding sessions (git status changes every turn) |
| `safe` | strip + sort + canonical (no content moved) | you've already moved volatile content out of `system` |
| `off` | nothing (still records stats) | measuring your un-aligned baseline |

## Plugin surface

- **`/permafrost:status`** — live hit rate, token split, dollars saved.
- **`/permafrost:doctor`** — what's busting the cache right now + how to fix it.
- **`/permafrost:benchmark`** — run the cache benchmark from inside a session.
- **`/permafrost:wrap`** — how to (re)launch CC through the proxy.
- **Statusline** — `❄ 88% cache hit · $0.12 saved` ([`scripts/statusline.sh`](scripts/statusline.sh)).
- **SessionStart hook** — auto-starts the proxy *only* when your session is
  actually routed through it ([`hooks/session_start.sh`](hooks/session_start.sh)).

## The env hardening (set before launching `claude`)

| Variable | Why |
|---|---|
| `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` | route CC through Permafrost |
| `ENABLE_TOOL_SEARCH=true` | **critical** — without it, a custom base URL makes CC stop deferring tools and re-inline the full tool set every turn (a giant buster) |
| `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` | pin to names that map to one DeepSeek SKU (`claude-opus*`→v4-pro, `claude-sonnet*/haiku*`→v4-flash) |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | fewer off-prefix one-off requests |

## Compared to

CCR routes models; LiteLLM warms Anthropic breakpoints; Headroom compresses and
only *detects* prefix volatility; Reasonix is a separate agent. **None is a CC
plugin that rewrites the request bytes to keep DeepSeek's prefix cache hitting.**
Full survey: [`docs/landscape.md`](docs/landscape.md).

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `PERMAFROST_PORT` | `8787` | proxy listen port |
| `PERMAFROST_UPSTREAM` | `https://api.deepseek.com/anthropic` | where to forward (any Anthropic-compatible endpoint works) |
| `PERMAFROST_MODE` | `aggressive` | `off` / `safe` / `aggressive` |
| `PERMAFROST_FREEZE_ENV` | `1` | freeze the env block into the anchor + emit only changed lines (`0` relocates the whole block) |
| `PERMAFROST_NORMALIZE_BETA` | `1` | sort + dedup the `anthropic-beta` header (never adds/drops a flag) |
| `PERMAFROST_COALESCE` | `1` | hold parallel cold-anchor requests until the first warms the cache (`0` disables) |
| `PERMAFROST_COALESCE_TIMEOUT_S` | `30` | follower deadlock guard |
| `PERMAFROST_COALESCE_SETTLE_MS` | `2500` | extra wait after release, to let DeepSeek's async cache write land |
| `PERMAFROST_COALESCE_RELEASE` | `first_byte` | or `completion`: release followers only after the leader fully streams (max hit odds, more latency) |
| `PERMAFROST_KEEPALIVE_S` | `0` (off) | opt-in: replay the last request unchanged after this much idle, keeping the prefix warm at hit price |
| `PERMAFROST_KEEPALIVE_IDLE_STOP_S` | `7200` | stop keepalives after this much idle (abandoned-session guard) |
| `PERMAFROST_PRICES` | V4 Flash | `"hit,miss,output"` USD/1M for the cost readout |

> The benchmark emulator measures byte-prefix overlap; DeepSeek matches a
> *rendered-token* prefix. The two load-bearing transforms (tool sort, env
> relocation) change the rendered prompt and are validated against the live API
> above. `cache_control` stripping and canonical serialization are byte-level
> insurance — harmless to DeepSeek, useful for byte-sensitive gateways.

## Tests

```bash
python3 tests/test_alignment.py     # pure, offline — prefix-stability properties
bash    tests/proxy_smoke.sh        # isolated end-to-end (local mock upstream, throwaway ports)
```

## Credits

Permafrost generalizes techniques proven in two excellent projects:
**[DeepSeek-Reasonix](https://github.com/esengine/DeepSeek-Reasonix)** (the
prefix-cache discipline and `prompt_cache_hit_tokens` accounting) and
**[Headroom](https://github.com/chopratejas/headroom)** (the proxy interception
pattern, canonical forwarding, and the `ENABLE_TOOL_SEARCH` finding). Where
Headroom's `CacheAligner` *detects* volatile content, Permafrost *relocates* it.

## License

MIT — see [LICENSE](LICENSE).
