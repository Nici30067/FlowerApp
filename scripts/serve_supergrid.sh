#!/usr/bin/env bash
# SuperGrid launcher: the map application with planning executed on Flower SuperGrid (control mode).
# Requires: `uv sync` and `uv run flwr login supergrid` completed on this machine.
# The backend and mode are fixed here; every other TRAVEL_* variable passes through with its documented default.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${TRAVEL_EXECUTION_BACKEND:-flower}" != flower ] || [ "${TRAVEL_FLOWER_MODE:-control}" != control ]; then
  printf 'note: this launcher always runs TRAVEL_EXECUTION_BACKEND=flower TRAVEL_FLOWER_MODE=control (ignoring %s/%s)\n' \
    "${TRAVEL_EXECUTION_BACKEND:-flower}" "${TRAVEL_FLOWER_MODE:-control}" >&2
fi
export TRAVEL_EXECUTION_BACKEND=flower
export TRAVEL_FLOWER_MODE=control
export TRAVEL_DATA_MODE="${TRAVEL_DATA_MODE:-fixture}"
export TRAVEL_FLOWER_CONNECTION="${TRAVEL_FLOWER_CONNECTION:-supergrid}"
export TRAVEL_FLOWER_FEDERATION="${TRAVEL_FLOWER_FEDERATION:-}"
export TRAVEL_MODEL="${TRAVEL_MODEL:-flower-endeavor-v1.0}"
export TRAVEL_MAX_TOOL_TURNS="${TRAVEL_MAX_TOOL_TURNS:-0}"
# An explicitly empty reasoning effort omits the field, so only an unset variable falls back to "low".
export TRAVEL_REASONING_EFFORT="${TRAVEL_REASONING_EFFORT-low}"
export TRAVEL_MODEL_TIMEOUT_S="${TRAVEL_MODEL_TIMEOUT_S:-120}"
export TRAVEL_MAX_MODEL_CALLS="${TRAVEL_MAX_MODEL_CALLS:-0}"
export TRAVEL_WALL_TIME_S="${TRAVEL_WALL_TIME_S:-900}"
export TRAVEL_MAX_OUTPUT_TOKENS="${TRAVEL_MAX_OUTPUT_TOKENS:-2000}"
export TRAVEL_DB="${TRAVEL_DB:-runtime/travel-supergrid.sqlite3}"
export TRAVEL_PORT="${TRAVEL_PORT:-8000}"
cap="$TRAVEL_MAX_MODEL_CALLS"
if [ "$cap" = 0 ] && [[ "$TRAVEL_MAX_TOOL_TURNS" =~ ^[0-9]+$ ]]; then
  cap="auto ($((5 * (TRAVEL_MAX_TOOL_TURNS + 2))))"
fi
printf '\nOpen http://127.0.0.1:%s\n' "$TRAVEL_PORT"
printf '  backend=flower mode=control connection=%s federation=%s\n' \
  "$TRAVEL_FLOWER_CONNECTION" "${TRAVEL_FLOWER_FEDERATION:-account default}"
printf '  model=%s tool-turns=%s reasoning=%s model-calls=%s wall-time=%ss max-output-tokens=%s\n\n' \
  "$TRAVEL_MODEL" "$TRAVEL_MAX_TOOL_TURNS" "${TRAVEL_REASONING_EFFORT:-omitted}" "$cap" "$TRAVEL_WALL_TIME_S" \
  "$TRAVEL_MAX_OUTPUT_TOKENS"
exec uv run python -m travel_agent.cli serve --port "$TRAVEL_PORT"
