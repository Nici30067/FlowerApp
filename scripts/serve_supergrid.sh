#!/usr/bin/env bash
# Start the map application with planning executed on Flower SuperGrid (control mode).
# Requires: `uv sync` and `uv run flwr login supergrid` completed on this machine.
set -euo pipefail
cd "$(dirname "$0")/.."
export TRAVEL_EXECUTION_BACKEND="${TRAVEL_EXECUTION_BACKEND:-flower}"
export TRAVEL_FLOWER_MODE="${TRAVEL_FLOWER_MODE:-control}"
export TRAVEL_DATA_MODE="${TRAVEL_DATA_MODE:-fixture}"
export TRAVEL_MODEL="${TRAVEL_MODEL:-flower-endeavor-v1.0}"
export TRAVEL_FLOWER_FEDERATION="${TRAVEL_FLOWER_FEDERATION:-}"
export TRAVEL_MAX_TOOL_TURNS="${TRAVEL_MAX_TOOL_TURNS:-0}"
export TRAVEL_DB="${TRAVEL_DB:-runtime/travel-supergrid.sqlite3}"
export TRAVEL_PORT="${TRAVEL_PORT:-8000}"
printf '\nOpen http://127.0.0.1:%s  (SuperGrid backend, model %s)\n\n' "$TRAVEL_PORT" "$TRAVEL_MODEL"
exec uv run python -m travel_agent.cli serve --port "$TRAVEL_PORT"
