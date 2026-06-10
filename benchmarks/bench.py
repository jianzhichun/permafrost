#!/usr/bin/env python3
"""End-to-end cache benchmark for Permafrost.

We can't make claims about cache hits without measuring them, so this harness
does two things:

  1. Replays a realistic, multi-turn, Claude-Code-shaped conversation through a
     faithful *emulator* of DeepSeek's prefix cache — for each alignment mode
     (off / safe / aggressive) and each class of cache-buster — and reports the
     hit rate, tokens billed, and dollar cost. No API key required; fully
     deterministic.

  2. With `--real` and a DEEPSEEK_API_KEY set, fires two identical requests at
     the live DeepSeek Anthropic endpoint and prints the real
     prompt_cache_hit_tokens it returns — the same probe Reasonix uses to prove
     the cache actually serves this request shape.

The emulator implements DeepSeek's documented rule verbatim: "A subsequent
request can only hit the cache if it fully matches a cache prefix unit." The
match is anchored at byte 0; the first differing byte ends the hit. Units are
quantized to fixed-size blocks (DeepSeek's live granularity measures ~64
tokens; Reasonix's realcache probe confirms this).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import permafrost_align as pa  # noqa: E402

BLOCK_TOKENS = 64
BYTES_PER_TOKEN = 4  # rough, but applied consistently to every arm


def est_tokens(b: bytes) -> int:
    return len(b) // BYTES_PER_TOKEN


def byte_lcp(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class DeepSeekCacheEmulator:
    """Models DeepSeek's on-disk prefix cache (prefix-from-0, block-quantized)."""

    def __init__(self) -> None:
        self.seen: list[bytes] = []

    def send(self, serialized: bytes) -> dict[str, int]:
        input_tokens = est_tokens(serialized)
        best_lcp = 0
        for prior in self.seen:
            best_lcp = max(best_lcp, byte_lcp(serialized, prior))
        # A hit only counts in whole prefix-unit blocks.
        hit_tokens = (est_tokens(serialized[:best_lcp]) // BLOCK_TOKENS) * BLOCK_TOKENS
        hit_tokens = min(hit_tokens, input_tokens)
        miss_tokens = input_tokens - hit_tokens
        self.seen.append(serialized)
        return {"hit": hit_tokens, "miss": miss_tokens}


# --- synthetic Claude Code traffic -----------------------------------------

# A stable, sizeable system instruction (stands in for CC's agent prompt).
_INSTRUCTION = (
    "You are Claude Code, an interactive CLI coding agent. Follow the user's "
    "instructions precisely, use the provided tools to inspect and edit the "
    "repository, prefer minimal diffs, and explain your reasoning concisely. "
) * 60  # ~ a few thousand tokens, byte-stable across turns

_TOOL_NAMES = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch",
    "Task", "TodoWrite", "NotebookEdit", "mcp__github__list_prs",
    "mcp__github__create_issue", "mcp__postgres__query",
]


def _tool(name: str) -> dict:
    return {
        "name": name,
        "description": f"The {name} tool. Use it to {name.lower()} things in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"arg": {"type": "string", "description": "primary argument"}},
            "required": ["arg"],
        },
    }


def _env_block(turn: int, volatile: bool) -> dict:
    """Claude Code's environment/context block. Volatile when git status moves."""
    git = f"M src/module_{turn}.py\n A tests/test_{turn}.py" if volatile else "clean"
    date = "2026-06-10"
    return {
        "type": "text",
        "text": (
            "<env>\n"
            "Working directory: /home/dev/repo\n"
            "Is directory a git repo: yes\n"
            "Platform: linux\n"
            f"Today's date: {date}\n"
            "Current branch: main\n"
            f"gitStatus: {git}\n"
            "</env>"
        ),
        "cache_control": {"type": "ephemeral"},
    }


