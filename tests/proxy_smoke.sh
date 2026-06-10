#!/usr/bin/env bash
# Isolated end-to-end smoke test for the proxy.
# Spins up a local mock upstream + the proxy on throwaway ports, exercises the
# POST path and the introspection endpoints, then tears everything down.
# Touches no real API and no ~/.claude config.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MOCK_PORT=8990
PROXY_PORT=8991
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

echo "starting proxy on :$PROXY_PORT (mode=aggressive, relocate path, upstream=mock)"
# FREEZE_ENV=0 exercises the stateless relocate path this smoke asserts on;
# freeze+delta is covered by tests/test_alignment.py and the real-CC e2e.
PERMAFROST_PORT="$PROXY_PORT" PERMAFROST_MODE=aggressive PERMAFROST_FREEZE_ENV=0 \
  PERMAFROST_UPSTREAM="http://127.0.0.1:$MOCK_PORT" \
  "$PY" "$ROOT/proxy/permafrost_proxy.py" &
PROXY_PID=$!

# Wait for liveness.
for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$PROXY_PORT/permafrost/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

echo "--- health ---"
curl -fsS "http://127.0.0.1:$PROXY_PORT/permafrost/health"; echo

echo "--- POST /v1/messages?beta=true (P0: query string must NOT bypass alignment) ---"
REQ='{"model":"claude-sonnet-4-6","system":[{"type":"text","text":"Stable instructions here, long enough to matter and then some more text.","cache_control":{"type":"ephemeral"}},{"type":"text","text":"<env>\nToday'"'"'s date: 2026-06-10\ngitStatus: M a.py\n</env>"}],"tools":[{"name":"Zebra","description":"z","input_schema":{"type":"object"}},{"name":"Apple","description":"a","input_schema":{"type":"object"}}],"messages":[{"role":"user","content":[{"type":"text","text":"hello"}]}]}'
curl -fsS -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages?beta=true" \
  -H 'content-type: application/json' -H 'x-api-key: test' \
  -H 'anthropic-beta: feat-b,feat-a,feat-b' --data "$REQ" >/dev/null
echo "ok (streamed)"

echo "--- GET /v1/models (must be forwarded, not 404'd) ---"
curl -fsS "http://127.0.0.1:$PROXY_PORT/v1/models" | grep -q deepseek-v4-flash \
  && echo "  GET passthrough works" || { echo "FAIL: GET not forwarded"; exit 1; }

echo "--- anthropic-beta normalization (sort+dedup, no add/drop) ---"
BETA="$(cat "$REC.beta" 2>/dev/null || true)"
[[ "$BETA" == "feat-a,feat-b" ]] && echo "  beta normalized: '$BETA'" || { echo "FAIL: beta='$BETA' (expected feat-a,feat-b)"; exit 1; }

echo "--- forwarded body assertions ---"
"$PY" - "$REC" <<'PYEOF'
import json, sys
body = json.load(open(sys.argv[1]))
assert "cache_control" not in json.dumps(body), "cache_control should be stripped"
assert [t["name"] for t in body["tools"]] == ["Apple", "Zebra"], "tools should be sorted"
assert "gitStatus" not in json.dumps(body["system"]), "env should be relocated out of system"
assert "gitStatus" in json.dumps(body["messages"]), "env should be re-attached to a turn"
print("  forwarded body is aligned: cache_control stripped, tools sorted, env relocated")
PYEOF

echo "--- stats ---"
curl -fsS "http://127.0.0.1:$PROXY_PORT/permafrost/stats"; echo
HIT=$(curl -fsS "http://127.0.0.1:$PROXY_PORT/permafrost/stats" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["cache_hit_tokens"])')
[[ "$HIT" -ge 1 ]] && echo "  usage sniffing works: cache_hit_tokens=$HIT" || { echo "FAIL: no usage recorded"; exit 1; }

echo "--- doctor ---"
curl -fsS "http://127.0.0.1:$PROXY_PORT/permafrost/doctor"; echo

echo "SMOKE OK"
