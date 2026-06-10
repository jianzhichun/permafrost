# Permafrost benchmark results

Emulated DeepSeek cache, 12-turn Claude-Code-shaped conversation. Prices are DeepSeek V4 Flash (USD/1M): hit $0.0028, miss $0.14, output $0.28.

Reproduce: `python3 benchmarks/bench.py --turns 12 --write benchmarks/RESULTS.md`

## Methodology — what the emulator models (and why it changed)

Rebuilt to match the live findings in [`docs/e2e-findings.md`](../docs/e2e-findings.md):
DeepSeek caches the **rendered** request (`tools → system → messages`, Anthropic's
documented order), **not** the raw JSON body — so Claude Code serializing `messages`
first is irrelevant, and the cacheable anchor is `tools`+`system`. A hit must fully
match a ~64-token prefix unit from token 0; cache identity also includes request
params and the client header fingerprint; writes settle asynchronously (~6s).

| Scenario | Mode | Hit rate | Anchor resets | Cost (USD) | vs all-miss |
|---|---|---:|---:|---:|---:|
| A: MCP tool-order churn | off | 0.0% | 11 | $0.00828 | -0.0% |
| A: MCP tool-order churn | safe | 90.5% | 0 | $0.00158 | -80.6% |
| A: MCP tool-order churn | aggressive | 88.5% | 0 | $0.00173 | -79.0% |
| B: live git/env block | off | 82.2% | 11 | $0.00221 | -73.4% |
| B: live git/env block | safe | 83.7% | 11 | $0.00208 | -74.6% |
| B: live git/env block | aggressive | 88.4% | 0 | $0.00174 | -78.8% |
| C: both (realistic CC) | off | 0.0% | 11 | $0.00829 | -0.0% |
| C: both (realistic CC) | safe | 83.7% | 11 | $0.00208 | -74.6% |
| C: both (realistic CC) | aggressive | 88.4% | 0 | $0.00174 | -78.8% |

## Headline (scenario C, the realistic Claude Code case)

- **off**: 0.0% hit, 11 anchor resets, $0.00829 — tools render first, so churned tool order busts the prefix at byte 0.
- **aggressive**: 88.4% hit, 0 anchor resets, $0.00174.
- **cost reduction**: 79.0% cheaper than off.

## Why the model was rebuilt (raw-byte vs faithful)

```
faithful (renders tools+system first): 88% hit  vs  raw-body model (CC's messages-first order): 5% hit — the raw model under-reports because it keys on bytes DeepSeek doesn't cache.
```

## Cache identity includes params (live finding, reproduced offline)

```
same params, settled: 94% hit  ·  max_tokens changed: 0% hit
```
