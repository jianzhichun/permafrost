# What makes Claude Code miss DeepSeek's cache

> Research notes behind Permafrost. The question: *what does Claude Code do,
> request to request, that changes the leading bytes of the prompt and therefore
> resets DeepSeek's automatic prefix cache?* — and which of those can a
> passthrough proxy + a little env hygiene neutralize.

## The rule we're fighting

DeepSeek's context cache is automatic and **prefix-anchored at byte 0**. From
their docs:

> "A subsequent request can only hit the cache if it **fully matches** a cache
> prefix unit." Partial matches in the middle do not count.

The cache is read-only input: the first byte that differs from a previously-seen
request ends the hit; everything after it is billed at the full (miss) price —
about **50× more** than a hit on V4 Flash. Render order is `tools` → `system` →
`messages`, so the **cache anchor** is `tools` + `system`. Keep those bytes
frozen across turns and the whole conversation up to the newest turn is served
from cache. Let one byte near the front wobble and you pay full price for
everything.

So the enemy is *prefix instability*. Here is where Claude Code introduces it.

## The busters

| # | What CC does | Where it lands | Why it resets the cache | Permafrost's fix |
|---|---|---|---|---|
| 1 | **MCP tools finish connecting mid-session**, changing the tool set or its order | `tools` (position 0) | Any add/remove/reorder of a tool changes byte 0 of the anchor → total miss | `sort_tools`: emit tools in a deterministic `(name, canonical-json)` order, independent of arrival timing |
| 2 | **`ENABLE_TOOL_SEARCH` defaults off under a custom `ANTHROPIC_BASE_URL`** | `tools` + `system` | CC stops *deferring* tools and re-inlines the **entire** tool set every turn — tens of KB of churn at the front of the prefix | Forward `tool_reference` blocks faithfully; ship `ENABLE_TOOL_SEARCH=true` in the env block / `wrap` |
| 3 | **The environment block** (cwd, platform, **today's date**, a **`git status` snapshot**) | `system` | `git status` changes the moment you edit a file; the date changes daily — each change resets everything after it | `relocate_volatile`: lift the env block out of the anchor and re-attach it to the latest turn (aggressive mode) |
| 4 | **`cache_control` breakpoints slide to the newest turn** each request | `system` + `messages` | DeepSeek ignores the markers, but their *bytes* move, perturbing serialization around them | `strip_cache_control`: remove every `cache_control` key (no-op for DeepSeek's auto cache) |
| 5 | **`anthropic-beta` header flips** as feature flags toggle (interleaved thinking, fine-grained streaming, tool search…) | request header | Header churn can change how the upstream frames/keys the request mid-session | `sticky_beta`: pin the first-seen beta value per cache anchor for the session |
| 6 | **Model-name churn** — the main model vs. `ANTHROPIC_SMALL_FAST_MODEL` for cheap calls | `model` field | Caches are model-scoped; a different model is a different cache namespace, and DeepSeek maps `claude-opus*`→`deepseek-v4-pro`, `claude-sonnet*/haiku*`→`deepseek-v4-flash` | Pin `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` to names that map to **one** DeepSeek SKU |
| 7 | **Non-essential background traffic** (telemetry, title-gen, summaries) on different prefixes | separate requests | Each one-off prefix is a pure cache write, never read | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` |
| 8 | **Re-sending `reasoning_content`** in history (only on the OpenAI-style endpoint) | `messages` | DeepSeek bills re-sent reasoning as input (~500 tok/turn in Reasonix's measurements) | N/A for the Anthropic passthrough (thinking blocks are append-only in the tail); documented for the OpenAI path |
| 9 | **Non-deterministic JSON** — object-key reordering, `\uXXXX` escaping, whitespace | whole body | Byte-different serialization of identical content → prefix differs | `canonical_dumps`: compact separators, `ensure_ascii=False`, insertion-order (optional key sort) |

## The "random header" worry, specifically

Two distinct things get called "the header":

1. **The HTTP `anthropic-beta` header.** It carries feature flags and *can*
   change mid-session. For DeepSeek's **body**-anchored cache this header is not
   part of the cached prefix, so it rarely matters — but it does for real
   Anthropic caching and for any gateway that keys on it. Permafrost pins it
   (`sticky_beta`, on by default) so it can't wobble. It is **not** something a
   plugin can stop CC from *generating*; the only safe interception point is a
   proxy, which is why Permafrost is one.

2. **The request "header" in the colloquial sense — the front of the prompt
   body** (`tools` + `system`). This is the real battleground, and busters 1–4
   and 9 above are exactly "CC randomly modifying the head of the request."
   Permafrost rewrites this head into a stable, canonical form on every request.

The crucial constraint: **CC reads its env once, at process start, and never
re-checks it.** So `ANTHROPIC_BASE_URL`, `ENABLE_TOOL_SEARCH`, and the model
pins must be set *before* `claude` launches — `permafrost wrap` and the
`settings.example.json` block both do this. Changing them in a running session
does nothing.

## What a plugin alone cannot do

A Claude Code plugin runs hooks, commands and skills — it never sees or rewrites
the bytes CC puts on the wire. So the request-body fixes (1, 3, 4, 9) are only
possible from a **proxy** sitting at `ANTHROPIC_BASE_URL`. That is the whole
reason Permafrost ships a proxy and not just hooks. The plugin layer handles
what it *can* own: the env hardening (2, 6, 7), live diagnostics (`/doctor`),
and the savings statusline.
