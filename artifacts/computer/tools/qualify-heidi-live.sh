#!/usr/bin/env bash
#
# Start a disposable, same-origin CPTR instance for Heidi Live Terminal
# qualification. This script never reads the normal .cptr directory and never
# copies production credentials or model configuration.
#
# The caller may provide a temporary, non-production CPTR config through
# CPTR_QUALIFICATION_CONFIG_SOURCE. Its contents are copied into the temporary
# data directory and are removed with the rest of the instance. If it is not
# supplied, the instance intentionally starts without model connections and
# model qualification must fail closed.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -n "${CPTR_DATA_DIR:-}" || -n "${CPTR_STARTUP_TOKEN:-}" ]]; then
  echo "qualification refuses inherited CPTR_DATA_DIR/CPTR_STARTUP_TOKEN" >&2
  exit 2
fi

QUAL_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/cptr-heidi-qualification.XXXXXX")"
DATA_DIR="$QUAL_ROOT/data"
WORKSPACE="$QUAL_ROOT/workspace"
PORT="${CPTR_QUALIFICATION_PORT:-0}"
HOST="${CPTR_QUALIFICATION_HOST:-127.0.0.1}"
USER_ID_FILE="$QUAL_ROOT/user-id"
URL_FILE="$QUAL_ROOT/url"
SERVER_PID=""

cleanup() {
  set +e
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
  rm -rf -- "$QUAL_ROOT"
}
trap cleanup EXIT INT TERM HUP

mkdir -p "$DATA_DIR" "$WORKSPACE"
git -C "$WORKSPACE" init -q
git -C "$WORKSPACE" config user.name "CPTR Qualification"
git -C "$WORKSPACE" config user.email "qualification@invalid"
printf '# Disposable Heidi qualification\n' > "$WORKSPACE/README.md"
git -C "$WORKSPACE" add README.md
git -C "$WORKSPACE" commit -qm "qualification seed"

if [[ -n "${CPTR_QUALIFICATION_CONFIG_SOURCE:-}" ]]; then
  case "$CPTR_QUALIFICATION_CONFIG_SOURCE" in
    /*) config_source="$CPTR_QUALIFICATION_CONFIG_SOURCE" ;;
    *) config_source="$ROOT/$CPTR_QUALIFICATION_CONFIG_SOURCE" ;;
  esac
  [[ -f "$config_source" ]] || {
    echo "qualification config source does not exist" >&2
    exit 2
  }
  install -m 600 "$config_source" "$DATA_DIR/config.toml"
else
  QUAL_SECRET="$(
    python - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
  )"
  cat > "$DATA_DIR/config.toml" <<'EOF'
[server]
EOF
  printf 'secret = "%s"\n\n' "$QUAL_SECRET" >> "$DATA_DIR/config.toml"
  cat >> "$DATA_DIR/config.toml" <<'EOF'

[app_config]
"chat.default_model" = ""
"chat.connections" = "[]"
"subagents.enabled" = true
"subagents.background_enabled" = false
EOF
  chmod 600 "$DATA_DIR/config.toml"
fi

readarray -t QUAL_VALUES < <(
  python - "$DATA_DIR" "$WORKSPACE" <<'PY'
import asyncio
import secrets
import sys
from pathlib import Path

data_dir, workspace = sys.argv[1:3]
import os
os.environ["CPTR_DATA_DIR"] = data_dir

from cptr.utils.db import init_db
from cptr.models import User
from cptr.utils.config import hash_password, now_ms

username = f"qualification-{secrets.token_hex(8)}"
password = secrets.token_urlsafe(24)

async def main():
    await init_db()
    user_id = await User.create(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        display_name="Disposable Qualification",
        created_at=now_ms(),
    )
    print(user_id)
    print(username)
    print(password)
    print(workspace)

asyncio.run(main())
PY
)

USER_ID="${QUAL_VALUES[0]}"
QUAL_USER="${QUAL_VALUES[1]}"
QUAL_PASSWORD="${QUAL_VALUES[2]}"
printf '%s' "$USER_ID" > "$USER_ID_FILE"
chmod 600 "$USER_ID_FILE"

export CPTR_DATA_DIR="$DATA_DIR"
export CPTR_AUTO_GITIGNORE_DOT_CPTR=false
export CPTR_CORS_ALLOWED_ORIGINS="http://${HOST}:${PORT}"
export CPTR_FLOWDECK_ENABLED="${CPTR_FLOWDECK_ENABLED:-true}"
export CPTR_FLOWDECK_MODE="${CPTR_FLOWDECK_MODE:-controlled}"
export FLOWDECK_ENABLED="$CPTR_FLOWDECK_ENABLED"
export FLOWDECK_MODE="$CPTR_FLOWDECK_MODE"
# Never let the disposable instance discover the parent process's managed
# provider credentials. An explicitly supplied qualification config is the only
# supported way to provide a non-production model connection.
unset AI_INTEGRATIONS_OPENAI_API_KEY AI_INTEGRATIONS_OPENAI_BASE_URL

if [[ "$PORT" == "0" ]]; then
  PORT="$(
    python - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
  )"
fi
export CPTR_QUALIFICATION_PORT="$PORT"
export CPTR_QUALIFICATION_URL="http://${HOST}:${PORT}/"
printf '%s' "$CPTR_QUALIFICATION_URL" > "$URL_FILE"

# Deliberately do not print the username or password. A browser runner can
# source the process-local environment of this script's child if it is started
# by a parent harness; ordinary output contains only the disposable URL.
export CPTR_QUALIFICATION_USER="$QUAL_USER"
export CPTR_QUALIFICATION_PASSWORD="$QUAL_PASSWORD"
export CPTR_QUALIFICATION_WORKSPACE="$WORKSPACE"

uvicorn cptr.app:application --host "$HOST" --port "$PORT" &
SERVER_PID=$!

for _ in $(seq 1 120); do
  if curl -fsS --max-time 1 "http://${HOST}:${PORT}/api/health" >/dev/null 2>&1; then
    echo "$CPTR_QUALIFICATION_URL"
    wait "$SERVER_PID"
    exit $?
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "qualification server exited before becoming healthy" >&2
    exit 1
  fi
  sleep 0.25
done

echo "qualification server did not become healthy" >&2
exit 1