#!/usr/bin/env python3
"""Unit tests for proxy helpers — the parts that don't need a running server.

Importing permafrost_proxy is safe: the server only starts inside main().
"""

from __future__ import annotations

import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))
import permafrost_align as pa  # noqa: E402
import permafrost_proxy as pp  # noqa: E402


def test_messages_path_matches_with_query_and_slash() -> None:
    # P0 regression: a query string must not make the proxy skip alignment.
    assert pp._is_messages_path("/v1/messages")
    assert pp._is_messages_path("/v1/messages?beta=true")
    assert pp._is_messages_path("/v1/messages/")
    assert pp._is_messages_path("/anthropic/v1/messages?x=1")
    assert not pp._is_messages_path("/v1/models")
    assert not pp._is_messages_path("/v1/messages/batches")


def test_normalize_beta_sorts_and_dedups_without_dropping() -> None:
    assert pp._normalize_beta("feat-b,feat-a,feat-b") == "feat-a,feat-b"
    assert pp._normalize_beta(" b , a ") == "a,b"
    assert pp._normalize_beta("only-one") == "only-one"
    # never invents a flag, never empties a non-empty distinct set
    assert pp._normalize_beta("z,y,x") == "x,y,z"


def test_sniff_usage_streaming_head_and_tail() -> None:
    # message_start carries input/cache usage; message_delta (at the tail)
    # carries output. Both must be captured.
    sse = (
        'data: {"type":"message_start","message":{"usage":'
        '{"input_tokens":50,"cache_read_input_tokens":900,"output_tokens":1}}}\n\n'
        'data: {"type":"message_delta","usage":{"output_tokens":120}}\n\n'
        "data: [DONE]\n\n"
    )
    u = pp._sniff_usage(sse)
    assert u == {"input": 950, "hit": 900, "miss": 50, "output": 120}


def test_merge_usage_streaming_split() -> None:
    # message_start (head) carries hit/miss; message_delta (tail) carries output.
    head = pp._sniff_usage(
        'data: {"type":"message_start","message":{"usage":'
        '{"input_tokens":50,"cache_read_input_tokens":900}}}\n\n')
    tail = pp._sniff_usage('data: {"type":"message_delta","usage":{"output_tokens":120}}\n\n')
    assert pp._merge_usage(head, tail) == {"hit": 900, "miss": 50, "output": 120, "input": 950}


def test_merge_usage_nonstreaming_json_in_both_buffers() -> None:
    # Regression: a non-streaming response sits whole in head AND tail (small
    # body). Merging two full copies must not double-count or drop usage.
    body = ('{"id":"x","usage":{"prompt_tokens":1000,"prompt_cache_hit_tokens":800,'
            '"prompt_cache_miss_tokens":200,"completion_tokens":40}}')
    u = pp._merge_usage(pp._sniff_usage(body), pp._sniff_usage(body))
    assert u == {"hit": 800, "miss": 200, "output": 40, "input": 1000}


def test_normalize_usage_counts_cache_creation_as_miss() -> None:
    # Anthropic shape: tokens written to cache this turn are billed at full
    # price (plus a premium), so they count as misses, not hits.
    u = pa.normalize_usage({
        "input_tokens": 30, "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 770, "output_tokens": 40,
    })
    assert u == {"input": 1000, "hit": 770, "miss": 230, "output": 40}


def test_coalesce_holds_followers_until_leader_releases() -> None:
    c = pp.Coalescer(enabled=True, timeout_s=3.0)
    role, gate = c.begin("fp1")
    assert role == "leader"

    barrier = threading.Barrier(4)  # 3 followers + main
    roles: list[str] = []
    released: list[int] = []

    def follower() -> None:
        r, g = c.begin("fp1")
        roles.append(r)
        barrier.wait()          # all begin() calls are done
        c.wait_follower(g)      # blocks until the leader releases
        released.append(1)

    threads = [threading.Thread(target=follower) for _ in range(3)]
    for t in threads:
        t.start()
    barrier.wait()
    assert roles == ["follower"] * 3
    assert c.held == 3
    assert released == []        # still parked — leader hasn't fired a byte

    c.release(gate)              # leader's first upstream byte
    for t in threads:
        t.join(timeout=3)
    assert len(released) == 3
    assert c.released == 3 and c.leaders == 1 and c.timeouts == 0

    c.warm("fp1", gate)          # leader finished — anchor is warm for good
    assert c.begin("fp1")[0] == "pass"


def test_coalesce_follower_times_out() -> None:
    c = pp.Coalescer(enabled=True, timeout_s=0.2)
    c.begin("x")                 # leader, never releases
    role, gate = c.begin("x")
    assert role == "follower"
    c.wait_follower(gate)        # ~0.2s then gives up
    assert c.timeouts == 1


def test_coalesce_failed_leader_lets_next_be_leader() -> None:
    c = pp.Coalescer(enabled=True)
    _, g = c.begin("y")
    c.fail("y", g)               # upstream unreachable: drop the anchor
    assert c.begin("y")[0] == "leader"
    assert c.leaders == 2


def test_coalesce_disabled_passes_through() -> None:
    c = pp.Coalescer(enabled=False)
    assert c.begin("z") == ("pass", None)


def test_coalesce_no_fingerprint_passes() -> None:
    c = pp.Coalescer(enabled=True)
    assert c.begin(None) == ("pass", None)


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