def build_request(turn: int, history: list[dict], *, shuffle_tools: bool,
                  volatile_env: bool) -> dict:
    """One Anthropic Messages request as Claude Code would emit it.

    Top-level key order (model, system, tools, messages) is fixed, matching how
    clients serialize — so `system` and `tools` form the cache anchor.
    """
    system = [{"type": "text", "text": _INSTRUCTION, "cache_control": {"type": "ephemeral"}}]
    # CC appends the env/context block to the system array.
    system.append(_env_block(turn, volatile_env))

    names = list(_TOOL_NAMES)
    if shuffle_tools:
        # Deterministic per-turn rotation, simulating MCP servers binding in a
        # different order each launch.
        rot = turn % len(names)
        names = names[rot:] + names[:rot]
    tools = [_tool(n) for n in names]

    # Append-only growth: each turn adds a user turn + the prior assistant turn.
    msgs = list(history)
    msgs.append({"role": "user", "content": [{"type": "text",
                "text": f"Turn {turn}: please continue the task and edit the next file."}]})
    # Mark the most recent block for caching (CC slides this each turn).
    msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}

    return {
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "system": system,
        "tools": tools,
        "messages": msgs,
    }


def run_scenario(name: str, *, shuffle_tools: bool, volatile_env: bool,
                 mode: str, turns: int) -> dict:
    emu = DeepSeekCacheEmulator()
    history: list[dict] = []
    tot = {"hit": 0, "miss": 0, "output": 0}
    anchors: set[str] = set()
    per_turn = []

    for turn in range(turns):
        req = build_request(turn, history, shuffle_tools=shuffle_tools,
                            volatile_env=volatile_env)
        aligned, report = pa.align_request(json.loads(json.dumps(req)), mode)
        anchors.add(report.anchor_fingerprint)
        serialized = pa.canonical_dumps(aligned)
        u = emu.send(serialized)
        out = 220  # assistant reply size per turn, constant across arms
        tot["hit"] += u["hit"]
        tot["miss"] += u["miss"]
        tot["output"] += out
        per_turn.append(pa.hit_rate(u["hit"], u["miss"]))

        # Grow history append-only with a user + assistant turn for next round.
        history = req["messages"]
        history.append({"role": "assistant", "content": [{"type": "text",
                        "text": f"Done with turn {turn}. Edited the file and ran tests."}]})

    cost = pa.cost_usd(tot["hit"], tot["miss"], tot["output"])
    baseline = pa.cost_usd(0, tot["hit"] + tot["miss"], tot["output"])
    return {
        "scenario": name,
        "mode": mode,
        "turns": turns,
        "hit": tot["hit"],
        "miss": tot["miss"],
        "output": tot["output"],
        "hit_rate": pa.hit_rate(tot["hit"], tot["miss"]),
        "anchor_changes": len(anchors) - 1,
        "cost_usd": cost,
        "cost_if_all_miss": baseline,
        "saved_pct": (1 - cost / baseline) * 100 if baseline else 0.0,
    }


SCENARIOS = [
    ("A: MCP tool-order churn", dict(shuffle_tools=True, volatile_env=False)),
    ("B: live git/env block", dict(shuffle_tools=False, volatile_env=True)),
    ("C: both (realistic CC)", dict(shuffle_tools=True, volatile_env=True)),
]
MODES = ["off", "safe", "aggressive"]


def run_all(turns: int) -> list[dict]:
    rows = []
    for name, cfg in SCENARIOS:
        for mode in MODES:
            rows.append(run_scenario(name, mode=mode, turns=turns, **cfg))
    return rows


def fmt_table(rows: list[dict]) -> str:
    out = []
    out.append("| Scenario | Mode | Hit rate | Anchor resets | Cost (USD) | vs all-miss |")
    out.append("|---|---|---:|---:|---:|---:|")
    for r in rows:
        out.append(
            f"| {r['scenario']} | {r['mode']} | {r['hit_rate']*100:.1f}% | "
            f"{r['anchor_changes']} | ${r['cost_usd']:.5f} | -{r['saved_pct']:.1f}% |"
        )
    return "\n".join(out)


