#!/usr/bin/env python3
"""Regression test against a structurally-faithful Claude Code request fixture.

`fixtures/cc_request.json` reproduces the SHAPE of a real CC 2.1.x request
(captured via PERMAFROST_DUMP_DIR, content replaced): billing-header system
block with a per-request `cch` nonce, a large system block embedding the
<env> context, ten tools including MCP names, a `<system-reminder>` first user
turn, nested-JSON `metadata.user_id`, `stream`/`max_tokens` set.

The test simulates two consecutive turns the way CC actually drifts between
requests — new cch nonce, changed gitStatus, reshuffled tools, grown messages —
and asserts the aligned anchor stays byte-identical. If a Claude Code release
changes its request shape in a way the pipeline doesn't neutralize, this is
the test that goes red (offline, no API key).
"""

from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))
import permafrost_align as pa  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "cc_request.json")


def _load() -> dict:
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _next_turn(req: dict) -> dict:
    """Mutate a copy the way Claude Code drifts between consecutive requests."""
    t2 = copy.deepcopy(req)
    # per-request billing nonce changes
    t2["system"][0]["text"] = t2["system"][0]["text"].replace("cch=bcc4d", "cch=a8245")
    # the user edited a file -> gitStatus moves inside the big system block
    t2["system"][2]["text"] = t2["system"][2]["text"].replace(
        "gitStatus: M src/a.py", "gitStatus: M src/a.py\nM src/b.py")
    # MCP servers finished connecting in a different order
    t2["tools"] = t2["tools"][::-1]
    # the conversation grew (assistant reply + tool result + new user turn)
    t2["messages"] = t2["messages"] + [
        {"role": "assistant", "content": [{"type": "text", "text": "Read it; the bug is in divide()."}]},
        {"role": "user", "content": [{"type": "text", "text": "Go ahead and fix it.",
                                      "cache_control": {"type": "ephemeral"}}]},
    ]
    return t2


def test_fixture_anchor_frozen_across_cc_drift() -> None:
    store = pa.FreezeStore()
    t1, t2 = _load(), _next_turn(_load())
    b1, r1 = pa.align_request(t1, "aggressive", store=store)
    b2, r2 = pa.align_request(t2, "aggressive", store=store)
    assert r1.anchor_fingerprint == r2.anchor_fingerprint, (
        "anchor must survive cch + gitStatus + tool-order drift")
    assert r1.lineage == r2.lineage
    assert pa.anchor_payload(b1) == pa.anchor_payload(b2)


def test_fixture_individual_transforms() -> None:
    store = pa.FreezeStore()
    b1, r1 = pa.align_request(_load(), "aggressive", store=store)
    assert r1.metadata_stabilized == 1, "cch nonce must be pinned"
    assert "cch=permafrost;" in b1["system"][0]["text"]
    assert r1.env_frozen, "the env-bearing system block must be frozen"
    assert r1.cache_control_stripped == 3
    assert "cache_control" not in json.dumps(b1)
    names = [t["name"] for t in b1["tools"]]
    assert names == sorted(names), "tools must come out sorted"

    b2, r2 = pa.align_request(_next_turn(_load()), "aggressive", store=store)
    # the frozen anchor keeps turn-1's gitStatus; turn-2's new line rides the tail
    assert "M src/b.py" not in json.dumps(b2["system"])
    assert r2.env_delta_lines == 1
    assert "M src/b.py" in json.dumps(b2["messages"][-1])


def test_fixture_session_and_metadata_intact() -> None:
    b, _ = pa.align_request(_load(), "aggressive", store=pa.FreezeStore())
    assert pa.extract_session(_load()) == "00000000-0000-4000-8000-000000000000"
    # alignment must never touch fields outside system/tools/cache_control
    assert b["metadata"] == _load()["metadata"]
    assert b["model"] == "claude-sonnet-4-6" and b["stream"] is True
    assert b["max_tokens"] == 32000


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
