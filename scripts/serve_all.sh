#!/usr/bin/env bash
# Start three map app instances side by side for comparison:
#   8000  SuperGrid backend, model flower-endeavor-v1.0 (primary)
#   8001  SuperGrid backend, model openai/gpt-5.6-sol   (fallback)
#   8002  Local backend, rule-based specialists, fixture data (offline)
# Requires: `uv sync` and `uv run flwr login supergrid` completed on this machine.
# Stop them again with scripts/stop_all.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

FEDERATION="${TRAVEL_FLOWER_FEDERATION:-@imranmali/workspace}"
LOG_DIR="${TRAVEL_LOG_DIR:-runtime/logs}"
mkdir -p runtime "$LOG_DIR"

# serve_supergrid.sh always runs backend=flower in control mode; every other TRAVEL_* variable passes through.
(TRAVEL_PORT=8000 TRAVEL_MODEL=flower-endeavor-v1.0 TRAVEL_FLOWER_FEDERATION="$FEDERATION" \
  TRAVEL_DB=runtime/endeavor.sqlite3 nohup ./scripts/serve_supergrid.sh > "$LOG_DIR/server-8000-endeavor.log" 2>&1 &)
(TRAVEL_PORT=8001 TRAVEL_MODEL=openai/gpt-5.6-sol TRAVEL_FLOWER_FEDERATION="$FEDERATION" \
  TRAVEL_DB=runtime/fallback.sqlite3 nohup ./scripts/serve_supergrid.sh > "$LOG_DIR/server-8001-fallback.log" 2>&1 &)
# The offline instance starts the server directly: the local backend with rule-based specialists needs no login.
(TRAVEL_EXECUTION_BACKEND=local TRAVEL_AGENT_MODE=rules TRAVEL_DATA_MODE=fixture TRAVEL_DB=runtime/offline.sqlite3 \
  nohup uv run python -m travel_agent.cli serve --port 8002 > "$LOG_DIR/server-8002-offline.log" 2>&1 &)

for _ in $(seq 1 20); do
  ok=0
  for p in 8000 8001 8002; do
    curl -s -o /dev/null "http://127.0.0.1:$p/health" && ok=$((ok + 1))
  done
  [ "$ok" -eq 3 ] && break
  sleep 1
done

for p in 8000 8001 8002; do
  echo "--- port $p ---"
  curl -s "http://127.0.0.1:$p/api/config" | python3 -c "
import json, sys
c = json.load(sys.stdin)
print('backend:', c['execution_backend'], '| agent mode:', c['agent_mode'], '| data:', c['data_mode'],
      '| model:', c.get('model'), '| federation:', (c.get('flower') or {}).get('federation'))
"
done

echo
echo "Logs: $LOG_DIR/server-8000-endeavor.log, server-8001-fallback.log, server-8002-offline.log"
echo "Open http://127.0.0.1:8000 (endeavor), :8001 (fallback), :8002 (offline)"
