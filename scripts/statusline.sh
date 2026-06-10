#!/usr/bin/env bash
# Permafrost statusline badge: shows the live DeepSeek cache hit rate + savings.
#
# Wire it into ~/.claude/settings.json:
#   { "statusLine": { "type": "command",
#       "command": "/abs/path/to/permafrost/scripts/statusline.sh" } }
#
# Claude Code pipes session JSON on stdin; we ignore it and read the proxy stats.
set -euo pipefail
cat >/dev/null 2>&1 || true   # drain stdin

PORT="${PERMAFROST_PORT:-8787}"
HOST="${PERMAFROST_HOST:-127.0.0.1}"
JSON="$(curl -fsS "http://${HOST}:${PORT}/permafrost/stats" 2>/dev/null || true)"

if [[ -z "$JSON" ]]; then
  echo "❄ permafrost: off"
  exit 0
fi

python3 - "$JSON" <<'PY'
import json, sys
try:
    s = json.loads(sys.argv[1])
except Exception:
    print("❄ permafrost: off"); raise SystemExit
hit = s.get("hit_rate", 0) * 100
saved = s.get("saved_usd", 0)
saved_pct = s.get("saved_pct", 0)
resets = s.get("prefix_changes", 0)
warn = "  ⚠ anchor churn" if resets else ""
print(f"❄ {hit:.0f}% cache hit · ${saved:.3f} saved ({saved_pct:.0f}%){warn}")
PY
