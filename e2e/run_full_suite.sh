#!/usr/bin/env bash
# The FULL end-to-end suite: every Permafrost feature exercised against real
# Claude Code and the real DeepSeek API, with hard assertions per phase.
#
#   P1  alignment & freeze     multi-turn CC task: hit rate, frozen anchor, env freeze
#   P2  coalescing             (a) real CC parallel-subagent fan-out  (b) deterministic
#                              3-burst on a cold anchor with default settle
#   P3  keepalive + resume     idle past the interval -> keepalive fires -> resumed
#                              session reads the warm cache
#   P4  warm endpoint          unchanged replay hits >=90%
#   P5  sessions & lineages    >=2 session buckets; zero within-lineage churn
#   P6  doctor anchor-diff     a forced anchor change yields a byte-level was/now diff
#
# (Byte-level request handling — query-string paths, GET passthrough, beta
# normalization — is covered by the offline smokes in tests/; this suite covers
# live cache behavior.)
#
# Isolated: throwaway CLAUDE_CONFIG_DIR/project/proxy ports; never touches
# ~/.claude. Requires a funded DEEPSEEK_API_KEY and `claude` on PATH.
# Cost: a few cents. Runtime: ~4 minutes (P3 idles on purpose).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${DEEPSEEK_API_KEY:?set DEEPSEEK_API_KEY (a funded DeepSeek key)}"
PORT="${PERMAFROST_E2E_PORT:-8986}"
DIFF_PORT=$((PORT + 1))
MODEL="${MODEL:-claude-sonnet-4-6}"
KA_S=20
WORK="$(mktemp -d /tmp/permafrost-suite.XXXXXX)"
CFG="$WORK/cfg"; PROJ="$WORK/proj"; PFHOME="$WORK/pf"
mkdir -p "$CFG" "$PROJ" "$PFHOME"
PY="${PYTHON:-python3}"
BASE="http://127.0.0.1:$PORT"

PASS=0; FAIL=0; RESULTS=()
phase() { echo; echo "━━━ $1 ━━━"; }
ok()   { PASS=$((PASS+1)); RESULTS+=("PASS  $1"); echo "  ✔ $1"; }
bad()  { FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1"); echo "  ✘ $1"; }
note() { RESULTS+=("INFO  $1"); echo "  · $1"; }

cleanup() {
  PERMAFROST_HOME="$PFHOME" PERMAFROST_PORT="$PORT" "$ROOT/cli/permafrost" down >/dev/null 2>&1 || true
  PERMAFROST_HOME="$PFHOME/diff" PERMAFROST_PORT="$DIFF_PORT" "$ROOT/cli/permafrost" down >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

stats() { curl -fsS "$BASE/permafrost/stats"; }
jget()  { "$PY" -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1], {'d': d}))" "$1"; }

run_cc() {  # run_cc <prompt> [extra claude args...] -> prints result JSON
  local prompt="$1"; shift
  ( cd "$PROJ" && timeout 240 env \
      CLAUDE_CONFIG_DIR="$CFG" \
      ANTHROPIC_BASE_URL="$BASE" \
      ANTHROPIC_API_KEY="$DEEPSEEK_API_KEY" \
      ENABLE_TOOL_SEARCH=true \
      claude -p "$prompt" --model "$MODEL" --permission-mode bypassPermissions \
      --output-format json "$@" 2>/dev/null )
}

# ─── P0: preflight ───────────────────────────────────────────────────────────
phase "P0 preflight"
command -v claude >/dev/null || { bad "claude not on PATH"; exit 1; }
BAL=$(curl -fsS https://api.deepseek.com/user/balance \
        -H "Authorization: Bearer $DEEPSEEK_API_KEY" | jget "d['is_available']")
[[ "$BAL" == "True" ]] && ok "key funded" || { bad "DeepSeek key has no balance"; exit 1; }

cat > "$PROJ/calc.py" <<'EOF'
def add(a, b):
    return a + b

def divide(a, b):
    return a / b   # TODO: handle division by zero
EOF
echo "# calc" > "$PROJ/README.md"
( cd "$PROJ" && git init -q && git add -A && git -c user.email=e@e.com -c user.name=e commit -qm init )

PERMAFROST_HOME="$PFHOME" PERMAFROST_PORT="$PORT" PERMAFROST_MODE=aggressive \
  PERMAFROST_FREEZE_ENV=1 PERMAFROST_KEEPALIVE_S="$KA_S" \
  "$ROOT/cli/permafrost" up >/dev/null
