#!/usr/bin/env bash
# Isolated concurrency test for cold-anchor coalescing.
# Fires N parallel identical /v1/messages requests at the proxy while the mock
# upstream holds the leader for a moment, then asserts the proxy elected exactly
# ONE leader and held the other N-1 as followers. Local-only, torn down after.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MOCK_PORT=8992
PROXY_PORT=8993
PY="${PYTHON:-python3}"
N=5

cleanup() {
  [[ -n "${MOCK_PID:-}" ]] && kill "$MOCK_PID" 2>/dev/null || true
  [[ -n "${PROXY_PID:-}" ]] && kill "$PROXY_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "starting mock upstream on :$MOCK_PORT (0.5s leader delay)"
MOCK_DELAY=0.5 "$PY" "$ROOT/tests/mock_upstream.py" "$MOCK_PORT" &
MOCK_PID=$!

echo "starting proxy on :$PROXY_PORT (coalescing on)"
PERMAFROST_PORT="$PROXY_PORT" PERMAFROST_MODE=aggressive PERMAFROST_COALESCE=1 \
  PERMAFROST_UPSTREAM="http://127.0.0.1:$MOCK_PORT" \
  "$PY" "$ROOT/proxy/permafrost_proxy.py" &
PROXY_PID=$!

for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:$PROXY_PORT/permafrost/health" >/dev/null 2>&1 && break
  sleep 0.1
done

REQ='{"model":"claude-sonnet-4-6","system":[{"type":"text","text":"Stable shared prefix for a fan-out of subagents, long enough to matter."}],"tools":[{"name":"Read","description":"r","input_schema":{"type":"object"}}],"messages":[{"role":"user","content":[{"type":"text","text":"go"}]}]}'

echo "firing $N concurrent identical requests (same cold anchor)"
pids=()
for _ in $(seq 1 "$N"); do
  curl -fsS --max-time 15 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
    -H 'content-type: application/json' -H 'x-api-key: test' --data "$REQ" >/dev/null &
  pids+=($!)
done
wait "${pids[@]}"   # only the curls — NOT the mock/proxy started with &

echo "--- coalesce stats ---"
STATS_JSON="$(curl -fsS "http://127.0.0.1:$PROXY_PORT/permafrost/stats")"
N="$N" STATS_JSON="$STATS_JSON" "$PY" <<'PY'
import json, os
c = json.loads(os.environ["STATS_JSON"])["coalesce"]
n = int(os.environ["N"])
print(" ", c)
assert c["leaders"] == 1, "expected 1 leader, got %d" % c["leaders"]
assert c["followers_held"] == n - 1, "expected %d held, got %d" % (n - 1, c["followers_held"])
assert c["timeouts"] == 0, "unexpected timeouts: %d" % c["timeouts"]
print("  coalescing works: 1 leader warmed the cache, %d followers waited then read it"
      % c["followers_held"])
PY
echo "COALESCE SMOKE OK"
