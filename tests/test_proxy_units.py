#!/usr/bin/env python3
"""Unit tests for proxy helpers — the parts that don't need a running server.

Importing permafrost_proxy is safe: the server only starts inside main().
"""

from __future__ import annotations

import os
import sys

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
