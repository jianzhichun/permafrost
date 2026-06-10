# Landscape: who else works on LLM cache / prefix optimization

The user asked how many open-source projects target the same goal. Short answer:
**several touch prompt caching or Claude-Code-to-DeepSeek routing, but none is a
Claude Code plugin whose specific job is to rewrite CC's request bytes to
maximize DeepSeek's automatic prefix cache.** That's the gap Permafrost fills.

Surveyed June 2026. Grouped by what they actually do.

## A. Prefix-cache stabilization (closest neighbors)

| Project | What it does | vs. Permafrost |
|---|---|---|
| **[Headroom](https://github.com/chopratejas/headroom)** | Context-compression proxy + `headroom wrap claude`. Its `CacheAligner` **detects** volatile content in the system prompt and warns; deterministic tool sort; byte-faithful forwarding; session-sticky `anthropic-beta`. | Headroom's compression is the headline; its cache aligner is **detector-only**. Permafrost is cache-first and **relocates** volatile content instead of only warning, specialized for DeepSeek's prefix cache, shipped as a CC plugin. |
| **[DeepSeek-Reasonix](https://github.com/esengine/DeepSeek-Reasonix)** | A whole DeepSeek-native coding *agent* (Go), engineered around the prefix cache: frozen system prefix, sorted tools, turn-tail injection, compaction as the one reset point. | Reasonix is a standalone agent, not a way to make **Claude Code** cache-friendly. Permafrost ports its techniques into a proxy that sits in front of CC. |

## B. Claude Code ↔ DeepSeek routing (no cache work)

| Project | What it does | vs. Permafrost |
|---|---|---|
| **[claude-code-router](https://github.com/musistudio/claude-code-router)** (CCR) | Local proxy that routes CC requests to different backend models by task. | Routing, not cache stability. Could sit *alongside* Permafrost. |
| **[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)** | Wraps CC/Gemini/Codex CLIs as OpenAI/Anthropic-compatible API services. | Transport/compat layer; no prefix-cache alignment. |
| **claudecode-deepseek-stack** (MG-Cafe) | Two env vars to point CC at DeepSeek. | Exactly the setup Permafrost starts from — then fixes the cache busting that setup leaves on the table. |

## C. Provider-native / response caching

| Project | What it does | vs. Permafrost |
|---|---|---|
| **LiteLLM** (Claude Code prompt-cache routing) | Provider-sticky routing + a warm-up call that pre-caches the system prompt; manages Anthropic `cache_control`. | Targets Anthropic's explicit-breakpoint cache, not DeepSeek's automatic prefix cache; doesn't rewrite CC's volatile prefix. |
| **NadirClaw** | OpenAI-compatible router; LRU **response** cache (identical prompt → skip API). | Response-level exact-match cache, a different mechanism from input prefix caching. |
| **OpenRouter prompt caching** | Provider-sticky routing to keep implicit caches warm; advises "keep the start of messages consistent." | Hosted routing guidance; Permafrost *enforces* that consistency on the wire. |

## D. Claude-Code-specific cache fixes (adjacent, Anthropic-only)

| Project | What it does | vs. Permafrost |
|---|---|---|
| **[claude-code-cache-fix](https://github.com/cnighswonger/claude-code-cache-fix)** | Fixes a prompt-cache regression on *resumed* CC sessions (Anthropic billing). | Anthropic's own cache, resume-specific; not DeepSeek, not prefix-byte alignment. |
| **[flightlesstux/prompt-caching](https://github.com/flightlesstux/prompt-caching)** | Zero-config automatic prompt caching for CC (Anthropic). | Manages Anthropic `cache_control` placement; orthogonal to DeepSeek's marker-free auto cache. |

## E. Output/context shrinkers (reduce tokens, not stabilize prefix)

[RTK](https://github.com/rtk-ai/rtk), [lean-ctx](https://github.com/yvgude/lean-ctx),
hosted compressors — they cut how many tokens you send. Complementary to
Permafrost: fewer tokens *and* a frozen prefix compound.

## The one-line differentiation

> Permafrost is the only one that is **(a) a Claude Code plugin, (b) aimed at
> DeepSeek's automatic prefix cache specifically, and (c) actively rewriting the
> request bytes** (sort, strip, relocate, canonicalize) to keep the `tools+system`
> anchor frozen — rather than routing models, compressing tokens, managing
> Anthropic breakpoints, or merely *detecting* volatility.
