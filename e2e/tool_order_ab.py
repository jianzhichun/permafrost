#!/usr/bin/env python3
"""The decisive A/B: does DeepSeek's cache key on tool ORDER?

This is the test that establishes where Permafrost actually has value. A vanilla
Claude Code session already hits DeepSeek's cache ~90% (its system prompt is
globally shared and stays warm), so Permafrost adds nothing there. Its job is the
case where the tool list is NOT stable — MCP servers connecting/reshuffling — so
the question that decides whether the tool is worth anything is simply: does
reordering the tools bust DeepSeek's cache?

Two arms against the live DeepSeek Anthropic endpoint, fixed system + fixed
messages, only the tool *order* varies:
  A (bare):       rotate the tool order each request — what a churning MCP set produces
  B (Permafrost): always sorted — what Permafrost emits

Result we measured (2026-06): bare 33% hit, Permafrost 71% hit. Tools render
first, so a reordered list diverges the cached prefix at byte 0. Run it yourself:

  DEEPSEEK_API_KEY=sk-... python3 e2e/tool_order_ab.py
"""
from __future__ import annotations
import json, os, sys, time, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import permafrost_align as pa  # noqa: E402

KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
if not KEY:
    sys.exit("set DEEPSEEK_API_KEY (a funded DeepSeek key)")
BASE = os.environ.get("PERMAFROST_UPSTREAM", "https://api.deepseek.com/anthropic").rstrip("/")
NAMES = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "Task", "Skill", "mcp__db__query"]


def _tool(n: str) -> dict:
    return {"name": n, "description": f"Tool {n} does {n}-related work in the workspace.",
            "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}, "required": ["arg"]}}


def _send(tools: list[dict], tag: str) -> dict:
    body = {"model": "claude-sonnet-4-6", "max_tokens": 8,
            "system": [{"type": "text",
                        "text": f"UNIQUE-{tag}. " + ("You are a coding agent; keep this prefix stable. " * 90)}],
            "tools": tools, "messages": [{"role": "user", "content": "Reply ok."}]}
    req = urllib.request.Request(BASE + "/v1/messages", data=pa.canonical_dumps(body),
                                 headers={"content-type": "application/json", "x-api-key": KEY,
                                          "anthropic-version": "2023-06-01"})
    return pa.normalize_usage(json.loads(urllib.request.urlopen(req, timeout=120).read()).get("usage", {}))


def run(label: str, sort: bool, tag: str, reqs: int = 4) -> float:
    h = m = 0
    print(f"\n[{label}]")
    for i in range(reqs):
        names = sorted(NAMES) if sort else NAMES[i:] + NAMES[:i]
        u = _send([_tool(n) for n in names], tag)
        h += u["hit"]; m += u["miss"]
        print(f"  req{i}: order={'sorted' if sort else names[0] + '..'}  hit={u['hit']} miss={u['miss']}")
        time.sleep(7)  # let DeepSeek's async cache write settle
    rate = 100 * h / (h + m) if (h + m) else 0
    print(f"  => {label}: {rate:.0f}% hit ({h} hit / {m} miss)")
    return rate


if __name__ == "__main__":
    import time as _t
    tag = str(int(_t.time()))  # fresh anchors, not the globally-warm CC one
    a = run("A: bare — tool order rotates each request (churning MCP)", False, "rot" + tag)
    b = run("B: Permafrost — tools always sorted", True, "sort" + tag)
    print(f"\n=== VERDICT ===\n  bare (rotating)   : {a:.0f}% hit\n  Permafrost (sorted): {b:.0f}% hit")
    print("  -> Permafrost helps iff B >> A, i.e. DeepSeek keys on tool order (it does).")
