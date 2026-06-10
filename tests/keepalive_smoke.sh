#!/usr/bin/env bash
# Isolated smoke test for idle keepalive: send one real request through the
# proxy (1s keepalive interval), wait ~3s of idle, and assert the proxy fired a
# max_tokens=1 replay at the mock upstream. Local-only, torn down after.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MOCK_PORT=8988
PROXY_PORT=8989
REC="$(mktemp)"
PY="${PYTHON:-python3}"

cleanup() {
  [[ -n "${MOCK_PID:-}" ]] && kill "$MOCK_PID" 2>/dev/null || true
  [[ -n "${PROXY_PID:-}" ]] && kill "$PROXY_PID" 2>/dev/null || true
  rm -f "$REC" "$REC.beta"
}
trap cleanup EXIT

echo "starting mock upstream on :$MOCK_PORT"
MOCK_RECORD="$REC" "$PY" "$ROOT/tests/mock_upstream.py" "$MOCK_PORT" &
MOCK_PID=$!

echo "starting proxy on :$PROXY_PORT (keepalive every 1s)"
PERMAFROST_PORT="$PROXY_PORT" PERMAFROST_MODE=aggressive \
  PERMAFROST_KEEPALIVE_S=1 PERMAFROST_HOME="$(mktemp -d)" \
  PERMAFROST_UPSTREAM="http://127.0.0.1:$MOCK_PORT" \
  "$PY" "$ROOT/proxy/permafrost_proxy.py" &
PROXY_PID=$!

for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:$PROXY_PORT/permafrost/health" >/dev/null 2>&1 && break
  sleep 0.1
done

REQ='{"model":"claude-sonnet-4-6","system":[{"type":"text","text":"Stable prefix for keepalive test."}],"tools":[{"name":"Read","description":"r","input_schema":{"type":"object"}}],"messages":[{"role":"user","content":[{"type":"text","text":"go"}]}],"max_tokens":256}'
echo "sending one real request, then idling 3.5s..."
curl -fsS -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
  -H 'content-type: application/json' -H 'x-api-key: test' --data "$REQ" >/dev/null
sleep 3.5

STATS_JSON="$(curl -fsS "http://127.0.0.1:$PROXY_PORT/permafrost/stats")"
STATS_JSON="$STATS_JSON" REC="$REC" "$PY" <<'PY'
import json, os
s = json.loads(os.environ["STATS_JSON"])
ka = s["keepalive"]
print("  keepalive:", ka)
assert ka["enabled"] is True
assert ka["fires"] >= 1, "expected at least one keepalive fire after 3.5s idle"
assert ka["errors"] == 0, "keepalive fired but errored: %s" % ka
last = json.load(open(os.environ["REC"]))
# The replay must be byte-faithful to the original aligned request (params are
# part of DeepSeek's cache identity) — same max_tokens, no stream override.
assert last.get("max_tokens") == 256, "keepalive replay must not change max_tokens"
assert "stream" not in last, "keepalive replay must not inject a stream flag"
print("  keepalive works: %d unchanged-replay fire(s)" % ka["fires"])
PY
echo "KEEPALIVE SMOKE OK"
