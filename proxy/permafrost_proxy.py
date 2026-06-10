#!/usr/bin/env python3
"""permafrost_proxy — a cache-stabilizing Anthropic passthrough for DeepSeek.

Claude Code speaks the Anthropic Messages API. DeepSeek exposes an
Anthropic-compatible endpoint (https://api.deepseek.com/anthropic), so no
protocol translation is needed — Permafrost is a *passthrough* that does exactly
one thing: it rewrites the cache-relevant bytes of each /v1/messages request so
DeepSeek's automatic prefix cache keeps hitting, then streams the upstream
response back untouched. Every other path (GET /v1/models, batches, …) is
forwarded verbatim.

    Claude Code ──Anthropic /v1/messages──▶ Permafrost ──▶ DeepSeek /anthropic
                                              │
                                              ├─ align_request() freezes the prefix
                                              └─ records real cache hit/miss tokens

Run:  python3 permafrost_proxy.py            # 127.0.0.1:8787 -> api.deepseek.com
Env:
  PERMAFROST_PORT       (default 8787)
  PERMAFROST_HOST       (default 127.0.0.1)
  PERMAFROST_UPSTREAM   (default https://api.deepseek.com/anthropic)
  PERMAFROST_MODE       off | safe | aggressive   (default aggressive)
  PERMAFROST_FREEZE_ENV 1 to freeze the env block into the cached anchor and emit
                        only changed lines on the tail (default 1); 0 relocates
  PERMAFROST_NORMALIZE_BETA  1 to sort+dedup the anthropic-beta header (default 1)
  PERMAFROST_COALESCE   1 to hold parallel cold-anchor requests until the first
                        one warms the cache (default 1); 0 disables
  PERMAFROST_COALESCE_TIMEOUT_S  follower deadlock guard (default 30)
  PERMAFROST_COALESCE_SETTLE_MS  extra wait after release for the async cache
                        write to land (default 2500; live probes show ~6s to settle)
  PERMAFROST_COALESCE_RELEASE  first_byte (default, lowest latency) | completion
                        (release followers only after the leader fully streamed —
                        the boundary cache unit is persisted then; max hit odds)
  PERMAFROST_KEEPALIVE_S  OPT-IN: replay the last request (max_tokens=1) after this
                        many idle seconds to keep the cache warm (default 0 = off;
                        fires real billable requests at ~hit price)
  PERMAFROST_KEEPALIVE_IDLE_STOP_S  stop keepalives after this much idle (default 7200)
  PERMAFROST_PRICES     "hit,miss,output" USD per 1M to override the cost model
  PERMAFROST_DUMP_DIR   debug: write each aligned request's full body here, to
                        diff what a client varies between turns

Local introspection (GET):
  /permafrost/health    liveness + config
  /permafrost/stats     session + rolling cache stats
  /permafrost/doctor    last request's alignment report + prefix-change history
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import permafrost_align as pa  # noqa: E402

HOST = os.environ.get("PERMAFROST_HOST", "127.0.0.1")
PORT = int(os.environ.get("PERMAFROST_PORT", "8787"))
UPSTREAM = os.environ.get("PERMAFROST_UPSTREAM", "https://api.deepseek.com/anthropic").rstrip("/")
MODE = os.environ.get("PERMAFROST_MODE", "aggressive")
NORMALIZE_BETA = os.environ.get("PERMAFROST_NORMALIZE_BETA", "1") == "1"
FREEZE_ENV = os.environ.get("PERMAFROST_FREEZE_ENV", "1") == "1"
FREEZE_STORE = pa.FreezeStore() if FREEZE_ENV else None
DUMP_DIR = os.environ.get("PERMAFROST_DUMP_DIR")  # debug: write each request's anchor here

_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "accept-encoding",
}

# --- pooled upstream connections ---------------------------------------------
# One persistent connection per proxy thread (ThreadingHTTPServer gives each
# client connection its own thread, and Claude Code keeps its client connection
# alive), so consecutive turns reuse the TCP+TLS session instead of paying a
# fresh handshake (~100-300ms TTFT) every request.
_UP = urllib.parse.urlsplit(UPSTREAM)
_TL = threading.local()


def _upstream_conn(fresh: bool = False) -> tuple[http.client.HTTPConnection, bool]:
    """Return (conn, reused). `reused` is True only when we handed back an
    existing pooled connection — the one case where a send failure is a safely-
    retryable stale keep-alive rather than a real (possibly half-applied) error."""
    conn = None if fresh else getattr(_TL, "conn", None)
    reused = conn is not None
    if conn is None:
        if _UP.scheme == "https":
            conn = http.client.HTTPSConnection(_UP.hostname, _UP.port or 443, timeout=600)
        else:
            conn = http.client.HTTPConnection(_UP.hostname, _UP.port or 80, timeout=600)
        _TL.conn = conn
    return conn, reused


def _drop_upstream_conn() -> None:
    conn = getattr(_TL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _TL.conn = None

_SNIFF_HEAD = 1 << 18  # 256 KB — message_start (input/cache usage) lives here
_SNIFF_TAIL = 1 << 16  # 64 KB  — message_delta (output usage) lives here


def _prices() -> dict[str, float]:
    raw = os.environ.get("PERMAFROST_PRICES")
    if not raw:
        return pa.DEFAULT_PRICES
    try:
        h, m, o = (float(x) for x in raw.split(","))
        return {"hit_per_m": h, "miss_per_m": m, "output_per_m": o}
    except (ValueError, TypeError):
        return pa.DEFAULT_PRICES


def _is_messages_path(path: str) -> bool:
    """True for the Messages endpoint, query string and trailing slash aside."""
    clean = urllib.parse.urlsplit(path).path.rstrip("/")
    return clean.endswith("/v1/messages") or clean.endswith("/messages")


def _normalize_beta(value: str) -> str:
    """Sort + dedup a comma-separated anthropic-beta value.

    This stabilizes the header's bytes across a session without ever adding or
    removing a flag — purely reordering, which is always safe. (For DeepSeek's
    body-anchored cache the header isn't even part of the key; this is cheap
    insurance for byte-sensitive gateways and real Anthropic upstreams.)
    """
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return ",".join(sorted(dict.fromkeys(parts)))


COALESCE = os.environ.get("PERMAFROST_COALESCE", "1") == "1"
COALESCE_TIMEOUT_S = float(os.environ.get("PERMAFROST_COALESCE_TIMEOUT_S", "30"))
# DeepSeek's cache write is asynchronous: our live probes show an identical
# request ~4s after the leader's first byte still misses, ~6s hits. Releasing
# followers with no settle mostly wastes the wait, so default to 2.5s.
COALESCE_SETTLE_MS = int(os.environ.get("PERMAFROST_COALESCE_SETTLE_MS", "2500"))
# first_byte: release followers at the leader's first upstream byte + settle
#             (lowest added latency, cache write may still be in flight).
# completion: release when the leader's response has fully streamed (the
#             request-boundary cache unit is persisted right after) + settle —
#             higher latency, maximum hit probability.
COALESCE_RELEASE = os.environ.get("PERMAFROST_COALESCE_RELEASE", "first_byte")
COALESCE_CAP = int(os.environ.get("PERMAFROST_COALESCE_CAP", "1024"))


class Coalescer:
    """Cold-anchor coalescing for parallel fan-out (e.g. CC Task subagents).

    DeepSeek's cache is written asynchronously, so N requests that share a brand
    new prefix and fire at once all miss — none can read what the others are
    still writing. The coalescer lets the *first* request on an unseen anchor
    through as the "leader" and holds same-anchor "followers" until the leader's
    response starts streaming (the cache write has begun), then releases them so
    they read the warm prefix instead of all paying the cold-miss price.

    Single requests are never delayed: a lone request is its own leader and
    passes straight through. Only concurrent same-anchor bursts wait. Once an
    anchor is warm, everyone passes immediately. A follower never waits past
    `timeout_s` (deadlock guard).
    """

    def __init__(self, enabled: bool = True, timeout_s: float = 30.0,
                 settle_ms: int = 0, cap: int = 1024) -> None:
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.settle_ms = settle_ms
        self.cap = cap
        self.lock = threading.Lock()
        self.anchors: dict[str, dict] = {}  # fp -> {"state": warming|warm, "gate": Event}
        self._order: list[str] = []
        self.leaders = 0
        self.held = 0
        self.released = 0
        self.timeouts = 0

    def _put(self, fp: str, entry: dict) -> None:
        if fp in self.anchors:
            self._order.remove(fp)
        self.anchors[fp] = entry
        self._order.append(fp)
        while len(self._order) > self.cap:
            self.anchors.pop(self._order.pop(0), None)

    def begin(self, fp: str | None) -> tuple[str, threading.Event | None]:
        """Classify a request: ('leader'|'follower'|'pass', gate)."""
        if not self.enabled or not fp:
            return ("pass", None)
        with self.lock:
            e = self.anchors.get(fp)
            if e is None:
                gate = threading.Event()
                self._put(fp, {"state": "warming", "gate": gate})
                self.leaders += 1
                return ("leader", gate)
            if e["state"] == "warm":
                return ("pass", None)
            self.held += 1
            return ("follower", e["gate"])

    def wait_follower(self, gate: threading.Event) -> None:
        ok = gate.wait(timeout=self.timeout_s)
        with self.lock:
            if ok:
                self.released += 1
            else:
                self.timeouts += 1
        if ok and self.settle_ms:
            time.sleep(self.settle_ms / 1000.0)

    def release(self, gate: threading.Event | None) -> None:
        """Leader got its first upstream byte — let the followers go."""
        if gate is not None:
            gate.set()

    def warm(self, fp: str | None, gate: threading.Event | None) -> None:
        """Leader finished a usable response — mark the anchor warm for good."""
        if fp:
            with self.lock:
                e = self.anchors.get(fp)
                if e is not None:
                    e["state"] = "warm"
        if gate is not None:
            gate.set()

    def fail(self, fp: str | None, gate: threading.Event | None) -> None:
        """Leader never reached a usable response — drop the anchor so the next
        request becomes a fresh leader, and release any waiters (they go cold,
        no worse than baseline)."""
        if fp:
            with self.lock:
                self.anchors.pop(fp, None)
                if fp in self._order:
                    self._order.remove(fp)
        if gate is not None:
            gate.set()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "enabled": self.enabled,
                "leaders": self.leaders,
                "followers_held": self.held,
                "followers_released": self.released,
                "timeouts": self.timeouts,
                "tracked_anchors": len(self.anchors),
            }


COALESCER = Coalescer(enabled=COALESCE, timeout_s=COALESCE_TIMEOUT_S,
                      settle_ms=COALESCE_SETTLE_MS, cap=COALESCE_CAP)

KEEPALIVE_S = float(os.environ.get("PERMAFROST_KEEPALIVE_S", "0") or 0)
KEEPALIVE_IDLE_STOP_S = float(os.environ.get("PERMAFROST_KEEPALIVE_IDLE_STOP_S", "7200"))


class Keepalive:
    """Keep the cache warm through idle gaps; support cross-session pre-warm.

    DeepSeek evicts cache entries that go unused (TTL undocumented — hours-ish
    under load). A long think-time gap mid-session can leave the next turn
    paying full miss price on the whole prefix. When enabled (interval > 0),
    this replays the most recent aligned request with `max_tokens: 1` whenever
    the proxy has been idle for one interval — the entire prefix (anchor +
    conversation) is re-read at hit price (~2% of miss), keeping it resident.

    OPT-IN: it fires real billable requests autonomously, so the default is off.
    It stops after `idle_stop_s` without real traffic (abandoned session guard).

    Note on cross-session pre-warm: we measured it and it does NOT work on
    DeepSeek — an anchor-only replay (tools+system + placeholder message) is
    shorter than any persisted cache prefix unit, so it can never "fully match"
    one; it returned 0 cache-read on the live API. Hence only same-conversation
    keepalive is offered, and only the full unchanged body is replayed.
    """

    _SLOT_CAP = 8  # parallel conversations kept warm at once (LRU)

    def __init__(self, interval_s: float, idle_stop_s: float, sender=None) -> None:
        self.interval_s = interval_s
        self.idle_stop_s = idle_stop_s
        self.sender = sender or self._http_send  # injectable for tests
        self.lock = threading.Lock()
        # One slot per conversation (keyed by CC session id), so parallel
        # sessions through one proxy all stay warm, not just the most recent.
        self.slots: dict[str, dict] = {}
        self.fires = 0
        self.hit = 0
        self.miss = 0
        self.errors = 0

    # -- recording real traffic ------------------------------------------------
    def note_request(self, body: dict, headers: dict[str, str], anchor: str,
                     session: str | None = None) -> None:
        # Keep the client's FULL header set (minus hop-by-hop). Measured live:
        # replaying an identical body with only the auth headers misses the
        # original's cache — DeepSeek's cache identity includes the client's
        # header fingerprint, so the replay must look exactly like the client.
        kept = {k: v for k, v in headers.items()
                if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"}
        key = session or "(none)"
        with self.lock:
            self.slots.pop(key, None)  # re-insert = move to MRU position
            self.slots[key] = {"body": body, "headers": kept,
                               "last_real": time.time(), "last_fire": 0.0,
                               "anchor": anchor}
            while len(self.slots) > self._SLOT_CAP:
                self.slots.pop(next(iter(self.slots)))

    # -- firing ---------------------------------------------------------------
    def due(self, now: float) -> list[str]:
        """Slots whose conversation has idled one interval (but isn't abandoned)."""
        if self.interval_s <= 0:
            return []
        out = []
        with self.lock:
            for key, s in self.slots.items():
                idle = now - s["last_real"]
                if (self.interval_s <= idle <= self.idle_stop_s
                        and now - s["last_fire"] >= self.interval_s):
                    out.append(key)
        return out

    def fire(self, session: str | None = None) -> dict | None:
        """Replay one slot's request UNCHANGED (most recent slot by default).

        Measured on the live endpoint: a replay that differs in stream /
        max_tokens / headers misses the original's cache entirely (params and
        the client header fingerprint are part of DeepSeek's cache identity);
        a byte-identical replay hits 99.8%+. The cost of an unchanged replay is
        one regenerated reply at hit-price input.
        """
        with self.lock:
            if not self.slots:
                return None
            key = session if session in self.slots else next(reversed(self.slots))
            slot = self.slots[key]
            body = dict(slot["body"])
            headers = dict(slot["headers"])
            slot["last_fire"] = time.time()
        try:
            usage = self.sender(body, headers)
        except Exception as e:  # noqa: BLE001 — a failed keepalive must never crash
            with self.lock:
                self.errors += 1
            if os.environ.get("PERMAFROST_VERBOSE") == "1":
                sys.stderr.write(f"permafrost: keepalive failed: {e}\n")
            return None
        if usage:
            with self.lock:
                self.fires += 1
                self.hit += usage["hit"]
                self.miss += usage["miss"]
        return usage

    @staticmethod
    def _http_send(body: dict, headers: dict[str, str]) -> dict | None:
        data = pa.canonical_dumps(body)
        h = dict(headers)
        h["content-type"] = "application/json"
        req = urllib.request.Request(UPSTREAM + "/v1/messages", data=data,
                                     headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode("utf-8", "replace")
        return _sniff_usage(text)  # handles both plain JSON and SSE bodies

    def loop(self) -> None:
        tick = max(0.5, min(30.0, self.interval_s / 4)) if self.interval_s > 0 else 30.0
        while True:
            time.sleep(tick)
            for key in self.due(time.time()):
                self.fire(key)

    def snapshot(self) -> dict:
        with self.lock:
            prices = _prices()
            newest = next(reversed(self.slots)) if self.slots else None
            idle = (round(time.time() - self.slots[newest]["last_real"], 1)
                    if newest else None)
            return {
                "enabled": self.interval_s > 0,
                "interval_s": self.interval_s,
                "slots": len(self.slots),
                "fires": self.fires,
                "errors": self.errors,
                "hit_tokens": self.hit,
                "miss_tokens": self.miss,
                "cost_usd": round(pa.cost_usd(self.hit, self.miss, self.fires, prices), 6),
                "idle_s": idle,
            }


KEEPALIVE = Keepalive(KEEPALIVE_S, KEEPALIVE_IDLE_STOP_S)


class Stats:
    """Thread-safe session + rolling cache accounting."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.time()
        self.requests = 0
        self.hit = 0
        self.miss = 0
        self.output = 0
        self.last_report: dict | None = None
        # Churn is tracked per LINEAGE (stable system+tools ancestry): an anchor
        # change within one lineage is a real cache bust; switching between
        # request types (preflight vs agent loop) is not.
        self.lineages: dict[str, dict] = {}
        self.prefix_changes: list[dict] = []
        # Usage is also bucketed per SESSION (CC's metadata session id) so
        # multiple sessions through one proxy stay readable.
        self.sessions: dict[str, dict] = {}
        self.recent: list[dict] = []

    _LINEAGE_CAP = 64
    _EXCERPT = 70  # bytes of context shown on each side of a divergence

    def record_request(self, report: pa.AlignReport, session: str | None,
                       anchor_payload: bytes | None = None) -> None:
        with self.lock:
            self.requests += 1
            self.last_report = report.as_dict()
            anchor = report.anchor_fingerprint
            lin = self.lineages.get(report.lineage)
            if lin is None:
                if len(self.lineages) >= self._LINEAGE_CAP:
                    self.lineages.pop(next(iter(self.lineages)))
                lin = self.lineages[report.lineage] = {
                    "prev_anchor": None, "transitions": 0, "requests": 0,
                    "last_payload": None}
            lin["requests"] += 1
            if lin["prev_anchor"] is not None and anchor != lin["prev_anchor"]:
                lin["transitions"] += 1
                change = {
                    "lineage": report.lineage,
                    "from": lin["prev_anchor"],
                    "to": anchor,
                    "request": self.requests,
                    "volatile_found": report.volatile_found,
                }
                # Self-debugging: show exactly where the anchor bytes diverged,
                # so a future Claude Code release that introduces a new volatile
                # pattern is diagnosed from /permafrost/doctor, not from a
                # mysteriously sinking hit rate.
                old = lin.get("last_payload")
                if old is not None and anchor_payload is not None:
                    n = min(len(old), len(anchor_payload))
                    i = 0
                    while i < n and old[i] == anchor_payload[i]:
                        i += 1
                    lo = max(0, i - self._EXCERPT)
                    change["diverged_at_byte"] = i
                    change["was"] = old[lo:i + self._EXCERPT].decode("utf-8", "replace")
                    change["now"] = anchor_payload[lo:i + self._EXCERPT].decode("utf-8", "replace")
                self.prefix_changes.append(change)
                self.prefix_changes = self.prefix_changes[-20:]
            lin["prev_anchor"] = anchor
            if anchor_payload is not None:
                lin["last_payload"] = anchor_payload
            s = self.sessions.setdefault(session or "(none)", {
                "requests": 0, "hit": 0, "miss": 0, "output": 0})
            s["requests"] += 1
            s["last_seen"] = round(time.time() - self.started, 1)

    def record_usage(self, u: dict[str, int], session: str | None = None) -> None:
        with self.lock:
            self.hit += u["hit"]
            self.miss += u["miss"]
            self.output += u["output"]
            if session and session in self.sessions:
                s = self.sessions[session]
                s["hit"] += u["hit"]
                s["miss"] += u["miss"]
                s["output"] += u["output"]
            self.recent.append({"hit": u["hit"], "miss": u["miss"], "output": u["output"]})
            self.recent = self.recent[-50:]

    def snapshot(self) -> dict:
        with self.lock:
            prices = _prices()
            cost = pa.cost_usd(self.hit, self.miss, self.output, prices)
            baseline = pa.cost_usd(0, self.hit + self.miss, self.output, prices)
            return {
                "uptime_s": round(time.time() - self.started, 1),
                "requests": self.requests,
                "mode": MODE,
                "upstream": UPSTREAM,
                "cache_hit_tokens": self.hit,
                "cache_miss_tokens": self.miss,
                "output_tokens": self.output,
                "hit_rate": round(pa.hit_rate(self.hit, self.miss), 4),
                "cost_usd": round(cost, 6),
                "cost_usd_if_all_miss": round(baseline, 6),
                "saved_usd": round(baseline - cost, 6),
                "saved_pct": round((1 - cost / baseline) * 100, 1) if baseline else 0.0,
                "prefix_changes": len(self.prefix_changes),
                "sessions": {
                    k: dict(v, hit_rate=round(pa.hit_rate(v["hit"], v["miss"]), 3))
                    for k, v in self.sessions.items()
                },
                "lineages": {
                    k: {kk: vv for kk, vv in v.items() if kk != "last_payload"}
                    for k, v in self.lineages.items()
                },
            }


STATS = Stats()


def _sniff_usage(buf: str) -> dict[str, int] | None:
    """Best-effort extraction of a `usage` object from a (possibly SSE) body."""
    try:
        obj = json.loads(buf)
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            return pa.normalize_usage(obj["usage"])
    except (ValueError, TypeError):
        pass
    merged: dict[str, int] = {"input": 0, "hit": 0, "miss": 0, "output": 0}
    seen = False
    for line in buf.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except (ValueError, TypeError):
            continue
        usage = None
        if isinstance(evt.get("usage"), dict):
            usage = evt["usage"]
        elif isinstance(evt.get("message"), dict) and isinstance(evt["message"].get("usage"), dict):
            usage = evt["message"]["usage"]
        if usage:
            seen = True
            u = pa.normalize_usage(usage)
            merged["hit"] = max(merged["hit"], u["hit"])
            merged["miss"] = max(merged["miss"], u["miss"])
            merged["output"] = max(merged["output"], u["output"])
    merged["input"] = merged["hit"] + merged["miss"]
    return merged if seen else None


def _merge_usage(a: dict | None, b: dict | None) -> dict | None:
    """Combine usage sniffed from the head and tail of a response (max per field).

    Non-streaming responses put everything in one JSON object (caught in the
    head). Streaming responses split it: `message_start` (hit/miss) lands in the
    head, the final `message_delta` (output) lands in the tail.
    """
    if not a and not b:
        return None
    a = a or {"hit": 0, "miss": 0, "output": 0}
    b = b or {"hit": 0, "miss": 0, "output": 0}
    out = {k: max(a.get(k, 0), b.get(k, 0)) for k in ("hit", "miss", "output")}
    out["input"] = out["hit"] + out["miss"]
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Permafrost/0.1"

    def log_message(self, *args) -> None:
        if os.environ.get("PERMAFROST_VERBOSE") == "1":
            super().log_message(*args)

    def _is_loopback_client(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    # --- local introspection + GET passthrough ------------------------------
    def do_GET(self) -> None:
        clean = urllib.parse.urlsplit(self.path).path
        if clean.startswith("/permafrost/") and not self._is_loopback_client():
            # Control endpoints stay local even if someone binds the proxy wide:
            # /warm spends the user's money, /stats and /doctor leak metadata.
            return self._json(403, {"error": "permafrost control endpoints are loopback-only"})
        if clean == "/permafrost/health":
            return self._json(200, {"ok": True, "mode": MODE, "upstream": UPSTREAM,
                                    "host": HOST, "port": PORT})
        if clean == "/permafrost/stats":
            snap = STATS.snapshot()
            snap["coalesce"] = COALESCER.snapshot()
            snap["keepalive"] = KEEPALIVE.snapshot()
            return self._json(200, snap)
        if clean == "/permafrost/doctor":
            with STATS.lock:
                return self._json(200, {
                    "mode": MODE,
                    "last_request": STATS.last_report,
                    "prefix_changes": STATS.prefix_changes,
                    "coalesce": COALESCER.snapshot(),
                    "advice": self._doctor_advice(),
                })
        if clean.startswith("/permafrost/"):
            return self._json(404, {"error": "unknown permafrost endpoint",
                                    "try": ["/permafrost/stats", "/permafrost/doctor",
                                            "/permafrost/health"]})
        # Anything else (e.g. GET /v1/models) is a real API call — forward it.
        return self._forward("GET", None, None)

    def _doctor_advice(self) -> list[str]:
        advice: list[str] = []
        rep = STATS.last_report or {}
        vf = rep.get("volatile_found") or {}
        if vf and MODE != "aggressive":
            advice.append(
                f"Volatile tokens in the system prefix ({vf}); run in aggressive "
                "mode (PERMAFROST_MODE=aggressive) to relocate them."
            )
        churned = {k: v["transitions"] for k, v in STATS.lineages.items() if v["transitions"]}
        if churned:
            advice.append(
                f"The cache anchor changed within {len(churned)} request lineage(s): "
                f"{churned}. A change *within* a lineage is a real cache bust — "
                "every cached prefix token re-reads at full price."
            )
        c = COALESCER.snapshot()
        if c["enabled"] and c["followers_held"]:
            advice.append(
                f"Coalescing held {c['followers_held']} parallel same-anchor request(s) "
                f"({c['timeouts']} timed out) so they could read a warm prefix instead "
                "of all paying the cold-miss price.")
        if not advice:
            advice.append("Prefix anchor is stable. DeepSeek's cache is doing its job.")
        return advice

    # --- the proxy path ------------------------------------------------------
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""

        clean_path = urllib.parse.urlsplit(self.path).path
        if clean_path.startswith("/permafrost/") and not self._is_loopback_client():
            return self._json(403, {"error": "permafrost control endpoints are loopback-only"})
        if clean_path == "/permafrost/warm":
            usage = KEEPALIVE.fire()
            if usage is None:
                return self._json(409, {"warmed": False,
                                        "reason": "no request seen yet this proxy lifetime; "
                                                  "use `permafrost warm` (disk replay)"})
            return self._json(200, {"warmed": True, "usage": usage})

        out_bytes = raw
        report = None
        session = None
        if _is_messages_path(self.path) and raw:
            try:
                body = json.loads(raw)
                session = pa.extract_session(body)
                body, report = pa.align_request(body, MODE, store=FREEZE_STORE)
                out_bytes = pa.canonical_dumps(body)
                STATS.record_request(report, session, pa.anchor_payload(body))
                KEEPALIVE.note_request(body, dict(self.headers),
                                       report.anchor_fingerprint, session)
                if DUMP_DIR:
                    self._dump_anchor(body, report)
            except (ValueError, TypeError) as e:
                out_bytes = raw  # never let alignment break a request
                report = None
                if os.environ.get("PERMAFROST_VERBOSE") == "1":
                    sys.stderr.write(f"permafrost: align skipped: {e}\n")

        fp = report.anchor_fingerprint if report else None
        role, gate = COALESCER.begin(fp)
        if role == "follower":
            COALESCER.wait_follower(gate)
            self._forward("POST", out_bytes, report, session=session)
        elif role == "leader":
            # completion policy: followers stay parked until warm()/fail() below,
            # i.e. after the leader's response fully streamed.
            cb = (lambda: COALESCER.release(gate)) if COALESCE_RELEASE == "first_byte" else None
            status = self._forward("POST", out_bytes, report, session=session,
                                   first_byte_cb=cb)
            if status is not None and status < 400:
                COALESCER.warm(fp, gate)
            else:
                COALESCER.fail(fp, gate)  # no usable cache write; let next retry
        else:  # pass / disabled
            self._forward("POST", out_bytes, report, session=session)

    def _dump_anchor(self, body: dict, report) -> None:
        """Debug: persist each request's cache anchor (tools + system) so a diff
        can reveal exactly what real Claude Code varies between turns."""
        try:
            os.makedirs(DUMP_DIR, exist_ok=True)
            n = STATS.requests
            path = os.path.join(DUMP_DIR, f"req-{n:03d}-{report.anchor_fingerprint}.json")
            # Dump the FULL forwarded body (canonical bytes) so a diff can show
            # key order and exactly where consecutive requests diverge.
            with open(path, "wb") as f:
                f.write(pa.canonical_dumps(body))
        except Exception as e:
            if os.environ.get("PERMAFROST_VERBOSE") == "1":
                sys.stderr.write(f"permafrost: dump failed: {e}\n")

    def _build_upstream_headers(self) -> dict[str, str]:
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in _HOP_BY_HOP:
                continue
            if NORMALIZE_BETA and k.lower() == "anthropic-beta" and v:
                v = _normalize_beta(v)
            headers[k] = v
        return headers

    def _forward(self, method: str, out_bytes: bytes | None, report,
                 session: str | None = None, first_byte_cb=None) -> int | None:
        # self.path already carries /v1/... plus any query string; UPSTREAM ends
        # at /anthropic, so the upstream path is <prefix>/v1/messages[?query].
        # Returns the upstream HTTP status, or None if the upstream was
        # unreachable (so a coalescing leader knows whether a cache write happened).
        prefix = _UP.path.rstrip("/")
        path = prefix + (self.path if self.path.startswith("/") else "/" + self.path)
        headers = self._build_upstream_headers()

        resp = None
        for attempt in (0, 1):  # retry once, but ONLY for a stale reused conn
            conn, reused = _upstream_conn(fresh=attempt > 0)
            try:
                conn.request(method, path, body=out_bytes, headers=headers)
                resp = conn.getresponse()
                break
            except (http.client.HTTPException, BrokenPipeError, ConnectionResetError,
                    ssl.SSLError, OSError) as e:
                _drop_upstream_conn()
                # Retry only when a *reused* keep-alive connection failed: the
                # server closed it while idle, so our request never landed and a
                # resend is safe. A fresh connection that failed is a genuine
                # error — resending a POST could double-charge / double-execute.
                if attempt or not reused:
                    self._json(502, {"error": "upstream unreachable", "detail": str(e),
                                     "upstream": UPSTREAM})
                    return None

        status = resp.status or 200
        self.send_response(status)
        for k, v in resp.getheaders():
            if k.lower() in _HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        head = bytearray()
        tail = bytearray()
        first = True
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                if first:
                    first = False
                    if first_byte_cb is not None:
                        try:
                            first_byte_cb()  # release coalesced followers
                        except Exception:
                            pass
                if len(head) < _SNIFF_HEAD:
                    head.extend(chunk[: _SNIFF_HEAD - len(head)])
                tail.extend(chunk)
                if len(tail) > _SNIFF_TAIL:
                    del tail[:-_SNIFF_TAIL]
                self._write_chunk(chunk)
            self._write_chunk(b"")
        except (BrokenPipeError, ConnectionResetError):
            # Client went away mid-stream: the pooled connection still has
            # unread upstream data — drop it so the next request gets a clean one.
            _drop_upstream_conn()
            return status
        except (http.client.HTTPException, ssl.SSLError, OSError):
            _drop_upstream_conn()
            return status

        if report is not None:  # only meter aligned /v1/messages calls
            u_head = _sniff_usage(bytes(head).decode("utf-8", "replace"))
            u_tail = _sniff_usage(bytes(tail).decode("utf-8", "replace"))
            u = _merge_usage(u_head, u_tail)
            if u:
                STATS.record_usage(u, session)
        return status

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(b"%X\r\n" % len(data))
        if data:
            self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _json(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> None:
    if MODE not in ("off", "safe", "aggressive"):
        sys.stderr.write(f"permafrost: unknown PERMAFROST_MODE={MODE!r}; using aggressive\n")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    if KEEPALIVE.interval_s > 0:
        threading.Thread(target=KEEPALIVE.loop, daemon=True).start()
        sys.stderr.write(f"  keepalive   every {KEEPALIVE.interval_s:.0f}s while idle "
                         f"(stops after {KEEPALIVE.idle_stop_s:.0f}s without real traffic)\n")
    banner = (
        f"\n  permafrost {Handler.server_version.split('/')[1]}  mode={MODE}\n"
        f"  listening   http://{HOST}:{PORT}\n"
        f"  upstream    {UPSTREAM}\n"
        f"  point Claude Code here:\n"
        f"    ANTHROPIC_BASE_URL=http://{HOST}:{PORT} ENABLE_TOOL_SEARCH=true claude\n"
        f"  stats       curl http://{HOST}:{PORT}/permafrost/stats\n"
    )
    sys.stderr.write(banner)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\npermafrost: shutting down\n")
        httpd.shutdown()


if __name__ == "__main__":
    main()
