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
from collections import deque
from typing import Any

import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from cptr.routers.admin import require_admin
from cptr.services.control_auth import authenticate_control_request
from cptr.services.mcp_activity import McpActivityBatch, mcp_activity_store
from cptr.services.mcp_traffic import McpTrafficBatch, mcp_traffic_store
from cptr.utils.crypto import decrypt_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

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
    try:
        return await authenticate_control_request(request, "mcp:traffic:write")
    except PermissionError as exc:
        if str(exc).startswith("missing required scope"):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        raise HTTPException(status_code=401, detail="control-plane authentication failed") from exc


def _traffic_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def _require_activity_writer(request: Request) -> str:
    """Authenticate the plugin Activity writer with its dedicated scope."""
    try:
        return await authenticate_control_request(request, "mcp:activity:write")
    except PermissionError as exc:
        if str(exc).startswith("missing required scope"):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        raise HTTPException(status_code=401, detail="control-plane authentication failed") from exc


def _activity_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


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


# ── Endpoints ────────────────────────────────────────────────────────────────


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
