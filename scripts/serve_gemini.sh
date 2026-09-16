#!/usr/bin/env bash
# Local development server: specialists run on Google Gemini through the Chat Completions adapter (no SuperGrid credits).
# Usage: GEMINI_API_KEY=... ./scripts/serve_gemini.sh [port]   (default model gemini-3.8-flash, fixture data)
set -euo pipefail
cd "$(dirname "$0")/.."
: "${GEMINI_API_KEY:?Set GEMINI_API_KEY}"
export TRAVEL_EXECUTION_BACKEND=local TRAVEL_AGENT_MODE=model TRAVEL_DATA_MODE="${TRAVEL_DATA_MODE:-fixture}"
export TRAVEL_MODEL_API=chat TRAVEL_MODEL_BASE_URL="${TRAVEL_MODEL_BASE_URL:-https://generativelanguage.googleapis.com/v1beta/openai/}"
export TRAVEL_MODEL="${TRAVEL_MODEL:-gemini-3.8-flash}" TRAVEL_MODEL_API_KEY="$GEMINI_API_KEY"
export TRAVEL_MAX_TOOL_TURNS="${TRAVEL_MAX_TOOL_TURNS:-1}" TRAVEL_REASONING_EFFORT="" TRAVEL_DB="${TRAVEL_DB:-runtime/travel-gemini.sqlite3}"
PORT="${1:-${TRAVEL_PORT:-8000}}"
printf '\nOpen http://127.0.0.1:%s  (local backend, %s via Chat Completions adapter)\n\n' "$PORT" "$TRAVEL_MODEL"
exec uv run python -m travel_agent.cli serve --port "$PORT"
