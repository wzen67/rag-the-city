#!/usr/bin/env bash
# Serve the MCP catalog + answering engine on http://127.0.0.1:3000/mcp
# Uses the repo venv (see requirements.txt); set PY to override.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "no interpreter at $PY - create one with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
if [ ! -f boston.db ]; then
  echo "boston.db missing - building it first (one-off, ~1 min)" >&2
  "$PY" scripts/build_db.py
fi
exec "$PY" -m uvicorn mcp_server:app --host "${HOST:-127.0.0.1}" --port "${PORT:-3000}"
