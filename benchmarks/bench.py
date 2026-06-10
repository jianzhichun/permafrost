#!/usr/bin/env python3
"""Offline cache benchmark for Permafrost — faithful to measured DeepSeek behavior.

Replays a multi-turn, Claude-Code-shaped conversation through an emulator of
DeepSeek's Anthropic-endpoint cache, for each alignment mode (off/safe/
aggressive) and each class of cache-buster. No API key required; fully
deterministic. `--real` additionally fires two identical requests at the live
endpoint and prints the real cache-hit tokens.

The emulator is rebuilt to match what we measured against the live API
(docs/e2e-findings.md), not a naive raw-byte model:

  1. DeepSeek caches the RENDERED request in Anthropic's documented order
     (tools → system → messages), NOT the raw JSON body. Proven live: Claude
     Code serializes `messages` first, so raw byte-overlap between turns is ~6%,
     yet the hit rate is 66%. Body key order is therefore irrelevant; the
     cacheable anchor is tools+system.
  2. A hit must "fully match a cache prefix unit" from token 0 — quantized to
     ~64-token units (DeepSeek's measured granularity).
  3. Cache identity also includes the request params and the client's header
     fingerprint: a byte-identical body that differs in max_tokens/stream, or in
     headers, shares no cache (live: 0% vs 99.9%).
  4. The cache write is asynchronous — a unit is readable only after the writing
     response settles (~6s live), modeled here in virtual time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import permafrost_align as pa  # noqa: E402

UNIT_TOKENS = 64
BYTES_PER_TOKEN = 4  # rough, applied consistently to every arm
SETTLE_S = 6.0       # async cache-write delay (virtual seconds), from live probes
_IDENTITY_PARAMS = ("model", "max_tokens", "stream", "temperature", "top_p",
                    "top_k", "stop_sequences", "tool_choice")


def est_tokens(b: bytes) -> int:
    return len(b) // BYTES_PER_TOKEN


def byte_lcp(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def render(body: dict) -> bytes:
    """The bytes DeepSeek actually caches: tools → system → messages (Anthropic's
    documented render order), independent of the raw JSON body key order."""
    return (pa.canonical_dumps({"tools": body.get("tools")})
            + pa.canonical_dumps({"system": body.get("system")})
            + pa.canonical_dumps({"messages": body.get("messages")}))


def identity(body: dict, headers: dict | None = None) -> str:
    """Cache-identity namespace: params + client header fingerprint. Requests in
    different namespaces never share a cache entry even with an identical prefix."""
    params = pa.canonical_dumps({k: body.get(k) for k in _IDENTITY_PARAMS})
    hdr = pa.canonical_dumps(dict(sorted((headers or {}).items())))
    return hashlib.sha256(params + b"|" + hdr).hexdigest()[:16]


class DeepSeekCacheEmulator:
    """Renders → namespaces → unit-quantizes → async-settles. See module docstring."""

    def __init__(self, unit_tokens: int = UNIT_TOKENS, settle_s: float = SETTLE_S) -> None:
        self.unit = unit_tokens
        self.settle = settle_s
        self.store: list[tuple[str, bytes, float]] = []  # (identity, render, written_at)

    def send(self, body: dict, *, headers: dict | None = None, now: float = 1e9) -> dict[str, int]:
        r = render(body)
        idn = identity(body, headers)
        input_tokens = est_tokens(r)
        best = 0
        for prior_idn, prior, written_at in self.store:
            if prior_idn != idn:                 # different params/headers → no share
                continue
            if written_at + self.settle > now:   # async write hasn't landed yet
                continue
            best = max(best, byte_lcp(r, prior))
        # A hit only counts in whole prefix units fully matched from token 0.
        hit = (est_tokens(r[:best]) // self.unit) * self.unit
        hit = min(hit, input_tokens)
        self.store.append((idn, r, now))
        return {"hit": hit, "miss": input_tokens - hit}


class RawBytesEmulator:
    """The OLD (wrong) model: caches the raw JSON body prefix. Kept only to show,
    in the contrast section, why the rebuild was necessary — it under-reports
    because real Claude Code serializes `messages` first."""

    def __init__(self) -> None:
        self.seen: list[bytes] = []

    def send(self, body: dict, **_: object) -> dict[str, int]:
        raw = pa.canonical_dumps(body)
        input_tokens = est_tokens(raw)
        best = max((byte_lcp(raw, p) for p in self.seen), default=0)
        hit = min((est_tokens(raw[:best]) // UNIT_TOKENS) * UNIT_TOKENS, input_tokens)
        self.seen.append(raw)
        return {"hit": hit, "miss": input_tokens - hit}


# --- synthetic Claude Code traffic -----------------------------------------

_INSTRUCTION = (
    "You are Claude Code, an interactive CLI coding agent. Follow the user's "
    "instructions precisely, use the provided tools to inspect and edit the "
    "repository, prefer minimal diffs, and explain your reasoning concisely. "
) * 60  # byte-stable across turns

_TOOL_NAMES = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch",
    "Task", "TodoWrite", "NotebookEdit", "mcp__github__list_prs",
    "mcp__github__create_issue", "mcp__postgres__query",
]


def _tool(name: str) -> dict:
    return {"name": name,
            "description": f"The {name} tool. Use it to {name.lower()} things in the workspace.",
            "input_schema": {"type": "object",
                             "properties": {"arg": {"type": "string", "description": "primary argument"}},
                             "required": ["arg"]}}


def _env_block(turn: int, volatile: bool) -> dict:
    git = f"M src/module_{turn}.py\n A tests/test_{turn}.py" if volatile else "clean"
    return {"type": "text",
            "text": ("<env>\nWorking directory: /home/dev/repo\nIs directory a git repo: yes\n"
                     f"Platform: linux\nToday's date: 2026-06-10\nCurrent branch: main\n"
                     f"gitStatus: {git}\n</env>"),
            "cache_control": {"type": "ephemeral"}}


def build_request(turn: int, history: list[dict], *, shuffle_tools: bool,
                  volatile_env: bool) -> dict:
    """One request shaped like real Claude Code — note the JSON key order puts
    `messages` BEFORE `system`/`tools`, exactly as CC serializes it. The faithful
    emulator caches the rendered (tools→system→messages) form regardless, which
    is the whole point."""
    system = [{"type": "text", "text": _INSTRUCTION, "cache_control": {"type": "ephemeral"}},
              _env_block(turn, volatile_env)]
    names = list(_TOOL_NAMES)
    if shuffle_tools:
        rot = turn % len(names)
        names = names[rot:] + names[:rot]  # MCP servers binding in a different order
    msgs = list(history)
    msgs.append({"role": "user", "content": [{"type": "text",
                "text": f"Turn {turn}: please continue the task and edit the next file.",
                "cache_control": {"type": "ephemeral"}}]})
    return {"model": "claude-sonnet-4-6", "max_tokens": 4096, "stream": True,
            "metadata": {"user_id": "{\"session_id\":\"bench\"}"},
            "messages": msgs, "system": system, "tools": [_tool(n) for n in names]}


def run_scenario(name: str, *, shuffle_tools: bool, volatile_env: bool,
                 mode: str, turns: int) -> dict:
    emu = DeepSeekCacheEmulator()
    history: list[dict] = []
    tot = {"hit": 0, "miss": 0, "output": 0}
    anchors: set[str] = set()

    for turn in range(turns):
        req = build_request(turn, history, shuffle_tools=shuffle_tools, volatile_env=volatile_env)
        aligned, report = pa.align_request(json.loads(json.dumps(req)), mode)
        anchors.add(report.anchor_fingerprint)
        # turns are far apart in virtual time, so each turn's write has settled.
        u = emu.send(aligned, now=turn * 100.0 + 50.0)
        tot["hit"] += u["hit"]; tot["miss"] += u["miss"]; tot["output"] += 220
        history = req["messages"]
        history.append({"role": "assistant", "content": [{"type": "text",
                        "text": f"Done with turn {turn}. Edited the file and ran tests."}]})

    cost = pa.cost_usd(tot["hit"], tot["miss"], tot["output"])
    baseline = pa.cost_usd(0, tot["hit"] + tot["miss"], tot["output"])
    return {"scenario": name, "mode": mode, "turns": turns, **tot,
            "hit_rate": pa.hit_rate(tot["hit"], tot["miss"]),
            "anchor_changes": len(anchors) - 1, "cost_usd": cost,
            "cost_if_all_miss": baseline,
            "saved_pct": (1 - cost / baseline) * 100 if baseline else 0.0}


SCENARIOS = [
    ("A: MCP tool-order churn", dict(shuffle_tools=True, volatile_env=False)),
    ("B: live git/env block", dict(shuffle_tools=False, volatile_env=True)),
    ("C: both (realistic CC)", dict(shuffle_tools=True, volatile_env=True)),
]
MODES = ["off", "safe", "aggressive"]


def run_all(turns: int) -> list[dict]:
    return [run_scenario(n, mode=m, turns=turns, **cfg)
            for n, cfg in SCENARIOS for m in MODES]


def fmt_table(rows: list[dict]) -> str:
    out = ["| Scenario | Mode | Hit rate | Anchor resets | Cost (USD) | vs all-miss |",
           "|---|---|---:|---:|---:|---:|"]
    for r in rows:
        out.append(f"| {r['scenario']} | {r['mode']} | {r['hit_rate']*100:.1f}% | "
                   f"{r['anchor_changes']} | ${r['cost_usd']:.5f} | -{r['saved_pct']:.1f}% |")
    return "\n".join(out)


def contrast_demo(turns: int) -> str:
    """Run scenario C / aggressive through both emulators to show why the
    raw-byte model was wrong: it under-reports because real CC puts messages
    first, while DeepSeek re-renders tools+system to the front."""
    canon = DeepSeekCacheEmulator()
    raw = RawBytesEmulator()
    history: list[dict] = []
    ch = cm = rh = rm = 0
    for turn in range(turns):
        req = build_request(turn, history, shuffle_tools=True, volatile_env=True)
        aligned, _ = pa.align_request(json.loads(json.dumps(req)), "aggressive")
        cu = canon.send(json.loads(json.dumps(aligned)), now=turn * 100.0 + 50.0)
        ru = raw.send(json.loads(json.dumps(aligned)))
        ch += cu["hit"]; cm += cu["miss"]; rh += ru["hit"]; rm += ru["miss"]
        history = req["messages"]
        history.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    return (f"faithful (renders tools+system first): {pa.hit_rate(ch, cm)*100:.0f}% hit  vs  "
            f"raw-body model (CC's messages-first order): {pa.hit_rate(rh, rm)*100:.0f}% hit "
            f"— the raw model under-reports because it keys on bytes DeepSeek doesn't cache.")


def identity_demo() -> str:
    """Two identical-prefix requests that differ only in max_tokens get no shared
    cache — matching the live finding that params are part of cache identity."""
    emu = DeepSeekCacheEmulator()
    base = {"model": "m", "max_tokens": 8, "system": [{"type": "text", "text": "x" * 4000}],
            "tools": [], "messages": [{"role": "user", "content": "go"}]}
    emu.send(json.loads(json.dumps(base)), now=0.0)
    same = emu.send(json.loads(json.dumps(base)), now=100.0)        # identical → hits
    diff = dict(json.loads(json.dumps(base)), max_tokens=9)         # only max_tokens differs
    diffu = emu.send(diff, now=200.0)                              # → no shared cache
    return (f"same params, settled: {pa.hit_rate(same['hit'], same['miss'])*100:.0f}% hit  ·  "
            f"max_tokens changed: {pa.hit_rate(diffu['hit'], diffu['miss'])*100:.0f}% hit")


def real_probe() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return "real probe skipped: no DEEPSEEK_API_KEY / ANTHROPIC_API_KEY in env."
    import time
    import urllib.request
    base = os.environ.get("PERMAFROST_UPSTREAM", "https://api.deepseek.com/anthropic").rstrip("/")
    big = ("You are a coding agent. Keep this prefix identical across turns so the "
           "context cache can serve it. ") * 80
    body = {"model": "claude-sonnet-4-6", "max_tokens": 8,
            "system": [{"type": "text", "text": big}],
            "messages": [{"role": "user", "content": "Reply with the single word: ok."}]}
    headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}

    def send():
        req = urllib.request.Request(base + "/v1/messages", data=pa.canonical_dumps(body), headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    try:
        send()
        time.sleep(7)
        second = send()
    except Exception as e:  # noqa: BLE001
        return f"real probe failed: {e}"
    u = pa.normalize_usage(second.get("usage", {}))
    return (f"real probe: 2nd identical request -> hit={u['hit']} miss={u['miss']} "
            f"(hit_rate={pa.hit_rate(u['hit'], u['miss'])*100:.0f}%) against {base}")


def report_md(rows: list[dict], turns: int, real_line: str | None) -> str:
    agg = next(r for r in rows if r["scenario"].startswith("C") and r["mode"] == "aggressive")
    off = next(r for r in rows if r["scenario"].startswith("C") and r["mode"] == "off")
    lines = [
        "# Permafrost benchmark results",
        "",
        f"Emulated DeepSeek cache, {turns}-turn Claude-Code-shaped conversation. Prices are "
        f"DeepSeek V4 Flash (USD/1M): hit ${pa.DEFAULT_PRICES['hit_per_m']}, "
        f"miss ${pa.DEFAULT_PRICES['miss_per_m']}, output ${pa.DEFAULT_PRICES['output_per_m']}.",
        "",
        "Reproduce: `python3 benchmarks/bench.py --turns %d --write benchmarks/RESULTS.md`" % turns,
        "",
        "## Methodology — what the emulator models (and why it changed)",
        "",
        "Rebuilt to match the live findings in [`docs/e2e-findings.md`](../docs/e2e-findings.md):",
        "DeepSeek caches the **rendered** request (`tools → system → messages`, Anthropic's",
        "documented order), **not** the raw JSON body — so Claude Code serializing `messages`",
        "first is irrelevant, and the cacheable anchor is `tools`+`system`. A hit must fully",
        "match a ~64-token prefix unit from token 0; cache identity also includes request",
        "params and the client header fingerprint; writes settle asynchronously (~6s).",
        "",
        fmt_table(rows),
        "",
        "## Headline (scenario C, the realistic Claude Code case)",
        "",
        f"- **off**: {off['hit_rate']*100:.1f}% hit, {off['anchor_changes']} anchor resets, "
        f"${off['cost_usd']:.5f} — tools render first, so churned tool order busts the prefix at byte 0.",
        f"- **aggressive**: {agg['hit_rate']*100:.1f}% hit, {agg['anchor_changes']} anchor resets, "
        f"${agg['cost_usd']:.5f}.",
        f"- **cost reduction**: {(1 - agg['cost_usd']/off['cost_usd'])*100:.1f}% cheaper than off."
        if off["cost_usd"] else "",
        "",
        "## Why the model was rebuilt (raw-byte vs faithful)",
        "",
        "```",
        contrast_demo(turns),
        "```",
        "",
        "## Cache identity includes params (live finding, reproduced offline)",
        "",
        "```",
        identity_demo(),
        "```",
    ]
    if real_line:
        lines += ["", "## Live DeepSeek probe", "", "```", real_line, "```"]
    lines.append("")
    return "\n".join([ln for ln in lines if ln is not None])


def main() -> None:
    ap = argparse.ArgumentParser(description="Permafrost cache benchmark")
    ap.add_argument("--turns", type=int, default=12)
    ap.add_argument("--real", action="store_true", help="also run the live DeepSeek probe")
    ap.add_argument("--write", metavar="PATH", help="write a Markdown report to PATH")
    args = ap.parse_args()

    rows = run_all(args.turns)
    print(fmt_table(rows))
    print("\ncontrast:", contrast_demo(args.turns))
    print("identity:", identity_demo())
    real_line = real_probe() if args.real else None
    if real_line:
        print("\n" + real_line)
    if args.write:
        with open(args.write, "w", encoding="utf-8") as f:
            f.write(report_md(rows, args.turns, real_line))
        print(f"\nwrote {args.write}")


if __name__ == "__main__":
    main()
