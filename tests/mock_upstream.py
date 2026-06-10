#!/usr/bin/env python3
"""A tiny mock of DeepSeek's Anthropic endpoint for isolated proxy tests.

POST /v1/messages -> an SSE stream carrying a `usage` block with cache fields,
so the proxy's usage sniffer has something to parse. It also records the exact
request body it received to $MOCK_RECORD (if set) so a test can assert the
proxy forwarded aligned bytes. Pure stdlib, localhost only.
"""

from __future__ import annotations

import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RECORD = os.environ.get("MOCK_RECORD")
DELAY = float(os.environ.get("MOCK_DELAY", "0") or 0)  # secs before responding

SSE = (
    'data: {"type":"message_start","message":{"usage":{"input_tokens":120,'
    '"cache_read_input_tokens":880,"output_tokens":1}}}\n\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
    'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
    "data: [DONE]\n\n"
)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        if RECORD:
            with open(RECORD, "wb") as f:
                f.write(body)
        # Record the anthropic-beta header too, so a test can assert normalization.
        if RECORD:
            with open(RECORD + ".beta", "w") as f:
                f.write(self.headers.get("anthropic-beta", ""))
        if DELAY:
            time.sleep(DELAY)  # hold the leader so followers queue up
        payload = SSE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # Stand in for a real GET endpoint (e.g. /v1/models) so the proxy's
        # GET-passthrough path can be exercised.
        payload = b'{"data":[{"id":"deepseek-v4-flash"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8990
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
