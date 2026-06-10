<div align="center"><pre>
   ___  ___ ___ __  __  _   ___ ___  ___  ___ _____
  | _ \| __| _ \  \/  | /_\ | __| _ \/ _ \/ __|_   _|
  |  _/| _||   / |\/| |/ _ \| _||   / (_) \__ \ | |
  |_|  |___|_|_\_|  |_/_/ \_\_| |_|_\\___/|___/ |_|
        freeze the prefix · melt the bill
</pre></div>

<p align="center"><strong>A Claude Code plugin that keeps DeepSeek's prompt cache hitting when Claude Code would otherwise bust it.</strong></p>

<p align="center">cache-stable passthrough proxy · deterministic tool order · env freeze+delta · cold-anchor coalescing · live hit-rate statusline · zero deps</p>

---

Pointing Claude Code at DeepSeek's Anthropic-compatible endpoint is cheap, and a
**vanilla session already hits DeepSeek's cache ~90% on its own** — CC's system
prompt is shared across users, so it stays warm. The cache breaks when your
**tool list isn't stable**: MCP servers connect and reshuffle it, and because
tools render *first* in the cached prefix, any reorder busts everything from
byte 0. Permafrost sits between CC and DeepSeek and rewrites the cache-relevant
bytes so the `tools + system` anchor stays byte-identical turn after turn — then
streams the reply straight back.

```
  Claude Code ──Anthropic /v1/messages──▶ Permafrost ──▶ DeepSeek /anthropic
   (unchanged)        127.0.0.1:8787     freeze prefix       (both speak Anthropic,
                                          + record hits        no translation)
```

## Do you actually need it?

We ran **bare DeepSeek vs Permafrost head-to-head on the live API** and read
DeepSeek's real cache tokens. The honest result is two-sided:

| Your Claude Code session | bare DeepSeek | + Permafrost |
|---|---:|---:|
| Vanilla — stable tools, no MCP (10-turn task) | 89.6% hit | 89.6% — **no difference** |
| Tool list churns — MCP servers reshuffle it | **33% hit** | **71% hit** |

**If you run plain single-agent CC, you don't need this** — DeepSeek already does
the job. Permafrost earns its keep on the second row: tool churn busts the prefix
at byte 0 and bare collapses to 33%; the deterministic sort holds it at 71%
(reproduce: [`e2e/tool_order_ab.py`](e2e/tool_order_ab.py)). So it's for users with
**MCP servers**, heavily **customized** setups whose anchor isn't the shared-warm
one, or **parallel subagent fan-outs** on cold anchors.

**On cost:** DeepSeek is ~11× cheaper than Claude on pricing alone (cache-hit
input $0.0028 vs $0.30 /1M) — that's DeepSeek, not us, the moment you switch
endpoints. Permafrost's *additional* contribution is keeping the cache hitting
when it would otherwise bust (the 33% → 71% roughly halves the bill on a
tool-churning workload; on a vanilla session it adds ~0%). Price-for-identical-
traffic, not a model-quality claim — `deepseek-v4-flash` is not Sonnet 4.6.

## Quick start

```bash
git clone https://github.com/jianzhichun/permafrost && cd permafrost
export ANTHROPIC_API_KEY=sk-your-deepseek-key
./cli/permafrost wrap     # starts the proxy, sets env for the child only, execs claude
```

`wrap` sets `ANTHROPIC_BASE_URL` + `ENABLE_TOOL_SEARCH=true` for the child `claude`
process only — it never touches your shell or `~/.claude/settings.json`.

**As a plugin** (gives you `/permafrost:*` commands, statusline, auto-start hook):

```
/plugin marketplace add jianzhichun/permafrost
/plugin install permafrost@permafrost
```

**Persistent:** copy the `env` block from [`settings.example.json`](settings.example.json)
into `~/.claude/settings.json`, run `permafrost up`, start `claude` normally. (CC
reads its env once at launch — set these *before* it starts.)

## What it does

On every `/v1/messages` request, the pipeline
([`proxy/permafrost_align.py`](proxy/permafrost_align.py)) keeps the `tools+system`
anchor byte-stable, then reads DeepSeek's real `prompt_cache_hit_tokens`:

1. **sorts tools** deterministically — late-binding MCP servers can't reshuffle
   position 0 of the prefix. *(This is the one that matters most — see above.)*
2. **freezes the env block + emits deltas** — pins the first-seen env/context
   block (cwd, date, `git status`) into the anchor; later turns send only changed
   lines on the tail. `PERMAFROST_FREEZE_ENV=0` relocates the whole block instead.
3. **strips `cache_control`** and **serializes canonically** — DeepSeek ignores
   the markers; canonical bytes remove serialization drift.

