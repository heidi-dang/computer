"""Authenticated snapshot/replay/live SSE streams for the CPTR Live Workbench."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from cptr.routers.coding import _coding_root, _command_snapshot, _workspace
from cptr.routers.control import _services, _user
from cptr.services.live_events import (
    LiveEventEnvelope,
    command_target_key,
    live_event_hub,
    workbench_target_key,
)
from cptr.services.workbench_sessions import workbench_session_store
from cptr.utils.redaction import redact_external_text

router = APIRouter(prefix="/api/control/v1", tags=["control-live"])
STREAM_HEARTBEAT_SECONDS = 15.0
TERMINAL_STATUSES = {
    "COMPLETE",
    "COMPLETE_WITH_TOOL_ERRORS",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "REVIEW_REQUIRED",
    "REJECTED",
}


def _task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    """Expose only the live status projection; never stream prompt/raw output."""
    return {
        key: (
            redact_external_text(task[key])
            if key == "error" and isinstance(task[key], str)
            else task[key]
        )
        for key in ("id", "workspace_id", "status", "error", "created_at", "updated_at")
        if key in task
    }


def _workbench_snapshot(session: dict[str, Any]) -> dict[str, Any]:
    return {
        key: session.get(key)
        for key in (
            "session_id",
            "name",
            "workspace_id",
            "status",
            "active_target_type",
            "active_target_id",
            "active_workspace_id",
        )
    }


async def _owned_workbench_snapshot(request: Request, session_id: str) -> dict[str, Any]:
    user_id = await _user(request, "task:read")
    session = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    if str(session.get("status") or "").upper() == "ARCHIVED":
        raise HTTPException(status_code=409, detail="workbench session is archived")
    return _workbench_snapshot(session)


def _monitor_snapshot(monitor: Any) -> dict[str, Any]:
    return {
        "monitor_id": monitor.monitor_id,
        "workspace_id": monitor.workspace_id,
        "status": monitor.status.value,
        "scope_count": len(monitor.scopes),
        "verified_count": sum(item.status.value == "VERIFIED" for item in monitor.scopes),
        "current_scope": monitor.current_scope_id,
        "scopes": [
            {
                "scope_id": item.scope_id,
                "status": item.status.value,
                "attempt_count": item.attempt_count,
                "worker_task_ids": list(item.worker_task_ids),
            }
            for item in monitor.scopes
        ],
    }


def _sse(*, event: str, event_id: str, data: dict[str, Any]) -> str:
    return f"event: {event}\nid: {event_id}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _event_name(event: LiveEventEnvelope) -> str:
    return event.event_type


async def _stream(
    request: Request,
    *,
    target_key: str,
    snapshot: dict[str, Any],
    after_sequence: int,
    stop_on_terminal: bool = True,
) -> AsyncIterator[str]:
    yield _sse(event="snapshot", event_id="0", data=snapshot)
    snapshot_value = snapshot.get("snapshot")
    if (
        stop_on_terminal
        and isinstance(snapshot_value, dict)
        and str(snapshot_value.get("status", "")).upper() in TERMINAL_STATUSES
    ):
        return
    iterator = live_event_hub.subscribe(target_key, after_sequence=after_sequence).__aiter__()
    pending: asyncio.Task[LiveEventEnvelope] | None = asyncio.create_task(iterator.__anext__())
    try:
        while True:
            if await request.is_disconnected():
                return
            assert pending is not None
            done, _ = await asyncio.wait({pending}, timeout=STREAM_HEARTBEAT_SECONDS)
            if not done:
                yield ": heartbeat\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None

            yield _sse(event=_event_name(event), event_id=str(event.sequence), data=event.to_dict())
            status = str(event.payload.get("status", "")).upper()
            if stop_on_terminal and (
                status in TERMINAL_STATUSES or event.event_type.endswith(".terminal")
            ):
                return
            pending = asyncio.create_task(iterator.__anext__())
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        await iterator.aclose()


def _after_sequence(request: Request) -> int:
    value = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        return max(0, int(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid live-event cursor") from exc


async def _recovery_snapshot(*, target_key: str, target: str, snapshot: dict[str, Any], after: int):
    replay = await live_event_hub.store.snapshot(target_key, after_sequence=after)
    return {
        "version": 1,
        "target": target,
        "snapshot": snapshot,
        "replay": replay,
    }


@router.get("/workbench-sessions/{session_id}/stream/snapshot")
async def workbench_stream_snapshot(request: Request, session_id: str):
    snapshot = await _owned_workbench_snapshot(request, session_id)
    return await _recovery_snapshot(
        target_key=workbench_target_key(session_id),
        target="workbench",
        snapshot=snapshot,
        after=_after_sequence(request),
    )


@router.get("/workbench-sessions/{session_id}/stream")
async def workbench_stream(request: Request, session_id: str):
    snapshot = await _owned_workbench_snapshot(request, session_id)
    return StreamingResponse(
        _stream(
            request,
            target_key=workbench_target_key(session_id),
            snapshot={"target": "workbench", "snapshot": snapshot},
            after_sequence=_after_sequence(request),
            stop_on_terminal=False,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/tasks/{task_id}/stream/snapshot")
async def task_stream_snapshot(request: Request, task_id: str):
    user_id = await _user(request, "task:read")
    agent, _ = _services(request)
    try:
        task = await agent.get_task(task_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return await _recovery_snapshot(
        target_key=f"task:{task_id}",
        target="task",
        snapshot=_task_snapshot(task),
        after=_after_sequence(request),
    )


@router.get("/tasks/{task_id}/stream")
async def task_stream(request: Request, task_id: str):
    user_id = await _user(request, "task:read")
    agent, _ = _services(request)
    try:
        snapshot = await agent.get_task(task_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return StreamingResponse(
        _stream(
            request,
            target_key=f"task:{task_id}",
            snapshot={"target": "task", "snapshot": _task_snapshot(snapshot)},
            after_sequence=_after_sequence(request),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _direct_command_live_snapshot(
    request: Request,
    *,
    workspace_id: str,
    command_id: str,
    worker_id: str | None = None,
) -> dict[str, Any]:
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, worker_id)
    command = await _command_snapshot(
        request,
        workspace_path=str(root),
        command_id=command_id,
    )
    # The replay stream is the authoritative terminal-output surface. Keep the
    # point-in-time snapshot to lifecycle fields so raw command output cannot
    # bypass live-event redaction/sanitization.
    return {
        "command_id": command_id,
        "workspace_id": workspace_id,
        "status": command.get("status", "RUNNING"),
        "exit_code": command.get("exit_code"),
    }


@router.get("/workspaces/{workspace_id}/coding/commands/{command_id}/stream/snapshot")
async def command_stream_snapshot(
    request: Request,
    workspace_id: str,
    command_id: str,
    worker_id: str | None = None,
):
    snapshot = await _direct_command_live_snapshot(
        request,
        workspace_id=workspace_id,
        command_id=command_id,
        worker_id=worker_id,
    )
    return await _recovery_snapshot(
        target_key=command_target_key(workspace_id, command_id),
        target="command",
        snapshot=snapshot,
        after=_after_sequence(request),
    )


@router.get("/workspaces/{workspace_id}/coding/commands/{command_id}/stream")
async def command_stream(
    request: Request,
    workspace_id: str,
    command_id: str,
    worker_id: str | None = None,
):
    snapshot = await _direct_command_live_snapshot(
        request,
        workspace_id=workspace_id,
        command_id=command_id,
        worker_id=worker_id,
    )
    return StreamingResponse(
        _stream(
            request,
            target_key=command_target_key(workspace_id, command_id),
            snapshot={"target": "command", "snapshot": snapshot},
            after_sequence=_after_sequence(request),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/autonomous/{monitor_id}/stream/snapshot")
async def monitor_stream_snapshot(request: Request, monitor_id: str):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    monitor = await supervisor.store.get_monitor(monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    return await _recovery_snapshot(
        target_key=f"monitor:{monitor_id}",
        target="monitor",
        snapshot=_monitor_snapshot(monitor),
        after=_after_sequence(request),
    )


@router.get("/autonomous/{monitor_id}/stream")
async def monitor_stream(request: Request, monitor_id: str):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    monitor = await supervisor.store.get_monitor(monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    return StreamingResponse(
        _stream(
            request,
            target_key=f"monitor:{monitor_id}",
            snapshot={"target": "monitor", "snapshot": _monitor_snapshot(monitor)},
            after_sequence=_after_sequence(request),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
