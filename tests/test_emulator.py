#!/usr/bin/env python3
"""Tests that the benchmark emulator is faithful to measured DeepSeek behavior.

These lock the four properties the offline benchmark relies on, each tied to a
finding in docs/e2e-findings.md. Pure, offline.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "benchmarks"))
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))
import bench  # noqa: E402


def _body(**over) -> dict:
    b = {"model": "claude-sonnet-4-6", "max_tokens": 8, "stream": True,
         "system": [{"type": "text", "text": "stable instructions " * 300}],
         "tools": [{"name": "Read"}, {"name": "Edit"}],
         "messages": [{"role": "user", "content": "go"}]}
    b.update(over)
    return b


def test_render_ignores_body_key_order() -> None:
    # Finding 1: DeepSeek caches the rendered request, so two bodies with the
    # same tools/system/messages but different JSON key order are cache-identical.
    a = {"model": "m", "system": [{"type": "text", "text": "S"}],
         "tools": [{"name": "T"}], "messages": [{"role": "user", "content": "U"}]}
    b = {"messages": [{"role": "user", "content": "U"}], "tools": [{"name": "T"}],
         "model": "m", "system": [{"type": "text", "text": "S"}]}
    assert bench.render(a) == bench.render(b)


def test_identity_namespaces_on_params() -> None:
    # Finding 4: params are part of cache identity.
    assert bench.identity(_body(max_tokens=8)) != bench.identity(_body(max_tokens=9))
    assert bench.identity(_body(stream=True)) != bench.identity(_body(stream=False))
    assert bench.identity(_body()) == bench.identity(_body())  # same params → same id
    # ...and on the client header fingerprint.
    h1 = {"user-agent": "claude-cli/2.1"}
    h2 = {"user-agent": "some-script/1.0"}
    assert bench.identity(_body(), h1) != bench.identity(_body(), h2)


def test_param_mismatch_shares_no_cache() -> None:
    emu = bench.DeepSeekCacheEmulator()
    emu.send(_body(max_tokens=8), now=0.0)
    same = emu.send(_body(max_tokens=8), now=100.0)     # settled, identical → hits
    diff = emu.send(_body(max_tokens=9), now=200.0)     # different identity → misses
    assert same["hit"] > 0
    assert diff["hit"] == 0


def test_unit_quantization_partial_prefix_is_zero() -> None:
    # A shared prefix shorter than one 64-token (~256-byte) unit doesn't count.
    emu = bench.DeepSeekCacheEmulator()
    a = {"model": "m", "tools": [], "system": [{"type": "text", "text": "common tail same"}],
         "messages": [{"role": "user", "content": "AAAA"}]}
    b = {"model": "m", "tools": [], "system": [{"type": "text", "text": "common tail same"}],
         "messages": [{"role": "user", "content": "BBBB"}]}
    # tools+system render identical but it's < one unit; messages diverge.
    emu.send(a, now=0.0)
    u = emu.send(b, now=100.0)
    assert u["hit"] == 0, "a sub-unit shared prefix must not count as a hit"


def test_async_write_not_visible_before_settle() -> None:
    # Finding: the cache write is async. An identical request fired before the
    # writer settles still misses; after settle it hits.
    emu = bench.DeepSeekCacheEmulator(settle_s=6.0)
    big = _body(system=[{"type": "text", "text": "x" * 8000}])
    emu.send(json.loads(json.dumps(big)), now=0.0)
    early = emu.send(json.loads(json.dumps(big)), now=3.0)   # within settle → miss
    late = emu.send(json.loads(json.dumps(big)), now=10.0)   # after settle → hit
    assert early["hit"] == 0
    assert late["hit"] > 0


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