def real_probe() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return "real probe skipped: no DEEPSEEK_API_KEY / ANTHROPIC_API_KEY in env."
    import urllib.request

    base = os.environ.get("PERMAFROST_UPSTREAM", "https://api.deepseek.com/anthropic").rstrip("/")
    big = ("You are a coding agent. Keep this prefix identical across turns so the "
           "context cache can serve it. ") * 80
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 8,
        "system": [{"type": "text", "text": big}],
        "messages": [{"role": "user", "content": "Reply with the single word: ok."}],
    }
    headers = {"content-type": "application/json", "x-api-key": key,
               "anthropic-version": "2023-06-01"}

    def send():
        req = urllib.request.Request(base + "/v1/messages",
                                     data=pa.canonical_dumps(body), headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    try:
        send()  # warm the cache
        import time
        time.sleep(3)
        second = send()
    except Exception as e:  # noqa: BLE001
        return f"real probe failed: {e}"
    u = pa.normalize_usage(second.get("usage", {}))
    return (f"real probe: 2nd identical request -> hit={u['hit']} miss={u['miss']} "
            f"(hit_rate={pa.hit_rate(u['hit'], u['miss'])*100:.0f}%) against {base}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Permafrost cache benchmark")
    ap.add_argument("--turns", type=int, default=12)
    ap.add_argument("--real", action="store_true", help="also run the live DeepSeek probe")
    ap.add_argument("--write", metavar="PATH", help="write a Markdown report to PATH")
    args = ap.parse_args()

    rows = run_all(args.turns)
    table = fmt_table(rows)
    print(table)

    real_line = real_probe() if args.real else None
    if real_line:
        print("\n" + real_line)

    if args.write:
        with open(args.write, "w", encoding="utf-8") as f:
            f.write(report_md(rows, args.turns, real_line))
        print(f"\nwrote {args.write}")


def report_md(rows: list[dict], turns: int, real_line: str | None) -> str:
    agg = next(r for r in rows if r["scenario"].startswith("C") and r["mode"] == "aggressive")
    off = next(r for r in rows if r["scenario"].startswith("C") and r["mode"] == "off")
    lines = [
        "# Permafrost benchmark results",
        "",
        f"Emulated DeepSeek prefix cache, {turns}-turn Claude-Code-shaped conversation. "
        "Prices are DeepSeek V4 Flash (USD/1M): "
        f"hit ${pa.DEFAULT_PRICES['hit_per_m']}, miss ${pa.DEFAULT_PRICES['miss_per_m']}, "
        f"output ${pa.DEFAULT_PRICES['output_per_m']}.",
        "",
        "Reproduce: `python3 benchmarks/bench.py --turns %d --write benchmarks/RESULTS.md`" % turns,
        "",
        fmt_table(rows),
        "",
        "## Headline (scenario C, the realistic Claude Code case)",
        "",
        f"- **off**: {off['hit_rate']*100:.1f}% hit rate, {off['anchor_changes']} anchor resets, "
        f"${off['cost_usd']:.5f}",
        f"- **aggressive**: {agg['hit_rate']*100:.1f}% hit rate, {agg['anchor_changes']} anchor resets, "
        f"${agg['cost_usd']:.5f}",
        f"- **cost reduction**: {(1 - agg['cost_usd']/off['cost_usd'])*100:.1f}% cheaper than off "
        "on identical traffic.",
        "",
        "Anchor resets = how many times the `tools`+`system` prefix changed bytes "
        "across the run. Every reset forces DeepSeek to re-read the whole prefix at "
        "full (miss) price. Permafrost's job is to drive this to 0.",
    ]
    if real_line:
        lines += ["", "## Live DeepSeek probe", "", "```", real_line, "```"]
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
