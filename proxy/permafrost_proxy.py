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
  PERMAFROST_NORMALIZE_BETA  1 to sort+dedup the anthropic-beta header (default 1)
  PERMAFROST_PRICES     "hit,miss,output" USD per 1M to override the cost model

Local introspection (GET):
  /permafrost/health    liveness + config
  /permafrost/stats     session + rolling cache stats
  /permafrost/doctor    last request's alignment report + prefix-change history
"""

from __future__ import annotations

import json
import os
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

_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "accept-encoding",
}

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
        self.prev_anchor: str | None = None
        self.prefix_changes: list[dict] = []
        self.recent: list[dict] = []

    def record_request(self, report: pa.AlignReport) -> None:
        with self.lock:
            self.requests += 1
            self.last_report = report.as_dict()
            anchor = report.anchor_fingerprint
            if self.prev_anchor is not None and anchor != self.prev_anchor:
                self.prefix_changes.append({
                    "from": self.prev_anchor,
                    "to": anchor,
                    "request": self.requests,
                    "volatile_found": report.volatile_found,
                    "blocks_relocated": report.blocks_relocated,
                })
                self.prefix_changes = self.prefix_changes[-20:]
            self.prev_anchor = anchor

    def record_usage(self, u: dict[str, int]) -> None:
        with self.lock:
            self.hit += u["hit"]
            self.miss += u["miss"]
            self.output += u["output"]
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

    # --- local introspection + GET passthrough ------------------------------
    def do_GET(self) -> None:
        clean = urllib.parse.urlsplit(self.path).path
        if clean == "/permafrost/health":
            return self._json(200, {"ok": True, "mode": MODE, "upstream": UPSTREAM,
                                    "host": HOST, "port": PORT})
        if clean == "/permafrost/stats":
            return self._json(200, STATS.snapshot())
        if clean == "/permafrost/doctor":
            with STATS.lock:
                return self._json(200, {
                    "mode": MODE,
                    "last_request": STATS.last_report,
                    "prefix_changes": STATS.prefix_changes,
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
        if STATS.prefix_changes:
            advice.append(
                f"The cache anchor changed {len(STATS.prefix_changes)}x this session. "
                "Each change forces DeepSeek to re-read the whole prefix at full price."
            )
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

        out_bytes = raw
        report = None
        if _is_messages_path(self.path) and raw:
            try:
                body = json.loads(raw)
                body, report = pa.align_request(body, MODE)
                out_bytes = pa.canonical_dumps(body)
                STATS.record_request(report)
            except (ValueError, TypeError) as e:
                out_bytes = raw  # never let alignment break a request
                if os.environ.get("PERMAFROST_VERBOSE") == "1":
                    sys.stderr.write(f"permafrost: align skipped: {e}\n")

        self._forward("POST", out_bytes, report)

    def _build_upstream_headers(self) -> dict[str, str]:
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in _HOP_BY_HOP:
                continue
            if NORMALIZE_BETA and k.lower() == "anthropic-beta" and v:
                v = _normalize_beta(v)
            headers[k] = v
        return headers

    def _forward(self, method: str, out_bytes: bytes | None, report) -> None:
        # self.path already carries /v1/... plus any query string; UPSTREAM ends
        # at /anthropic, so the full URL is <upstream>/v1/messages[?query].
        url = UPSTREAM + self.path if self.path.startswith("/") else UPSTREAM + "/" + self.path
        headers = self._build_upstream_headers()
        req = urllib.request.Request(url, data=out_bytes, headers=headers, method=method)

        head = bytearray()
        tail = bytearray()
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            resp = e
        except urllib.error.URLError as e:
            return self._json(502, {"error": "upstream unreachable", "detail": str(e),
                                    "upstream": UPSTREAM})

        status = getattr(resp, "status", 200) or 200
        self.send_response(status)
        for k, v in resp.headers.items():
            if k.lower() in _HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                if len(head) < _SNIFF_HEAD:
                    head.extend(chunk[: _SNIFF_HEAD - len(head)])
                tail.extend(chunk)
                if len(tail) > _SNIFF_TAIL:
                    del tail[:-_SNIFF_TAIL]
                self._write_chunk(chunk)
            self._write_chunk(b"")
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            try:
                resp.close()
            except Exception:
                pass

        if report is not None:  # only meter aligned /v1/messages calls
            u_head = _sniff_usage(bytes(head).decode("utf-8", "replace"))
            u_tail = _sniff_usage(bytes(tail).decode("utf-8", "replace"))
            u = _merge_usage(u_head, u_tail)
            if u:
                STATS.record_usage(u)

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
