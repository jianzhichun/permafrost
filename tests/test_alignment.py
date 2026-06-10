#!/usr/bin/env python3
"""Tests for the alignment pipeline. Pure and offline — no network, no API key.

Run directly (`python3 tests/test_alignment.py`) or under pytest. The central
property is the same one Reasonix proves in its cachehit_e2e_test.go: across an
append-only conversation, the cache anchor (tools + system) must stay
byte-identical, so DeepSeek's prefix cache serves the whole anchor every turn.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import permafrost_align as pa  # noqa: E402


def _system(turn: int) -> list[dict]:
    return [
        {"type": "text", "text": "Stable agent instructions. " * 50,
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"<env>\nToday's date: 2026-06-10\n"
                                 f"gitStatus: M file_{turn}.py\n</env>"},
    ]


def _tools(turn: int) -> list[dict]:
    names = ["Bash", "Read", "Edit", "Grep", "mcp__db__query"]
    rot = turn % len(names)
    names = names[rot:] + names[:rot]  # reshuffle each turn
    return [{"name": n, "description": f"{n} tool",
             "input_schema": {"type": "object", "properties": {}}} for n in names]


def _request(turn: int, messages: list[dict]) -> dict:
    return {"model": "claude-sonnet-4-6", "system": _system(turn),
            "tools": _tools(turn), "messages": messages}


def test_sort_tools_is_deterministic() -> None:
    a, _ = pa.align_request(_request(0, []), "safe")
    b, _ = pa.align_request(_request(3, []), "safe")  # different input order
    assert [t["name"] for t in a["tools"]] == [t["name"] for t in b["tools"]]
    assert [t["name"] for t in a["tools"]] == sorted(t["name"] for t in a["tools"])


def test_cache_control_stripped() -> None:
    body, report = pa.align_request(_request(0, []), "safe")
    assert report.cache_control_stripped >= 1
    assert "cache_control" not in json.dumps(body)


def test_volatile_detected() -> None:
    _, report = pa.align_request(_request(0, []), "off")
    assert report.volatile_found.get("date", 0) >= 1


def test_aggressive_relocates_env_out_of_anchor() -> None:
    body, report = pa.align_request(_request(0, [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}]), "aggressive")
    assert report.blocks_relocated == 1
    # The env/git text must no longer live in the system anchor...
    assert "gitStatus" not in json.dumps(body["system"])
    # ...but must still be present in the request (moved to the last turn).
    assert "gitStatus" in json.dumps(body["messages"])


def test_anchor_frozen_across_turns_aggressive() -> None:
    """The killer property: tools+system anchor is byte-identical every turn."""
    history: list[dict] = []
    anchors = set()
    for turn in range(8):
        history.append({"role": "user", "content": [{"type": "text", "text": f"t{turn}"}]})
        body, report = pa.align_request(_request(turn, list(history)), "aggressive")
        anchors.add(report.anchor_fingerprint)
        history.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    assert len(anchors) == 1, f"anchor changed across turns: {anchors}"


def test_anchor_unstable_when_off() -> None:
    """Without alignment, the same traffic resets the anchor every turn."""
    history: list[dict] = []
    anchors = set()
    for turn in range(8):
        history.append({"role": "user", "content": [{"type": "text", "text": f"t{turn}"}]})
        _, report = pa.align_request(_request(turn, list(history)), "off")
        anchors.add(report.anchor_fingerprint)
        history.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    assert len(anchors) > 1, "expected the unaligned anchor to churn"


def test_serialized_prefix_grows_monotonically_aggressive() -> None:
    """Each aligned request's bytes share an ever-longer prefix with the last.

    This is what makes DeepSeek's hit tokens climb: request N's cached prefix is
    (almost) all of request N-1.
    """
    history: list[dict] = []
    prev: bytes | None = None
    for turn in range(6):
        history.append({"role": "user", "content": [{"type": "text", "text": f"turn {turn} body"}]})
        body, _ = pa.align_request(_request(turn, list(history)), "aggressive")
        cur = pa.canonical_dumps(body)
        if prev is not None:
            n = min(len(prev), len(cur))
            lcp = 0
            while lcp < n and prev[lcp] == cur[lcp]:
                lcp += 1
            # The shared prefix must cover the whole anchor (tools+system+model),
            # i.e. thousands of bytes, not just the opening brace.
            assert lcp > 1000, f"prefix shared only {lcp} bytes at turn {turn}"
        prev = cur
        history.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})


def test_normalize_usage_both_shapes() -> None:
    ds = pa.normalize_usage({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800,
                             "prompt_cache_miss_tokens": 200, "completion_tokens": 50})
    assert ds == {"input": 1000, "hit": 800, "miss": 200, "output": 50}
    an = pa.normalize_usage({"input_tokens": 200, "cache_read_input_tokens": 800,
                             "output_tokens": 50})
    assert an["hit"] == 800 and an["miss"] == 200 and an["output"] == 50


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
