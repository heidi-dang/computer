#!/usr/bin/env bash
#
# Replit runtime adapter. The application itself remains the upstream cptr
# server; this script only supplies the managed port and keeps its SQLite
# state with the checked-out project during development.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

export CPTR_DATA_DIR="${CPTR_DATA_DIR:-"$PROJECT_DIR/.cptr"}"

# On Replit, SESSION_SECRET is already a workspace secret. Reuse it only for
# the one-time cptr setup URL so a workflow restart does not invalidate the
# setup screen before the initial administrator is created. Outside Replit,
# cptr retains its random startup token behavior.
if [[ -z "${CPTR_STARTUP_TOKEN:-}" && -n "${SESSION_SECRET:-}" ]]; then
  export CPTR_STARTUP_TOKEN="$SESSION_SECRET"
fi

exec python -m cptr.cli run \
  --host 0.0.0.0 \
  --port "${PORT:?PORT is required}" \
  --headless