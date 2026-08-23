#!/usr/bin/env bash
#
# Replit service adapter for the upstream CPTR ASGI application. The source
# remains in the Computer artifact; this companion service exists only because
# web artifacts publish static files while CPTR's FastAPI and Socket.IO runtime
# must stay alive for API, terminal, and realtime connections.
set -euo pipefail

COMPUTER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../computer" && pwd)"
cd "$COMPUTER_DIR"

export CPTR_DATA_DIR="${CPTR_DATA_DIR:-"$COMPUTER_DIR/.cptr"}"

if [[ -z "${CPTR_STARTUP_TOKEN:-}" && -n "${SESSION_SECRET:-}" ]]; then
  export CPTR_STARTUP_TOKEN="$SESSION_SECRET"
fi

exec python -m cptr.cli run \
  --host 0.0.0.0 \
  --port "${PORT:?PORT is required}" \
  --headless