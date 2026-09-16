#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  uv venv .venv --python "${PYTHON_VERSION:-3.11}" --allow-existing
  uv pip install --python .venv/bin/python -r requirements-demo.txt
else
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements-demo.txt
fi
printf '\nOpen http://127.0.0.1:%s\nFixture data and rule-based replay are enabled by default.\n\n' "${TRAVEL_PORT:-8000}"
exec .venv/bin/python -m travel_agent.cli serve "$@"
