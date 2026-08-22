#!/usr/bin/env bash
# Serve the demo UI on http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")"
PY="${PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "no interpreter at $PY - create one with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
[ -f boston.db ] || { echo "building boston.db (one-off, ~1 min)" >&2; "$PY" scripts/build_db.py; }
exec "$PY" -m uvicorn webapp:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
