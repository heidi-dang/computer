"""MCP (Model Context Protocol) REST API.

Exposes live MCP server introspection and tool invocation over HTTP so that
clients can discover and call MCP tools without going through the agent loop.

Endpoints
---------
GET  /api/mcp/servers                          – list registered servers with live health
GET  /api/mcp/servers/{server_id}/tools        – list tools on a specific server
POST /api/mcp/servers/{server_id}/tools/{name}/invoke – invoke a tool
GET  /api/mcp/servers/{server_id}/status       – live connection status
POST /api/mcp/servers/{server_id}/reconnect    – force reconnect a dead session
GET  /api/mcp/servers/{server_id}/logs         – recent subprocess logs (stdio servers)
GET  /api/mcp/tools                            – aggregate list of all tools (all servers)
GET  /api/mcp/tools/{tool_name}                – schema for one tool
POST /api/mcp/servers/{server_id}/resources/list  – list MCP resources
POST /api/mcp/servers/{server_id}/resources/read  – read a specific MCP resource
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Any

import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from cptr.memory.mcp_adapter import MemoryMcpAdapter
from cptr.memory.service import MemoryUnavailableError
from cptr.routers.admin import require_admin
from cptr.services.coding_benchmark import SUITE_ID, coding_benchmark_store
from cptr.services.control_auth import require_control_user
from cptr.services.factory_observability import FactoryObservabilityService
from cptr.services.mcp_activity import McpActivityBatch, mcp_activity_store
from cptr.services.mcp_diagnostics import (
    McpDiagnosticsBatch,
    McpUsageDiagnostic,
    mcp_diagnostics_store,
)
from cptr.services.mcp_traffic import McpTrafficBatch, mcp_traffic_store
from cptr.services.mcp_usage_store import mcp_usage_store
from cptr.services.mcp_topology_config import get_topology_config, update_topology_aliases
from cptr.services.memory_observability import MemoryObservabilityService
from cptr.services.system_metrics import mcp_metrics_sampler
from cptr.utils import memory as managed_memory
from cptr.utils.crypto import decrypt_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])
factory_observability = FactoryObservabilityService()
memory_observability = MemoryObservabilityService()

# ── Per-server log buffer (stdio only, ring buffer of 500 lines) ──────────────
_server_logs: dict[str, deque[str]] = {}


def _log_buffer(server_id: str) -> deque[str]:
    if server_id not in _server_logs:
        _server_logs[server_id] = deque(maxlen=500)
    return _server_logs[server_id]


def append_server_log(server_id: str, line: str) -> None:
    """Called by the stdio manager to record output."""
    _log_buffer(server_id).append(line)


async def _require_traffic_writer(request: Request) -> str:
    """Authenticate the plugin telemetry writer with its dedicated scope."""
    return await require_control_user(request, "mcp:traffic:write")


def _traffic_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def _require_activity_writer(request: Request) -> str:
    """Authenticate the plugin Activity writer with its dedicated scope."""
    return await require_control_user(request, "mcp:activity:write")


def _activity_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def _require_diagnostics_writer(request: Request) -> str:
    """Authenticate the plugin Diagnostics writer with its dedicated scope."""
    return await require_control_user(request, "mcp:diagnostics:write")


def _diagnostics_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _factory_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"


def _bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _memory_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _get_tool_servers() -> list[dict]:
    """Load tool server configs from the Config store (same as admin router)."""
    from cptr.models import Config

    value = await Config.get("tool_servers")
    return list(value) if isinstance(value, list) else []


def _headers_for(server: dict) -> dict | None:
    """Build auth headers for an HTTP MCP server."""
    raw_key = server.get("api_key") or server.get("apiKey") or ""
    if not raw_key:
        return None
    try:
        key = decrypt_key(raw_key)
    except Exception:
        key = raw_key
    return {"Authorization": f"Bearer {key}"}


async def _get_client(server: dict):
    """Return a connected MCPClient for the given server config dict."""
    from cptr.utils.mcp.client import MCPClient
    from cptr.utils.mcp.stdio_manager import stdio_manager

    server_type = server.get("type", "openapi")
    server_id = server["id"]

    if server_type == "mcp":
        url = server.get("url", "")
        if not url:
            raise ValueError("MCP server has no URL")
        headers = _headers_for(server)
        client = MCPClient()
        await client.connect(url, headers=headers)
        return client, True  # (client, should_disconnect_after)

    elif server_type == "mcp_stdio":
        command = server.get("command", "")
        if not command:
            raise ValueError("stdio MCP server has no command")
        client = await stdio_manager.get_client(
            server_id=server_id,
            command=command,
            args=server.get("args") or [],
            env=server.get("env"),
            cwd=server.get("cwd"),
        )
        return client, False  # keep-alive; don't disconnect after

    raise ValueError(
        f"Server type '{server_type}' is not an MCP server (type must be 'mcp' or 'mcp_stdio')"
    )


# ── Request / response models ────────────────────────────────────────────────


class InvokeToolRequest(BaseModel):
    arguments: dict[str, Any] = {}


class ResourceReadRequest(BaseModel):
    uri: str


class MemoryToolInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = {}


class McpTopologyConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aliases: dict[str, str | None]


# ── Endpoints ────────────────────────────────────────────────────────────────


def _memory_source_forgetter(request: Request, user_id: str):
    async def _forget(row: dict[str, Any]) -> None:
        structured = (
            row.get("structured_value") if isinstance(row.get("structured_value"), dict) else {}
        )
        source = structured.get("managed_source") if isinstance(structured, dict) else None
        if not isinstance(source, dict) or not source:
            return
        operation = {
            "action": "remove",
            "path": str(source.get("path") or ""),
            "heading": str(source.get("heading") or ""),
            "memory_id": str(source.get("memory_id") or ""),
            "old_text": str(row.get("canonical_text") or ""),
        }
        result = await managed_memory.write_memory(
            request,
            user_id,
            str(row.get("workspace") or ""),
            str(source.get("scope") or row.get("scope") or "workspace"),
            [operation],
        )
        if not bool(result.get("success")):
            error = str(
                result.get("error") or result.get("message") or "managed source delete failed"
            )
            if "no matching section or text" not in error.lower():
                raise RuntimeError(error)

    return _forget


@router.get("/memory/tools")
async def list_memory_core_tools(
    request: Request,
    workspace_id: str | None = Query(default=None, max_length=200),
):
    """Expose the embedded Memory Core through an owner-bound MCP-style adapter."""
    admin = require_admin(request)
    try:
        workspace = await memory_observability.resolve_workspace_path(admin.user_id, workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="memory workspace not found") from exc
    adapter = MemoryMcpAdapter(
        user_id=admin.user_id,
        workspace=workspace,
        allow_mutations=True,
        source_forgetter=_memory_source_forgetter(request, admin.user_id),
    )
    tools = adapter.tool_definitions()
    return {"tools": tools, "count": len(tools), "workspace_id": workspace_id}


@router.post("/memory/tools/{tool_name}/invoke")
async def invoke_memory_core_tool(
    request: Request,
    tool_name: str,
    body: MemoryToolInvokeRequest,
    workspace_id: str | None = Query(default=None, max_length=200),
):
    """Invoke one embedded Memory Core tool with server-bound identity/workspace."""
    admin = require_admin(request)
    try:
        workspace = await memory_observability.resolve_workspace_path(admin.user_id, workspace_id)
        adapter = MemoryMcpAdapter(
            user_id=admin.user_id,
            workspace=workspace,
            allow_mutations=True,
            source_forgetter=_memory_source_forgetter(request, admin.user_id),
        )
        result = await adapter.call_tool(tool_name, body.arguments)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MemoryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"tool": tool_name, "workspace_id": workspace_id, "result": result}


@router.get("/memory/snapshot")
async def get_memory_observability_snapshot(
    request: Request,
    workspace_id: str | None = Query(default=None, max_length=200),
    node_limit: int = Query(default=400, ge=25, le=2000),
    event_limit: int = Query(default=120, ge=1, le=500),
):
    """Return a bounded owner-scoped projection of canonical memory plus provenance."""
    admin = require_admin(request)
    try:
        return await memory_observability.snapshot(
            user_id=admin.user_id,
            workspace_id=workspace_id,
            node_limit=node_limit,
            event_limit=event_limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="memory workspace not found") from exc


@router.get("/memory/timeline")
async def get_memory_observability_timeline(
    request: Request,
    at_ms: int = Query(ge=0),
    known_at_ms: int | None = Query(default=None, ge=0),
    workspace_id: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=300, ge=1, le=1000),
):
    """Return bi-temporal memory: valid at ``at_ms`` and optionally known by ``known_at_ms``."""
    admin = require_admin(request)
    try:
        return await memory_observability.timeline(
            user_id=admin.user_id,
            workspace_id=workspace_id,
            at_ms=at_ms,
            known_at_ms=known_at_ms,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="memory workspace not found") from exc


@router.get("/memory/stream")
async def stream_memory_observability(
    request: Request,
    workspace_id: str | None = Query(default=None, max_length=200),
    node_limit: int = Query(default=400, ge=25, le=2000),
    event_limit: int = Query(default=120, ge=1, le=500),
):
    """Stream changed memory graph/provenance snapshots to the authenticated admin UI."""
    admin = require_admin(request)

    async def _event_stream():
        previous_fingerprint: str | None = None
        quiet_ticks = 0
        yield "retry: 1500\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                snapshot = await memory_observability.snapshot(
                    user_id=admin.user_id,
                    workspace_id=workspace_id,
                    node_limit=node_limit,
                    event_limit=event_limit,
                )
            except KeyError:
                yield _memory_sse("memory_error", {"code": "MEMORY_WORKSPACE_NOT_FOUND"})
                break
            fingerprint = str(snapshot.get("fingerprint") or "")
            if fingerprint != previous_fingerprint:
                previous_fingerprint = fingerprint
                quiet_ticks = 0
                yield _memory_sse("snapshot", snapshot)
            else:
                quiet_ticks += 1
                if quiet_ticks >= 10:
                    quiet_ticks = 0
                    yield ": keepalive\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/factory/snapshot")
async def get_factory_observability_snapshot(
    request: Request,
    run_id: str | None = Query(default=None, max_length=200),
    run_limit: int = Query(default=20, ge=1, le=30),
):
    """Return a bounded, owner-scoped Dark Factory operations snapshot."""
    admin = require_admin(request)
    try:
        return await factory_observability.snapshot(
            user_id=admin.user_id,
            run_id=run_id,
            run_limit=run_limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="factory run not found") from exc


@router.get("/factory/stream")
async def stream_factory_observability(
    request: Request,
    run_id: str | None = Query(default=None, max_length=200),
    run_limit: int = Query(default=20, ge=1, le=30),
):
    """Stream changed durable Dark Factory snapshots to the authenticated admin UI."""
    admin = require_admin(request)

    async def _event_stream():
        previous_fingerprint: str | None = None
        previous_event_sequence = 0
        previous_progress: str | None = None
        interval_seconds = _bounded_env_float(
            "CPTR_FACTORY_STREAM_INTERVAL_SECONDS", 0.5, 0.25, 5.0
        )
        keepalive_seconds = _bounded_env_float(
            "CPTR_FACTORY_STREAM_KEEPALIVE_SECONDS", 15.0, 5.0, 60.0
        )
        loop = asyncio.get_running_loop()
        last_emit_at = loop.time()
        yield "retry: 1500\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                snapshot = await factory_observability.snapshot(
                    user_id=admin.user_id,
                    run_id=run_id,
                    run_limit=run_limit,
                )
            except KeyError:
                yield _factory_sse("factory_error", {"code": "FACTORY_RUN_NOT_FOUND"})
                break

            fingerprint = str(snapshot.get("fingerprint") or "")
            selected = snapshot.get("selected") if isinstance(snapshot, dict) else None
            selected = selected if isinstance(selected, dict) else None
            progress = selected.get("progress") if selected else None
            progress_key = (
                json.dumps(progress, sort_keys=True, separators=(",", ":"), default=str)
                if isinstance(progress, dict)
                else None
            )
            summary = selected.get("summary", {}) if selected else {}
            summary = summary if isinstance(summary, dict) else {}
            current_event_sequence = int(summary.get("last_event_sequence") or 0)

            if fingerprint != previous_fingerprint:
                initial = previous_fingerprint is None
                if not initial:
                    selected_run_id = str(selected.get("run_id") or "") if selected else ""
                    cursor = previous_event_sequence
                    while selected_run_id and cursor < current_event_sequence:
                        try:
                            activity_batch = await factory_observability.activity_since(
                                user_id=admin.user_id,
                                run_id=selected_run_id,
                                after_sequence=cursor,
                                limit=500,
                            )
                        except KeyError:
                            yield _factory_sse("factory_error", {"code": "FACTORY_RUN_NOT_FOUND"})
                            return
                        if not activity_batch:
                            break
                        advanced_cursor = cursor
                        for event in activity_batch:
                            sequence = int(event.get("sequence") or 0)
                            if sequence <= cursor:
                                continue
                            yield _factory_sse("activity", event)
                            last_emit_at = loop.time()
                            advanced_cursor = max(advanced_cursor, sequence)
                        if advanced_cursor <= cursor:
                            break
                        cursor = advanced_cursor
                    if progress_key != previous_progress and isinstance(progress, dict):
                        yield _factory_sse("progress", progress)
                        last_emit_at = loop.time()

                # Keep snapshot first on initial connect for backwards-compatible
                # hydration; subsequent fine-grained events arrive before the
                # snapshot so activity/progress surfaces react immediately.
                yield _factory_sse("snapshot", snapshot)
                last_emit_at = loop.time()
                if initial and isinstance(progress, dict):
                    yield _factory_sse("progress", progress)
                    last_emit_at = loop.time()

                previous_fingerprint = fingerprint
                previous_event_sequence = current_event_sequence
                previous_progress = progress_key
            elif loop.time() - last_emit_at >= keepalive_seconds:
                yield ": keepalive\n\n"
                last_emit_at = loop.time()

            await asyncio.sleep(interval_seconds)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/diagnostics/events")
async def ingest_mcp_diagnostics(request: Request, body: McpDiagnosticsBatch):
    """Persist usage durably before publishing one bounded diagnostics batch."""
    owner_id = await _require_diagnostics_writer(request)
    accepted_usage_ids = await mcp_usage_store.ingest(owner_id, body.events)
    filtered_events = []
    emitted_usage_ids: set[str] = set()
    durable_duplicates = 0
    for event in body.events:
        if not isinstance(event, McpUsageDiagnostic):
            filtered_events.append(event)
            continue
        if event.event_id not in accepted_usage_ids or event.event_id in emitted_usage_ids:
            durable_duplicates += 1
            continue
        emitted_usage_ids.add(event.event_id)
        filtered_events.append(event)
    result = await mcp_diagnostics_store.ingest(filtered_events)
    result["duplicates"] += durable_duplicates
    return result


async def _diagnostics_snapshot(owner_id: str) -> dict[str, object]:
    snapshot = await mcp_diagnostics_store.snapshot()
    snapshot["usage_periods"] = await mcp_usage_store.summary(owner_id)
    return snapshot


@router.get("/diagnostics/snapshot")
async def get_mcp_diagnostics_snapshot(request: Request):
    """Return bounded live diagnostics plus database-backed durable usage periods."""
    admin = require_admin(request)
    await mcp_metrics_sampler.ensure_started()
    return await _diagnostics_snapshot(admin.user_id)


@router.get("/engineering/sessions")
async def get_mcp_engineering_sessions(
    request: Request, limit: int = Query(default=50, ge=1, le=200)
):
    """Return payload-free observed engineering metrics; these are not comparable benchmarks."""
    admin = require_admin(request)
    return await mcp_usage_store.engineering_sessions(admin.user_id, limit=limit)


@router.get("/benchmarks/leaderboard")
async def get_mcp_benchmark_leaderboard(
    request: Request,
    suite_id: str = Query(default=SUITE_ID, min_length=1, max_length=80),
):
    """Return only comparable standardized benchmark results to the admin dashboard."""
    admin = require_admin(request)
    try:
        return await coding_benchmark_store.leaderboard(admin.user_id, suite_id=suite_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:200]) from exc


@router.get("/diagnostics/stream")
async def stream_mcp_diagnostics(request: Request):
    """Stream bounded MCP diagnostics to an authenticated admin browser."""
    admin = require_admin(request)
    await mcp_metrics_sampler.ensure_started()

    async def _event_stream():
        queue = mcp_diagnostics_store.subscribe()
        try:
            yield _diagnostics_sse("snapshot", await _diagnostics_snapshot(admin.user_id))
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                event_name = str(event.get("kind") or "diagnostics")
                yield _diagnostics_sse(event_name, event)
        finally:
            mcp_diagnostics_store.unsubscribe(queue)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/topology/config")
async def get_mcp_topology_config(request: Request):
    """Return canonical topology names and admin-managed display aliases."""
    require_admin(request)
    return await get_topology_config()


@router.put("/topology/config")
async def put_mcp_topology_config(request: Request, body: McpTopologyConfigUpdate):
    """Partially update or reset admin-managed topology display aliases."""
    require_admin(request)
    try:
        return await update_topology_aliases(body.aliases)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/activity/events")
async def ingest_mcp_activity(request: Request, body: McpActivityBatch):
    """Accept one bounded batch of redacted tool activity from the MCP adapter."""
    await _require_activity_writer(request)
    return await mcp_activity_store.ingest(body.events)


@router.get("/activity/snapshot")
async def get_mcp_activity_snapshot(request: Request):
    """Return the admin-only bounded MCP tool activity snapshot."""
    require_admin(request)
    return await mcp_activity_store.snapshot()


@router.get("/activity/stream")
async def stream_mcp_activity(request: Request):
    """Stream bounded MCP tool activity to an authenticated admin browser."""
    require_admin(request)

    async def _event_stream():
        queue = mcp_activity_store.subscribe()
        try:
            yield _activity_sse("snapshot", await mcp_activity_store.snapshot())
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _activity_sse("activity", event)
        finally:
            mcp_activity_store.unsubscribe(queue)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/traffic/events")
async def ingest_mcp_traffic(request: Request, body: McpTrafficBatch):
    """Accept one bounded batch of sanitized telemetry from the MCP adapter."""
    await _require_traffic_writer(request)
    await mcp_traffic_store.expire_stale_sessions()
    return await mcp_traffic_store.ingest(body.events)


@router.get("/traffic/snapshot")
async def get_mcp_traffic_snapshot(request: Request):
    """Return the admin-only current MCP topology snapshot."""
    require_admin(request)
    await mcp_traffic_store.expire_stale_sessions()
    return await mcp_traffic_store.snapshot()


@router.get("/traffic/stream")
async def stream_mcp_traffic(request: Request):
    """Stream bounded MCP traffic events to an authenticated admin browser."""
    require_admin(request)

    async def _event_stream():
        queue = mcp_traffic_store.subscribe()
        try:
            await mcp_traffic_store.expire_stale_sessions()
            yield _traffic_sse("snapshot", await mcp_traffic_store.snapshot())
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    expired = await mcp_traffic_store.expire_stale_sessions()
                    if expired:
                        yield _traffic_sse("snapshot", await mcp_traffic_store.snapshot())
                    else:
                        yield ": keepalive\n\n"
                    continue
                yield _traffic_sse("traffic", event)
        finally:
            mcp_traffic_store.unsubscribe(queue)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/servers")
async def list_mcp_servers(request: Request):
    """List all registered tool servers with live health status."""
    require_admin(request)
    servers = await _get_tool_servers()
    result = []
    for s in servers:
        server_type = s.get("type", "openapi")
        entry: dict[str, Any] = {
            "id": s.get("id"),
            "name": s.get("name", ""),
            "type": server_type,
            "url": s.get("url", "") if server_type == "mcp" else None,
            "command": s.get("command", "") if server_type == "mcp_stdio" else None,
            "enabled": s.get("enabled", True),
            "health": "unknown",
        }
        if server_type in ("mcp", "mcp_stdio"):
            try:
                client, should_disconnect = await asyncio.wait_for(_get_client(s), timeout=5.0)
                # Just poke list_tools to verify connection
                await asyncio.wait_for(client.list_tool_specs(), timeout=5.0)
                entry["health"] = "connected"
                if should_disconnect:
                    await client.disconnect()
            except asyncio.TimeoutError:
                entry["health"] = "timeout"
            except Exception as exc:
                entry["health"] = f"error: {exc}"
        else:
            entry["health"] = "n/a"
        result.append(entry)
    return {"servers": result}


@router.get("/servers/{server_id}/tools")
async def list_server_tools(request: Request, server_id: str):
    """List all tools exposed by a specific MCP server."""
    require_admin(request)
    servers = await _get_tool_servers()
    server = next((s for s in servers if s.get("id") == server_id), None)
    if server is None:
        raise HTTPException(404, f"Server '{server_id}' not found")
    server_type = server.get("type", "openapi")
    if server_type not in ("mcp", "mcp_stdio"):
        raise HTTPException(400, "Server is not an MCP server")
    try:
        client, should_disconnect = await asyncio.wait_for(_get_client(server), timeout=15.0)
        try:
            tools = await asyncio.wait_for(client.list_tool_specs(), timeout=15.0)
        finally:
            if should_disconnect:
                await client.disconnect()
    except asyncio.TimeoutError:
        raise HTTPException(504, "MCP server connection timed out")
    except Exception as exc:
        raise HTTPException(502, f"MCP error: {exc}")
    return {"server_id": server_id, "tools": tools}


@router.post("/servers/{server_id}/tools/{tool_name}/invoke")
async def invoke_server_tool(
    request: Request,
    server_id: str,
    tool_name: str,
    body: InvokeToolRequest,
    stream: bool = Query(
        False, description="Return an SSE stream instead of a single JSON response"
    ),
):
    """Invoke a named tool on a specific MCP server.

    Set ?stream=1 to receive Server-Sent Events:
        event: tool_start   data: {"tool": "...", "arguments": {...}}
        event: tool_chunk   data: <one McpContentItem as JSON>
        event: tool_done    data: {"result": [...], "elapsed_ms": N}
        event: tool_error   data: {"message": "..."}
    """
    require_admin(request)
    servers = await _get_tool_servers()
    server = next((s for s in servers if s.get("id") == server_id), None)
    if server is None:
        raise HTTPException(404, f"Server '{server_id}' not found")
    server_type = server.get("type", "openapi")
    if server_type not in ("mcp", "mcp_stdio"):
        raise HTTPException(400, "Server is not an MCP server")

    async def _run_tool():
        """Connect and call the tool, returns (client, result, should_disconnect)."""
        client, should_disconnect = await asyncio.wait_for(_get_client(server), timeout=15.0)
        try:
            result = await asyncio.wait_for(
                client.call_tool(tool_name, body.arguments), timeout=60.0
            )
        finally:
            if should_disconnect:
                await client.disconnect()
        return result

    def _sse(event: str, data: Any) -> str:
        return "event: " + event + "\ndata: " + json.dumps(data) + "\n\n"

    if stream:

        async def _event_stream():
            import time

            t0 = time.time()
            yield _sse("tool_start", {"tool": tool_name, "arguments": body.arguments})
            try:
                result = await _run_tool()
                for item in result:
                    content = (
                        item
                        if isinstance(item, dict)
                        else (item.model_dump() if hasattr(item, "model_dump") else str(item))
                    )
                    yield _sse("tool_chunk", content)
                elapsed_ms = int((time.time() - t0) * 1000)
                result_serializable = [
                    r
                    if isinstance(r, dict)
                    else (r.model_dump() if hasattr(r, "model_dump") else str(r))
                    for r in result
                ]
                yield _sse("tool_done", {"result": result_serializable, "elapsed_ms": elapsed_ms})
            except asyncio.TimeoutError:
                yield _sse("tool_error", {"message": "MCP tool call timed out"})
            except Exception as exc:
                yield _sse("tool_error", {"message": str(exc)})

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming path (default)
    try:
        result = await _run_tool()
    except asyncio.TimeoutError:
        raise HTTPException(504, "MCP tool call timed out")
    except RuntimeError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"MCP error: {exc}")
    result_serializable = [
        r if isinstance(r, dict) else (r.model_dump() if hasattr(r, "model_dump") else str(r))
        for r in result
    ]
    return {"server_id": server_id, "tool": tool_name, "result": result_serializable}


@router.get("/servers/{server_id}/status")
async def get_server_status(request: Request, server_id: str):
    """Get the live connection status of a single MCP server."""
    require_admin(request)
    servers = await _get_tool_servers()
    server = next((s for s in servers if s.get("id") == server_id), None)
    if server is None:
        raise HTTPException(404, f"Server '{server_id}' not found")
    server_type = server.get("type", "openapi")
    if server_type not in ("mcp", "mcp_stdio"):
        return {"server_id": server_id, "type": server_type, "status": "n/a"}

    # For stdio, check if we have a live managed session
    if server_type == "mcp_stdio":
        from cptr.utils.mcp.stdio_manager import stdio_manager

        client = stdio_manager._instances.get(server_id)
        connected = client is not None and client.session is not None
        return {
            "server_id": server_id,
            "type": server_type,
            "status": "connected" if connected else "disconnected",
        }

    # For HTTP MCP, do a quick connect probe
    try:
        client, _ = await asyncio.wait_for(_get_client(server), timeout=5.0)
        await client.disconnect()
        return {"server_id": server_id, "type": server_type, "status": "connected"}
    except asyncio.TimeoutError:
        return {"server_id": server_id, "type": server_type, "status": "timeout"}
    except Exception as exc:
        return {"server_id": server_id, "type": server_type, "status": "error", "detail": str(exc)}


@router.post("/servers/{server_id}/reconnect")
async def reconnect_server(request: Request, server_id: str):
    """Force-reconnect a dead or stale MCP server session."""
    require_admin(request)
    servers = await _get_tool_servers()
    server = next((s for s in servers if s.get("id") == server_id), None)
    if server is None:
        raise HTTPException(404, f"Server '{server_id}' not found")
    server_type = server.get("type", "openapi")
    if server_type not in ("mcp", "mcp_stdio"):
        raise HTTPException(400, "Server is not an MCP server")

    if server_type == "mcp_stdio":
        from cptr.utils.mcp.stdio_manager import stdio_manager

        # Kill existing session if present
        await stdio_manager.disconnect(server_id)
        command = server.get("command", "")
        if not command:
            raise HTTPException(400, "stdio MCP server has no command configured")
        try:
            await asyncio.wait_for(
                stdio_manager.get_client(
                    server_id=server_id,
                    command=command,
                    args=server.get("args") or [],
                    env=server.get("env"),
                    cwd=server.get("cwd"),
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "Reconnection timed out")
        except Exception as exc:
            raise HTTPException(502, f"Reconnection failed: {exc}")
        return {"ok": True, "server_id": server_id, "status": "reconnected"}

    # For HTTP MCP, just verify connectivity
    try:
        client, _ = await asyncio.wait_for(_get_client(server), timeout=15.0)
        await client.disconnect()
    except asyncio.TimeoutError:
        raise HTTPException(504, "Reconnection timed out")
    except Exception as exc:
        raise HTTPException(502, f"Reconnection failed: {exc}")
    return {"ok": True, "server_id": server_id, "status": "connected"}


@router.get("/servers/{server_id}/logs")
async def get_server_logs(request: Request, server_id: str, limit: int = 200):
    """Retrieve recent log lines from an stdio MCP subprocess (ring buffer, newest last)."""
    require_admin(request)
    servers = await _get_tool_servers()
    server = next((s for s in servers if s.get("id") == server_id), None)
    if server is None:
        raise HTTPException(404, f"Server '{server_id}' not found")
    if server.get("type") != "mcp_stdio":
        raise HTTPException(400, "Log streaming is only available for stdio MCP servers")
    buf = _log_buffer(server_id)
    lines = list(buf)[-max(1, min(limit, 500)) :]
    return {"server_id": server_id, "lines": lines, "total_buffered": len(buf)}


@router.get("/tools")
async def list_all_tools(request: Request):
    """Aggregate listing of every tool across ALL connected MCP servers."""
    require_admin(request)
    servers = await _get_tool_servers()
    mcp_servers = [s for s in servers if s.get("type") in ("mcp", "mcp_stdio")]

    async def _fetch(server: dict) -> list[dict]:
        try:
            client, should_disconnect = await asyncio.wait_for(_get_client(server), timeout=10.0)
            try:
                specs = await asyncio.wait_for(client.list_tool_specs(), timeout=10.0)
            finally:
                if should_disconnect:
                    await client.disconnect()
            for spec in specs:
                spec["_server_id"] = server.get("id")
                spec["_server_name"] = server.get("name", "")
            return specs
        except Exception as exc:
            logger.warning("[mcp] Failed to list tools from %s: %s", server.get("id"), exc)
            return []

    results = await asyncio.gather(*[_fetch(s) for s in mcp_servers])
    all_tools: list[dict] = []
    seen: set[str] = set()
    for batch in results:
        for tool in batch:
            key = f"{tool.get('_server_id')}::{tool.get('name')}"
            if key not in seen:
                seen.add(key)
                all_tools.append(tool)
    return {"tools": all_tools, "count": len(all_tools)}


@router.get("/tools/{tool_name}")
async def get_tool_schema(request: Request, tool_name: str):
    """Fetch the JSON schema / description for a single tool by name (first match wins)."""
    require_admin(request)
    servers = await _get_tool_servers()
    mcp_servers = [s for s in servers if s.get("type") in ("mcp", "mcp_stdio")]

    for server in mcp_servers:
        try:
            client, should_disconnect = await asyncio.wait_for(_get_client(server), timeout=10.0)
            try:
                specs = await asyncio.wait_for(client.list_tool_specs(), timeout=10.0)
            finally:
                if should_disconnect:
                    await client.disconnect()
            match = next((s for s in specs if s.get("name") == tool_name), None)
            if match:
                match["_server_id"] = server.get("id")
                match["_server_name"] = server.get("name", "")
                return match
        except Exception as exc:
            logger.debug("[mcp] Skipping server %s: %s", server.get("id"), exc)

    raise HTTPException(404, f"Tool '{tool_name}' not found on any MCP server")


@router.post("/servers/{server_id}/resources/list")
async def list_server_resources(request: Request, server_id: str):
    """List MCP resources advertised by a server."""
    require_admin(request)
    servers = await _get_tool_servers()
    server = next((s for s in servers if s.get("id") == server_id), None)
    if server is None:
        raise HTTPException(404, f"Server '{server_id}' not found")
    server_type = server.get("type", "openapi")
    if server_type not in ("mcp", "mcp_stdio"):
        raise HTTPException(400, "Server is not an MCP server")
    try:
        client, should_disconnect = await asyncio.wait_for(_get_client(server), timeout=15.0)
        try:
            if not client.session:
                raise RuntimeError("Not connected")
            result = await asyncio.wait_for(client.session.list_resources(), timeout=15.0)
            resources = [r.model_dump() for r in result.resources]
        finally:
            if should_disconnect:
                await client.disconnect()
    except asyncio.TimeoutError:
        raise HTTPException(504, "MCP server timed out")
    except Exception as exc:
        raise HTTPException(502, f"MCP error: {exc}")
    return {"server_id": server_id, "resources": resources}


@router.post("/servers/{server_id}/resources/read")
async def read_server_resource(request: Request, server_id: str, body: ResourceReadRequest):
    """Read a specific MCP resource by URI."""
    require_admin(request)
    servers = await _get_tool_servers()
    server = next((s for s in servers if s.get("id") == server_id), None)
    if server is None:
        raise HTTPException(404, f"Server '{server_id}' not found")
    server_type = server.get("type", "openapi")
    if server_type not in ("mcp", "mcp_stdio"):
        raise HTTPException(400, "Server is not an MCP server")
    try:
        client, should_disconnect = await asyncio.wait_for(_get_client(server), timeout=15.0)
        try:
            if not client.session:
                raise RuntimeError("Not connected")
            result = await asyncio.wait_for(client.session.read_resource(body.uri), timeout=30.0)
            contents = [c.model_dump() for c in result.contents]
        finally:
            if should_disconnect:
                await client.disconnect()
    except asyncio.TimeoutError:
        raise HTTPException(504, "MCP server timed out")
    except Exception as exc:
        raise HTTPException(502, f"MCP error: {exc}")
    return {"server_id": server_id, "uri": body.uri, "contents": contents}
