#!/usr/bin/env bash
# Live-data development server: OpenStreetMap places (Overpass), Open-Meteo weather and geocoding, and OSRM routing
# on the public FOSSGIS instance (no key). Specialists run on Google Gemini through the Chat Completions adapter.
# Usage: GEMINI_API_KEY=... TRAVEL_CONTACT="you@example.com" ./scripts/serve_live.sh [port]   (default port 8011)
# Set TRAVEL_ROUTER=ors together with ORS_API_KEY to route through OpenRouteService instead of OSRM.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${GEMINI_API_KEY:?Set GEMINI_API_KEY}"
if [ -z "${TRAVEL_CONTACT:-}" ]; then
  printf 'Set TRAVEL_CONTACT to a public contact (e-mail address or URL); the public OSM services require it.\n' >&2
  exit 1
fi
export TRAVEL_ROUTER="${TRAVEL_ROUTER:-osrm}"
if [ "$TRAVEL_ROUTER" = ors ] && [ -z "${ORS_API_KEY:-}" ]; then
  printf 'TRAVEL_ROUTER=ors requires ORS_API_KEY; unset TRAVEL_ROUTER to use OSRM.\n' >&2
  exit 1
fi
export TRAVEL_EXECUTION_BACKEND=local TRAVEL_AGENT_MODE=model TRAVEL_DATA_MODE=live
export TRAVEL_ALLOW_PUBLIC_OVERPASS=true
export TRAVEL_MODEL_API=chat TRAVEL_MODEL_BASE_URL="${TRAVEL_MODEL_BASE_URL:-https://generativelanguage.googleapis.com/v1beta/openai/}"
export TRAVEL_MODEL="${TRAVEL_MODEL:-gemini-3.8-flash}" TRAVEL_MODEL_API_KEY="$GEMINI_API_KEY"
export TRAVEL_MAX_TOOL_TURNS="${TRAVEL_MAX_TOOL_TURNS:-1}" TRAVEL_REASONING_EFFORT="" TRAVEL_DB="${TRAVEL_DB:-runtime/travel-live.sqlite3}"
PORT="${1:-${TRAVEL_PORT:-8011}}"
printf '\nOpen http://127.0.0.1:%s  (live OpenStreetMap data, router=%s, %s via Chat Completions adapter)\n' \
  "$PORT" "$TRAVEL_ROUTER" "$TRAVEL_MODEL"
printf '  Type any city in the City box; it is geocoded through Open-Meteo and planned with Overpass + %s.\n\n' \
  "$TRAVEL_ROUTER"
exec uv run python -m travel_agent.cli serve --port "$PORT"
