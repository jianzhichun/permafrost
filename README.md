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

**Install as a plugin** for the `/permafrost:*` commands and the statusline — add
this repo to a marketplace or drop it under a skills dir; see
[Claude Code plugin docs](https://code.claude.com/docs/en/plugins).

## What it does, exactly

On every `/v1/messages` request, the alignment pipeline
([`proxy/permafrost_align.py`](proxy/permafrost_align.py)):

1. **strips `cache_control`** — DeepSeek ignores the markers; their drifting
   positions are pure prefix noise.
2. **sorts tools** deterministically — so late-binding MCP servers can't reshuffle
   position 0 of the prefix.
3. **relocates volatile content** (aggressive mode) — lifts the env/context block
   (today's date, `git status`, UUIDs, hashes) out of the cached anchor and
   re-attaches it to the latest turn. Same content, later position, no longer
   resetting the cache every time you touch a file.
4. **serializes canonically** — compact, UTF-8-faithful, stable bytes.

It then reads DeepSeek's `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
straight off the response and tracks your live hit rate and dollar savings.

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
| `PERMAFROST_SORT_KEYS` | `0` | also sort JSON object keys (extra determinism) |
| `PERMAFROST_STICKY_BETA` | `1` | pin the first-seen `anthropic-beta` per anchor |
| `PERMAFROST_PRICES` | V4 Flash | `"hit,miss,output"` USD/1M for the cost readout |

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