Plus three runtime features (depth in [`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md)):

- **Cold-anchor coalescing** — parallel `Task` subagents sharing a cold prefix
  would all pay the miss price (DeepSeek's write is async); one leader warms the
  cache, followers wait for its first byte. Live: 0% → 73%. `PERMAFROST_COALESCE=0` off.
- **Idle keepalive** (opt-in) — replays each conversation's last request,
  unchanged in body *and headers*, after `PERMAFROST_KEEPALIVE_S` idle, so a long
  think-gap doesn't lose the prefix to eviction. Live: 99.9% on replay.
- **Diagnostics** — `/permafrost:status` `:doctor` `:benchmark`, a savings
  statusline (`❄ 88% cache hit · $0.12 saved`), per-session + per-lineage stats,
  and byte-level anchor diffs (how we caught CC's per-request `cch` nonce).

The full catalogue of CC cache-busters it defends against:
[`docs/cache-busters.md`](docs/cache-busters.md).

## Validation

`e2e/run_full_suite.sh` drives headless `claude -p` through Permafrost against
**real DeepSeek** across six phases (alignment, coalescing, keepalive+resume,
warm, sessions, anchor-diff) — latest run **15/15 passed**. That proves the
mechanisms work end-to-end; the *value vs bare* is the head-to-head table above.
What these runs taught us about DeepSeek's cache (it re-renders before caching;
cache identity includes the client header fingerprint; it keys on tool order but
tolerates the `cch` nonce) is in [`docs/e2e-findings.md`](docs/e2e-findings.md).

<details><summary><b>Offline benchmark (no API key)</b></summary>

Emulator faithful to the live findings — caches the **rendered** request
(`tools → system → messages`), unit-quantized, with params + header identity.
12-turn CC-shaped conversation:

| Scenario | `off` | `aggressive` |
|---|---:|---:|
| Tool churn + live git/env | **0%** hit | **88%** hit |

`off` caches nothing (reshuffled tools bust byte 0); `aggressive` (sort) holds 88%.
`python3 benchmarks/bench.py --turns 12` to reproduce; `--real` adds a live probe.
Details + the raw-byte-vs-rendered cross-check: [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md).
</details>

## Reference

**Modes** (`PERMAFROST_MODE`): `aggressive` (default: sort + freeze + strip +
canonical) · `safe` (no content moved) · `off` (passthrough, still meters).

**Env hardening** — set *before* launching `claude`:

| Variable | Why |
|---|---|
| `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` | route CC through Permafrost |
| `ENABLE_TOOL_SEARCH=true` | **critical** — without it a custom base URL makes CC stop deferring tools and re-inline the whole set every turn |
| `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` | pin to one DeepSeek SKU (`claude-opus*`→v4-pro, `*sonnet/haiku*`→v4-flash) |

**Config** (env, all optional):

| Env | Default | Meaning |
|---|---|---|
| `PERMAFROST_PORT` | `8787` | listen port |
| `PERMAFROST_UPSTREAM` | `…/anthropic` | upstream (any Anthropic-compatible endpoint) |
| `PERMAFROST_FREEZE_ENV` | `1` | freeze env + delta (`0` = relocate whole block) |
| `PERMAFROST_COALESCE` / `_SETTLE_MS` | `1` / `2500` | coalescing + async-write settle |
| `PERMAFROST_KEEPALIVE_S` | `0` (off) | idle-replay interval |
| `PERMAFROST_PRICES` | V4 Flash | `"hit,miss,output"` USD/1M for the cost readout |

The proxy is zero-dependency stdlib Python, pools upstream connections (no
per-request TLS), and refuses non-loopback clients on its `/permafrost/*` endpoints.

## Tests

```bash
# offline (no key, in CI): alignment, proxy units, CC-fixture, emulator + 3 smokes
python3 tests/test_alignment.py && python3 tests/test_proxy_units.py
python3 tests/test_cc_fixture.py && python3 tests/test_emulator.py
bash tests/proxy_smoke.sh && bash tests/coalesce_smoke.sh && bash tests/keepalive_smoke.sh

# live (funded DEEPSEEK_API_KEY, a few cents):
bash e2e/run_claude_code.sh    # one real CC task
bash e2e/run_full_suite.sh     # every feature, asserted
python3 e2e/tool_order_ab.py   # the decisive bare-vs-Permafrost test
```

## Credits

Generalizes techniques from **[DeepSeek-Reasonix](https://github.com/esengine/DeepSeek-Reasonix)**
(prefix-cache discipline, `prompt_cache_hit_tokens` accounting) and
**[Headroom](https://github.com/chopratejas/headroom)** (proxy interception,
canonical forwarding, the `ENABLE_TOOL_SEARCH` finding). Where Headroom's
CacheAligner *detects* volatile content, Permafrost *rewrites* it. Compared to
CCR / LiteLLM / Headroom / Reasonix: [`docs/landscape.md`](docs/landscape.md).

MIT — see [LICENSE](LICENSE).