curl -fsS "$BASE/permafrost/health" >/dev/null && ok "proxy up (aggressive, keepalive ${KA_S}s)" || { bad "proxy up"; exit 1; }

# ─── P1: alignment & freeze on a real multi-turn task ────────────────────────
phase "P1 alignment & freeze (real multi-turn CC task)"
R1=$(run_cc "Read calc.py, then fix the divide-by-zero bug in divide() and add a docstring to add() using the Edit tool. Then say what changed.")
SID1=$(echo "$R1" | jget "d['session_id']")
TURNS=$(echo "$R1" | jget "d.get('num_turns', 0)")
[[ "$TURNS" -ge 3 ]] && ok "CC completed an agentic task (turns=$TURNS)" || bad "task too short (turns=$TURNS)"

S=$(stats)
HR=$(echo "$S" | jget "d['hit_rate']")
echo "$S" | jget "d['hit_rate'] >= 0.5" | grep -q True && ok "hit rate ≥50% (got $(echo "$HR" | cut -c1-5))" || bad "hit rate $HR < 0.5"
DOC=$(curl -fsS "$BASE/permafrost/doctor")
echo "$DOC" | jget "d['last_request']['env_frozen']" | grep -q True && ok "env block frozen into anchor" || bad "env not frozen"
echo "$DOC" | jget "d['last_request']['metadata_stabilized'] >= 1" | grep -q True && ok "cch billing nonce stabilized" || bad "cch not stabilized"

# ─── P2: coalescing ──────────────────────────────────────────────────────────
phase "P2a coalescing — real CC parallel subagent fan-out (observational)"
run_cc "In a SINGLE message, make exactly three parallel Agent tool calls (three subagents at once). Each subagent must read calc.py and report how many functions it defines. After they return, reply with just the number." >/dev/null
C=$(stats | jget "d['coalesce']")
note "real-CC fan-out coalesce stats: $C"

phase "P2b coalescing — deterministic cold 3-burst (default settle)"
BEFORE_HIT=$(stats | jget "d['cache_hit_tokens']"); BEFORE_MISS=$(stats | jget "d['cache_miss_tokens']")
BEFORE_HELD=$(stats | jget "d['coalesce']['followers_held']")
TAG="suite-$(date +%s)" BASE="$BASE" KEY="$DEEPSEEK_API_KEY" "$PY" - <<'PYEOF'
import json, os, threading, urllib.request
KEY, BASE, TAG = os.environ["KEY"], os.environ["BASE"], os.environ["TAG"]
INSTR = (f"Suite cold anchor {TAG}. You are a careful coding agent. " * 130)
def body():
    return {"model": "claude-sonnet-4-6", "max_tokens": 8,
            "system": [{"type": "text", "text": INSTR}],
            "tools": [{"name": "Read", "description": "r", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "go"}]}]}
def fire():
    r = urllib.request.Request(BASE + "/v1/messages", data=json.dumps(body()).encode(),
                               headers={"content-type": "application/json", "x-api-key": KEY,
                                        "anthropic-version": "2023-06-01"})
    urllib.request.urlopen(r, timeout=180).read()
ts = [threading.Thread(target=fire) for _ in range(3)]
[t.start() for t in ts]; [t.join() for t in ts]
PYEOF
AFTER_HIT=$(stats | jget "d['cache_hit_tokens']"); AFTER_MISS=$(stats | jget "d['cache_miss_tokens']")
AFTER_HELD=$(stats | jget "d['coalesce']['followers_held']")
DH=$((AFTER_HIT-BEFORE_HIT)); DM=$((AFTER_MISS-BEFORE_MISS)); HELD=$((AFTER_HELD-BEFORE_HELD))
[[ "$HELD" -ge 2 ]] && ok "burst coalesced ($HELD followers held)" || bad "followers held=$HELD (<2)"
RATE=$(( DH * 100 / (DH + DM + 1) ))
[[ "$RATE" -ge 40 ]] && ok "burst hit rate ${RATE}% with default settle" || bad "burst hit rate ${RATE}% (<40%)"

# ─── P3: keepalive + resume ──────────────────────────────────────────────────
phase "P3 keepalive + resume (idling ${KA_S}s+ so the keepalive fires…)"
sleep $((KA_S + 12))
KA=$(stats | jget "d['keepalive']")
FIRES=$(stats | jget "d['keepalive']['fires']"); KERR=$(stats | jget "d['keepalive']['errors']")
[[ "$FIRES" -ge 1 ]] && ok "keepalive fired (fires=$FIRES)" || bad "keepalive never fired: $KA"
[[ "$KERR" == "0" ]] && ok "keepalive errors=0" || bad "keepalive errors=$KERR"

