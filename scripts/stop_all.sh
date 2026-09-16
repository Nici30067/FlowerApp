#!/usr/bin/env bash
# Stop all map app instances started by serve_all.sh
set -euo pipefail

echo "Stopping servers on ports 8000, 8001, 8002..."

# Kill processes on ports 8000, 8001, 8002
for port in 8000 8001 8002; do
  if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
    lsof -Pi :$port -sTCP:LISTEN -t | xargs kill -9 2>/dev/null || true
    echo "Stopped server on port $port"
  else
    echo "No server found on port $port"
  fi
done

echo "Done!"
