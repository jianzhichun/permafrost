# Permafrost benchmark results

Emulated DeepSeek prefix cache, 12-turn Claude-Code-shaped conversation. Prices are DeepSeek V4 Flash (USD/1M): hit $0.0028, miss $0.14, output $0.28.

Reproduce: `python3 benchmarks/bench.py --turns 12 --write benchmarks/RESULTS.md`

| Scenario | Mode | Hit rate | Anchor resets | Cost (USD) | vs all-miss |
|---|---|---:|---:|---:|---:|
| A: MCP tool-order churn | off | 66.5% | 11 | $0.00337 | -59.4% |
| A: MCP tool-order churn | safe | 90.5% | 0 | $0.00158 | -80.7% |
| A: MCP tool-order churn | aggressive | 88.5% | 0 | $0.00173 | -79.0% |
| B: live git/env block | off | 66.4% | 11 | $0.00339 | -59.3% |
| B: live git/env block | safe | 67.6% | 11 | $0.00325 | -60.2% |
| B: live git/env block | aggressive | 88.4% | 0 | $0.00175 | -78.9% |
| C: both (realistic CC) | off | 66.4% | 11 | $0.00339 | -59.3% |
| C: both (realistic CC) | safe | 67.6% | 11 | $0.00325 | -60.2% |
| C: both (realistic CC) | aggressive | 88.4% | 0 | $0.00175 | -78.9% |

## Headline (scenario C, the realistic Claude Code case)

- **off**: 66.4% hit rate, 11 anchor resets, $0.00339
- **aggressive**: 88.4% hit rate, 0 anchor resets, $0.00175
- **cost reduction**: 48.4% cheaper than off on identical traffic.

Anchor resets = how many times the `tools`+`system` prefix changed bytes across the run. Every reset forces DeepSeek to re-read the whole prefix at full (miss) price. Permafrost's job is to drive this to 0.
