#!/usr/bin/env bash
# Real end-to-end test: run actual Claude Code (headless) through Permafrost,
# pointed at DeepSeek, on a small multi-turn agentic task, and report the cache
# hit rate the proxy observed. This is the authoritative validation — genuine CC
# traffic, real DeepSeek cache, real tokens.
#
# Fully isolated: a throwaway CLAUDE_CONFIG_DIR and project dir, a dedicated proxy
# port. It never touches your ~/.claude or your running session.
#
# Requires: a funded DEEPSEEK_API_KEY in the environment, and `claude` on PATH.
#   DEEPSEEK_API_KEY=sk-... ./e2e/run_claude_code.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${DEEPSEEK_API_KEY:?set DEEPSEEK_API_KEY (a funded DeepSeek key)}"
PORT="${PERMAFROST_PORT:-8998}"
MODEL="${MODEL:-claude-sonnet-4-6}"
WORK="$(mktemp -d /tmp/permafrost-e2e.XXXXXX)"
CFG="$WORK/cfg"; PROJ="$WORK/proj"; PFHOME="$WORK/pf"
mkdir -p "$CFG" "$PROJ" "$PFHOME"

cleanup() {
  PERMAFROST_HOME="$PFHOME" PERMAFROST_PORT="$PORT" "$ROOT/cli/permafrost" down >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# A tiny project with a bug for CC to find and fix (drives several tool turns).
cat > "$PROJ/calc.py" <<'EOF'
def add(a, b):
    return a + b

def divide(a, b):
    return a / b   # TODO: handle division by zero
EOF
echo "# calc" > "$PROJ/README.md"
( cd "$PROJ" && git init -q && git add -A && git -c user.email=e@e.com -c user.name=e commit -qm init )

echo "starting Permafrost on :$PORT -> DeepSeek (mode=aggressive, freeze on)"
PERMAFROST_HOME="$PFHOME" PERMAFROST_PORT="$PORT" PERMAFROST_MODE=aggressive \
  PERMAFROST_FREEZE_ENV=1 "$ROOT/cli/permafrost" up >/dev/null

echo "running real Claude Code (headless) on a fix-the-bug task..."
( cd "$PROJ" && timeout 240 env \
    CLAUDE_CONFIG_DIR="$CFG" \
    ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" \
    ANTHROPIC_API_KEY="$DEEPSEEK_API_KEY" \
    ENABLE_TOOL_SEARCH=true \
    claude -p "Read calc.py, then fix the divide-by-zero bug in divide() and add a docstring to add() using the Edit tool. Then say what changed." \
    --model "$MODEL" --permission-mode bypassPermissions --output-format json \
    2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  CC turns:",d.get("num_turns"),"| result:",(d.get("result") or "")[:80])' )

echo ""
echo "=== Permafrost saw (real DeepSeek cache) ==="
curl -fsS "http://127.0.0.1:$PORT/permafrost/stats" > "$WORK/stats.json"
python3 - "$WORK/stats.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
print(f"  requests        {s['requests']}")
print(f"  cache hit rate  {s['hit_rate']*100:.0f}%   ({s['cache_hit_tokens']:,} hit / {s['cache_miss_tokens']:,} miss tokens)")
print(f"  cost            ${s['cost_usd']:.5f}  vs ${s['cost_usd_if_all_miss']:.5f} all-miss  ->  {s['saved_pct']:.0f}% saved")
print(f"  anchor resets   {s['prefix_changes']}")
PY
echo "OK"
