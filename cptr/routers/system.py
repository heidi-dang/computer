"""System health and runtime metrics endpoints.

GET /api/system/health   – lightweight health-check (DB, model connections, MCP servers)
GET /api/system/metrics  – runtime metrics snapshot (CPU, memory, latencies, queue depths)
"""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import APIRouter, Request

from cptr.routers.admin import require_admin
from cptr.utils.db import database_ready, database_file_stats

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/health")
async def health_check(request: Request):
    """Lightweight health check: DB reachable, model connections live, MCP servers status."""
    require_admin(request)

    checks: dict[str, object] = {}

    # ── Database ──────────────────────────────────────────────────────────────
    try:
        db_ok = await asyncio.wait_for(database_ready(), timeout=3.0)
        checks["database"] = {"status": "ok" if db_ok else "error"}
    except asyncio.TimeoutError:
        checks["database"] = {"status": "timeout"}
    except Exception as exc:
        checks["database"] = {"status": "error", "detail": str(exc)}

    # ── Model connections (cached) ────────────────────────────────────────────
    try:
        from cptr.models import Config
        default_model = await Config.get("chat.default_model")
        checks["default_model"] = {"value": str(default_model) if default_model else None}
    except Exception as exc:
        checks["default_model"] = {"status": "error", "detail": str(exc)}

    # ── MCP servers ───────────────────────────────────────────────────────────
    try:
        from cptr.models import Config as ConfigModel
        tool_servers_raw = await ConfigModel.get("tool_servers")
        tool_servers = list(tool_servers_raw) if isinstance(tool_servers_raw, list) else []
        mcp_servers = [s for s in tool_servers if s.get("type") in ("mcp", "mcp_stdio")]

        from cptr.utils.mcp.stdio_manager import stdio_manager
        mcp_status = []
        for s in mcp_servers:
            sid = s.get("id", "")
            stype = s.get("type", "")
            if stype == "mcp_stdio":
                client = stdio_manager._instances.get(sid)
                alive = client is not None and client.session is not None
                mcp_status.append({"id": sid, "type": stype, "status": "connected" if alive else "disconnected"})
            else:
                mcp_status.append({"id": sid, "type": stype, "status": "http"})
        checks["mcp_servers"] = {"servers": mcp_status, "count": len(mcp_servers)}
    except Exception as exc:
        checks["mcp_servers"] = {"status": "error", "detail": str(exc)}

    # ── Process uptime ────────────────────────────────────────────────────────
    checks["process"] = {
        "pid": os.getpid(),
        "uptime_seconds": int(time.time() - _PROCESS_START),
    }

    overall = "ok" if checks.get("database", {}).get("status") == "ok" else "degraded"  # type: ignore[union-attr]
    return {"status": overall, "checks": checks}


@router.get("/metrics")
async def runtime_metrics_endpoint(request: Request):
    """Return runtime metrics snapshot (latencies, queue depths, memory, event-loop)."""
    require_admin(request)

    from cptr.services.runtime_metrics import runtime_metrics

    snapshot = runtime_metrics.snapshot()

    # Add DB file sizes
    snapshot["storage"] = database_file_stats()

    # Active terminal sessions
    try:
        from cptr.utils.terminal import manager as terminal_manager
        snapshot["terminal_sessions"] = len(terminal_manager._sessions)
    except Exception:
        snapshot["terminal_sessions"] = None

    # Active browser sessions
    try:
        from cptr.utils.browser.proxy import manager as browser_manager
        snapshot["browser_sessions"] = browser_manager.count()
    except Exception:
        snapshot["browser_sessions"] = None

    # Stdio MCP server count
    try:
        from cptr.utils.mcp.stdio_manager import stdio_manager
        snapshot["mcp_stdio_sessions"] = len(stdio_manager._instances)
    except Exception:
        snapshot["mcp_stdio_sessions"] = None

    return snapshot


_PROCESS_START = time.time()
