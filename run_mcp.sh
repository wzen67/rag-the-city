#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec conda run -n nn uvicorn mcp_server:app --host "${HOST:-127.0.0.1}" --port "${PORT:-3000}"
