---
description: Run the Permafrost cache benchmark (offline emulator, no API key)
argument-hint: "[turns]"
allowed-tools: Bash(python3:*)
---

Run the offline DeepSeek cache benchmark and report the result.

!`python3 "${CLAUDE_PLUGIN_ROOT}/benchmarks/bench.py" --turns ${ARGUMENTS:-12}`

Present the table to the user and call out the headline: in the realistic "both" scenario, how the hit rate and cost change between `off` and `aggressive` mode, and how many anchor resets each mode incurs. Note that this uses a faithful emulator of DeepSeek's prefix cache — to measure the live API instead, add `--real` with a `DEEPSEEK_API_KEY` set.
