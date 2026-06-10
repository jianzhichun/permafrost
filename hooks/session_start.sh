#!/usr/bin/env bash
# SessionStart hook: ensure the Permafrost proxy is running — but only when this
# session is actually routed through it. We never start a background server for
# users who installed the plugin but aren't pointing Claude Code at the proxy.
#
# Opt-in is automatic: if ANTHROPIC_BASE_URL points at the Permafrost port (or
# PERMAFROST_AUTOSTART=1 is set), we start the proxy in the background. Otherwise
# this is a silent no-op.
set -euo pipefail

PORT="${PERMAFROST_PORT:-8787}"
HOST="${PERMAFROST_HOST:-127.0.0.1}"
BASE="http://${HOST}:${PORT}"

routed=0
case "${ANTHROPIC_BASE_URL:-}" in
  *"${HOST}:${PORT}"*) routed=1 ;;
esac
[[ "${PERMAFROST_AUTOSTART:-0}" == "1" ]] && routed=1

[[ "$routed" == "1" ]] || exit 0

# Already up?
if curl -fsS "${BASE}/permafrost/health" >/dev/null 2>&1; then
  exit 0
fi

# Start it detached and return immediately so we don't delay the session.
LOGDIR="${PERMAFROST_HOME:-$HOME/.permafrost}"
mkdir -p "$LOGDIR"
nohup python3 "${CLAUDE_PLUGIN_ROOT}/proxy/permafrost_proxy.py" \
  >>"$LOGDIR/proxy.log" 2>&1 &
echo $! > "$LOGDIR/proxy.pid"

echo "permafrost: started cache proxy at ${BASE} (mode=${PERMAFROST_MODE:-aggressive})" >&2
exit 0
