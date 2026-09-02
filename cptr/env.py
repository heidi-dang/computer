"""Centralized environment configuration for cptr.

All environment variable reads go here. Import from this module
instead of reading os.environ directly.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("true", "1", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# ── Data directory ──────────────────────────────────────────
# Where cptr stores its database, config, and user data.
# Default: ~/.cptr
DATA_DIR = Path(os.environ.get("CPTR_DATA_DIR", str(Path.home() / ".cptr")))
CONFIG_FILE = DATA_DIR / "config.toml"
DB_FILE = DATA_DIR / "app.db"

# ── Logging ─────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("CPTR_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.environ.get("CPTR_LOG_FORMAT", "text").lower()

AUDIT_LOG_LEVEL = os.environ.get("CPTR_AUDIT_LOG_LEVEL", "NONE").upper()
AUDIT_LOG_PATH = Path(os.environ.get("CPTR_AUDIT_LOG_PATH", str(DATA_DIR / "logs" / "audit.jsonl")))
AUDIT_LOG_ROTATION = os.environ.get("CPTR_AUDIT_LOG_ROTATION", "10 MB")
AUDIT_MAX_BODY_SIZE = _env_int("CPTR_AUDIT_MAX_BODY_SIZE", 2048)
AUDIT_EXCLUDED_PATHS = [
    path.strip()
    for path in os.environ.get("CPTR_AUDIT_EXCLUDED_PATHS", "/api/chats,/v1/chat").split(",")
    if path.strip()
]

LOG_UPSTREAM_REQUESTS = _env_bool("CPTR_LOG_UPSTREAM_REQUESTS", "false")
UPSTREAM_REQUEST_LOG_PATH = Path(
    os.environ.get(
        "CPTR_UPSTREAM_REQUEST_LOG_PATH",
        str(DATA_DIR / "logs" / "upstream-requests.jsonl"),
    )
)
UPSTREAM_REQUEST_LOG_ROTATION = os.environ.get("CPTR_UPSTREAM_REQUEST_LOG_ROTATION", "50 MB")

# ── Startup token ───────────────────────────────────────────
# One-time token for first-time setup. Set by CLI, consumed by app.
STARTUP_TOKEN: str | None = os.environ.pop("CPTR_STARTUP_TOKEN", None)

# ── Chat settings ───────────────────────────────────────────
CHAT_MAX_ITERATIONS = int(os.environ.get("CHAT_MAX_ITERATIONS", "2048"))
ENABLE_CHAT_RECONCILE_ON_STARTUP: bool = os.environ.get(
    "ENABLE_CHAT_RECONCILE_ON_STARTUP", "true"
).lower() in ("true", "1", "yes")
CHAT_TOOL_MAX_CHARS = int(os.environ.get("CHAT_TOOL_MAX_CHARS", "50000"))
CHAT_TOOL_COMMAND_MAX_CHARS = int(os.environ.get("CHAT_TOOL_COMMAND_MAX_CHARS", "8000"))
CHAT_COMPACT_TOKEN_THRESHOLD = int(os.environ.get("CHAT_COMPACT_TOKEN_THRESHOLD", "80000"))
AGENT_SEED_TRANSCRIPT_MAX_CHARS = max(0, _env_int("CHAT_AGENT_SEED_TRANSCRIPT_MAX_CHARS", 12000))
# Claude SDK stdout JSON buffer; chat/tool output caps apply later after parsing.
CLAUDE_CODE_MAX_BUFFER_SIZE = _env_int("CPTR_CLAUDE_CODE_MAX_BUFFER_SIZE", 128 * 1024 * 1024)

# ── Workspace storage ───────────────────────────────────────
WORKSPACE_AUTO_GITIGNORE_DOT_CPTR_ENV = os.environ.get("CPTR_AUTO_GITIGNORE_DOT_CPTR")
WORKSPACE_AUTO_GITIGNORE_DOT_CPTR = _env_bool("CPTR_AUTO_GITIGNORE_DOT_CPTR", "true")

# ── Execute timeout ─────────────────────────────────────────
# Default wait (seconds) for run_command / check_task when the caller
# doesn't pass an explicit wait value.  None = return immediately.
EXECUTE_TIMEOUT: float | None = None
_execute_timeout = os.environ.get("CPTR_EXECUTE_TIMEOUT")
if _execute_timeout is not None:
    EXECUTE_TIMEOUT = float(_execute_timeout)

# ── AI stream settings ──────────────────────────────────────
STREAM_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("CPTR_STREAM_CONNECT_TIMEOUT", "30"))
STREAM_READ_TIMEOUT_SECONDS = float(os.environ.get("CPTR_STREAM_READ_TIMEOUT", "300"))
STREAM_WRITE_TIMEOUT_SECONDS = float(os.environ.get("CPTR_STREAM_WRITE_TIMEOUT", "600"))
TASK_CANCELLATION_TIMEOUT_SECONDS = max(
    0.1, float(os.environ.get("CPTR_TASK_CANCELLATION_TIMEOUT", "10"))
)

# ── Automation scheduler ────────────────────────────────────
AUTOMATION_POLL_INTERVAL = int(os.environ.get("AUTOMATION_POLL_INTERVAL", "10"))
TIMER_POLL_INTERVAL = int(os.environ.get("TIMER_POLL_INTERVAL", "1"))

# ── Autonomous supervisor ──────────────────────────────────
SUPERVISOR_POLL_INTERVAL = float(os.environ.get("CPTR_SUPERVISOR_POLL_INTERVAL", "2"))
SUPERVISOR_MAX_ATTEMPTS = _env_int("CPTR_SUPERVISOR_MAX_ATTEMPTS", 5)
SUPERVISOR_OPENAI_MODEL = os.environ.get("CPTR_SUPERVISOR_OPENAI_MODEL", "")
SUPERVISOR_OPENAI_API_KEY = os.environ.get("CPTR_SUPERVISOR_OPENAI_API_KEY", "")

# ── Dark Factory runtime ────────────────────────────────────
FACTORY_RUN_LEASE_MS = max(1_000, _env_int("CPTR_FACTORY_RUN_LEASE_MS", 30_000))

# ── SQLite performance / resilience ─────────────────────────
DB_BUSY_TIMEOUT_MS = max(100, _env_int("CPTR_DB_BUSY_TIMEOUT_MS", 5_000))
DB_CACHE_SIZE_KIB = max(1_024, _env_int("CPTR_DB_CACHE_SIZE_KIB", 32_768))
DB_MMAP_SIZE_BYTES = max(0, _env_int("CPTR_DB_MMAP_SIZE_BYTES", 128 * 1024 * 1024))
DB_WAL_AUTOCHECKPOINT_PAGES = max(100, _env_int("CPTR_DB_WAL_AUTOCHECKPOINT_PAGES", 1_000))
DB_SYNCHRONOUS = os.environ.get("CPTR_DB_SYNCHRONOUS", "NORMAL").strip().upper()
if DB_SYNCHRONOUS not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
    DB_SYNCHRONOUS = "NORMAL"

# ── Command execution performance / retention ───────────────
COMMAND_OUTPUT_BUFFER_BYTES = max(
    32 * 1024, _env_int("CPTR_COMMAND_OUTPUT_BUFFER_BYTES", 256 * 1024)
)
COMMAND_READ_CHUNK_BYTES = max(4_096, _env_int("CPTR_COMMAND_READ_CHUNK_BYTES", 16 * 1024))
COMMAND_SESSION_TTL_SECONDS = max(30, _env_int("CPTR_COMMAND_SESSION_TTL_SECONDS", 15 * 60))
COMMAND_SESSION_MAX_RETAINED = max(8, _env_int("CPTR_COMMAND_SESSION_MAX_RETAINED", 128))
COMMAND_SESSION_REAPER_INTERVAL_SECONDS = max(
    5, _env_int("CPTR_COMMAND_SESSION_REAPER_INTERVAL_SECONDS", 30)
)
COMMAND_LOG_MAX_BYTES = max(
    1 * 1024 * 1024, _env_int("CPTR_COMMAND_LOG_MAX_BYTES", 50 * 1024 * 1024)
)
COMMAND_LOG_BATCH_BYTES = max(16 * 1024, _env_int("CPTR_COMMAND_LOG_BATCH_BYTES", 128 * 1024))
COMMAND_LOG_FLUSH_INTERVAL_MS = max(10, _env_int("CPTR_COMMAND_LOG_FLUSH_INTERVAL_MS", 200))
COMMAND_LOG_QUEUE_SIZE = max(16, _env_int("CPTR_COMMAND_LOG_QUEUE_SIZE", 256))
COMMAND_EVENT_QUEUE_SIZE = max(8, _env_int("CPTR_COMMAND_EVENT_QUEUE_SIZE", 64))
TERMINAL_EVENT_COALESCE_BYTES = max(1_024, _env_int("CPTR_TERMINAL_EVENT_COALESCE_BYTES", 8 * 1024))
TERMINAL_EVENT_FLUSH_INTERVAL_MS = max(10, _env_int("CPTR_TERMINAL_EVENT_FLUSH_INTERVAL_MS", 100))

# ── Live-event durability ───────────────────────────────────
LIVE_EVENT_WRITE_BATCH_SIZE = max(1, _env_int("CPTR_LIVE_EVENT_WRITE_BATCH_SIZE", 64))
LIVE_EVENT_QUEUE_SIZE = max(64, _env_int("CPTR_LIVE_EVENT_QUEUE_SIZE", 2_048))
LIVE_EVENT_RETENTION_CLEANUP_INTERVAL = max(
    10, _env_int("CPTR_LIVE_EVENT_RETENTION_CLEANUP_INTERVAL", 100)
)

# ── Control-plane caches ────────────────────────────────────
CONTROL_AUTH_CACHE_TTL_SECONDS = max(0, _env_int("CPTR_CONTROL_AUTH_CACHE_TTL_SECONDS", 30))
CONTROL_AUTH_CACHE_MAX_ENTRIES = max(16, _env_int("CPTR_CONTROL_AUTH_CACHE_MAX_ENTRIES", 512))
DIRECT_CODING_IO_CONCURRENCY = max(1, _env_int("CPTR_DIRECT_CODING_IO_CONCURRENCY", 4))
DIRECT_WORKER_MAX_PER_WORKSPACE = max(1, _env_int("CPTR_DIRECT_WORKER_MAX_PER_WORKSPACE", 8))
DIRECT_WORKTREE_ROOT = os.environ.get("CPTR_DIRECT_WORKTREE_ROOT", "").strip()

# ── FDX repository intelligence ─────────────────────────────
FDX_ENABLED = _env_bool("CPTR_FDX_ENABLED", "true")
FDX_BINARY = os.environ.get("CPTR_FDX_BINARY", "").strip()
FDX_REQUEST_TIMEOUT_SECONDS = max(1, _env_int("CPTR_FDX_REQUEST_TIMEOUT_SECONDS", 20))
FDX_DAEMON_IDLE_TTL_SECONDS = max(30, _env_int("CPTR_FDX_DAEMON_IDLE_TTL_SECONDS", 10 * 60))
FDX_MAX_DAEMONS = max(1, _env_int("CPTR_FDX_MAX_DAEMONS", 8))
FDX_MAX_RESPONSE_BYTES = max(16 * 1024, _env_int("CPTR_FDX_MAX_RESPONSE_BYTES", 256 * 1024))

# ── Runtime metrics ─────────────────────────────────────────
METRICS_SAMPLE_WINDOW = max(128, _env_int("CPTR_METRICS_SAMPLE_WINDOW", 2_048))
EVENT_LOOP_LAG_SAMPLE_INTERVAL_MS = max(
    100, _env_int("CPTR_EVENT_LOOP_LAG_SAMPLE_INTERVAL_MS", 1_000)
)

# ── CORS ────────────────────────────────────────────────────
# Socket.IO CORS allowed origins.
# Default → "*" (allow all origins)
# Comma-separated list → allow specific origins only
#   e.g. "https://example.com,https://app.example.com"
_cors_raw = os.environ.get("CPTR_CORS_ALLOWED_ORIGINS", "*")
if _cors_raw.strip() == "*":
    CORS_ALLOWED_ORIGINS = "*"
else:
    CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()] or "*"