BH=$(stats | jget "d['cache_hit_tokens']"); BM=$(stats | jget "d['cache_miss_tokens']")
run_cc "Briefly: what did you change in divide()?" --resume "$SID1" >/dev/null
AH=$(stats | jget "d['cache_hit_tokens']"); AM=$(stats | jget "d['cache_miss_tokens']")
RH=$((AH-BH)); RM=$((AM-BM)); RRATE=$(( RH * 100 / (RH + RM + 1) ))
[[ "$RRATE" -ge 40 ]] && ok "resumed session read warm cache (${RRATE}% of resumed tokens hit)" \
                      || bad "resume hit only ${RRATE}%"

# ─── P4: warm endpoint ───────────────────────────────────────────────────────
phase "P4 warm endpoint (unchanged replay)"
curl -fsS -X POST "$BASE/permafrost/warm" >/dev/null   # ensure boundary unit exists
sleep 7
W=$(curl -fsS -X POST "$BASE/permafrost/warm")
WRATE=$(echo "$W" | jget "100*d['usage']['hit']//(d['usage']['hit']+d['usage']['miss']+1)")
[[ "$WRATE" -ge 90 ]] && ok "second warm hit ${WRATE}%" || bad "second warm hit ${WRATE}% (<90%)"

# ─── P5: sessions & lineages ─────────────────────────────────────────────────
phase "P5 per-session buckets & per-lineage churn"
S=$(stats)
NSESS=$(echo "$S" | jget "len(d['sessions'])")
[[ "$NSESS" -ge 2 ]] && ok "$NSESS session buckets tracked" || bad "only $NSESS session bucket(s)"
CHURN=$(echo "$S" | jget "sum(v['transitions'] for v in d['lineages'].values())")
[[ "$CHURN" == "0" ]] && ok "zero within-lineage anchor churn across all phases" || bad "lineage churn=$CHURN"

# ─── P6: doctor anchor-diff (separate off-mode proxy, forced change) ─────────
phase "P6 doctor anchor-diff plumbing (forced anchor change)"
mkdir -p "$PFHOME/diff"
PERMAFROST_HOME="$PFHOME/diff" PERMAFROST_PORT="$DIFF_PORT" PERMAFROST_MODE=off \
  PERMAFROST_COALESCE=0 "$ROOT/cli/permafrost" up >/dev/null
for GIT in "clean" "M src/a.py"; do
  curl -fsS -X POST "http://127.0.0.1:$DIFF_PORT/v1/messages" \
    -H 'content-type: application/json' -H "x-api-key: $DEEPSEEK_API_KEY" \
    -H 'anthropic-version: 2023-06-01' --data "{
      \"model\":\"claude-sonnet-4-6\",\"max_tokens\":4,
      \"system\":[{\"type\":\"text\",\"text\":\"Suite diff probe. Reply ok.\"},
                   {\"type\":\"text\",\"text\":\"<env>\\nToday's date: 2026-06-10\\ngitStatus: $GIT\\n</env>\"}],
      \"messages\":[{\"role\":\"user\",\"content\":\"ok\"}]}" >/dev/null
done
DIFFDOC=$(curl -fsS "http://127.0.0.1:$DIFF_PORT/permafrost/doctor")
echo "$DIFFDOC" | jget "'gitStatus' in (d['prefix_changes'][0].get('now','') + d['prefix_changes'][0].get('was',''))" \
  | grep -q True && ok "anchor-diff shows the changed bytes (was/now excerpts)" || bad "no diff excerpt in doctor"
PERMAFROST_HOME="$PFHOME/diff" PERMAFROST_PORT="$DIFF_PORT" "$ROOT/cli/permafrost" down >/dev/null

# ─── summary ─────────────────────────────────────────────────────────────────
echo; echo "━━━ SUITE SUMMARY ━━━"
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo
FINAL=$(stats)
echo "session totals: $(echo "$FINAL" | jget "f\"hit={d['cache_hit_tokens']:,} miss={d['cache_miss_tokens']:,} rate={d['hit_rate']*100:.0f}% saved=\${d['saved_usd']:.4f} ({d['saved_pct']:.0f}%)\"")"
echo "$PASS passed, $FAIL failed"
[[ "$FAIL" == "0" ]]
