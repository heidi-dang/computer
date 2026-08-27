"""Owner-scoped native Plugin page and control-plane Workbench Session APIs."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cptr.models import AutonomousMonitor, ControlTask, Workspace
from cptr.services.live_events import command_target_key, live_event_hub
from cptr.utils.tools import get_command_session
from cptr.utils.redaction import redact_external_text
from cptr.services.control_auth import authenticate_control_request
from cptr.services.workbench_sessions import (
    MAX_EVENT_LIST_LIMIT,
    publish_workbench_session_event,
    workbench_session_hub,
    workbench_session_store,
)
from cptr.utils.db import get_db

router = APIRouter(tags=["plugin-sessions"])


class CreateWorkbenchSessionRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    workspace_id: str | None = Field(default=None, max_length=200)


class BindWorkbenchSessionTargetRequest(BaseModel):
    target_type: str = Field(pattern="^(task|command|monitor)$")
    target_id: str = Field(min_length=1, max_length=200)
    workspace_id: str | None = Field(default=None, max_length=200)


class AppendWorkbenchSessionEventRequest(BaseModel):
    event_type: str = Field(default="mcp.tool.activity", min_length=1, max_length=120)
    state: str | None = Field(default=None, max_length=80)
    target_type: str | None = Field(default=None, pattern="^(task|command|monitor)$")
    target_id: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_id: str | None = Field(default=None, max_length=200)
    tool_name: str | None = Field(default=None, max_length=160)
    summary: str = Field(default="CPTR plugin activity", min_length=1, max_length=4_000)
    details: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class RenameWorkbenchSessionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class DeleteWorkbenchSessionRequest(BaseModel):
    confirmation_id: str = Field(min_length=16, max_length=200)


async def _control_user(request: Request, required_scope: str) -> str:
    try:
        return await authenticate_control_request(request, required_scope)
    except PermissionError as exc:
        message = str(exc)
        raise HTTPException(
            status_code=403 if message.startswith("missing required scope") else 401,
            detail="control-plane access denied",
        ) from exc


def _web_user(request: Request) -> str:
    auth = getattr(getattr(request, "state", None), "auth", None)
    user_id = getattr(auth, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    return str(user_id)


async def _ensure_workspace_owner(user_id: str, workspace_id: str | None) -> None:
    if not workspace_id:
        return
    async with await get_db() as db:
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != user_id:
        raise HTTPException(status_code=404, detail="workspace not found")


async def _ensure_target_owner(
    user_id: str, target_type: str, target_id: str, workspace_id: str | None
) -> None:
    """Require that a target exists and belongs to the current session owner."""
    if target_type == "command":
        if not workspace_id:
            raise HTTPException(status_code=422, detail="workspace_id is required for a command target")
        await _ensure_workspace_owner(user_id, workspace_id)
        command = get_command_session(None, target_id, context={"user_id": user_id})
        live_target = command.get("live_target") if command else None
        if (
            not isinstance(live_target, dict)
            or live_target.get("target_type") != "command"
            or live_target.get("workspace_id") != workspace_id
        ):
            raise HTTPException(status_code=404, detail="target not found")
        return
    model = ControlTask if target_type == "task" else AutonomousMonitor
    async with await get_db() as db:
        target = await db.get(model, target_id)
    if target is None or target.user_id != user_id:
        raise HTTPException(status_code=404, detail="target not found")


def _after_sequence(request: Request, after: int) -> int:
    header = request.headers.get("last-event-id", "")
    candidate = header.rsplit(":", 1)[-1] if header else str(after)
    try:
        return max(0, int(candidate))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid workbench session cursor") from exc


def _sse(envelope: dict) -> str:
    return (
        f"id: {envelope['session_id']}:{envelope['sequence']}\n"
        "event: workbench.session\n"
        f"data: {json.dumps(envelope, separators=(',', ':'), ensure_ascii=False)}\n\n"
    )


async def _terminal_snapshot_and_key(owner_id: str, session: dict) -> tuple[str, dict]:
    """Return an owner-authorized live target key and safe initial projection."""
    target_type = session.get("active_target_type")
    target_id = session.get("active_target_id")
    workspace_id = session.get("active_workspace_id")
    if target_type not in {"task", "command", "monitor"} or not target_id:
        raise HTTPException(status_code=409, detail="workbench session has no active live target")
    if target_type == "command":
        if not workspace_id:
            raise HTTPException(status_code=409, detail="workbench session command target is incomplete")
        await _ensure_workspace_owner(owner_id, workspace_id)
        command = get_command_session(None, target_id, context={"user_id": owner_id})
        if command is None:
            raise HTTPException(status_code=404, detail="target not found")
        return command_target_key(workspace_id, target_id), {
            "target": "command",
            "snapshot": {
                "command_id": target_id,
                "workspace_id": workspace_id,
                "status": "COMPLETE" if command.get("done") else "RUNNING",
                "exit_code": command.get("exit_code"),
            },
        }
    model = ControlTask if target_type == "task" else AutonomousMonitor
    async with await get_db() as db:
        target = await db.get(model, target_id)
    if target is None or target.user_id != owner_id:
        raise HTTPException(status_code=404, detail="target not found")
    if target_type == "task":
        snapshot = {
            "id": target.id,
            "workspace_id": target.workspace_id,
            "status": target.status,
            "error": redact_external_text(str(target.error or "")) or None,
            "created_at": target.created_at,
            "updated_at": target.updated_at,
        }
    else:
        snapshot = {
            "monitor_id": target.id,
            "workspace_id": target.workspace_id,
            "status": target.status,
            "current_scope": target.current_scope_id,
        }
    return f"{target_type}:{target_id}", {"target": target_type, "snapshot": snapshot}


async def _terminal_stream(
    request: Request, *, target_key: str, snapshot: dict, after_sequence: int
) -> AsyncIterator[str]:
    yield "event: snapshot\nid: 0\ndata: " + json.dumps(snapshot, separators=(",", ":")) + "\n\n"
    iterator = live_event_hub.subscribe(target_key, after_sequence=after_sequence).__aiter__()
    try:
        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=20)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:
                return
            payload = event.to_dict()
            yield (
                f"id: {event.sequence}\n"
                f"event: {event.event_type}\n"
                f"data: {json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}\n\n"
            )
            status = str(event.payload.get("status", "")).upper()
            if status in {"COMPLETE", "COMPLETE_WITH_TOOL_ERRORS", "FAILED", "BLOCKED", "CANCELLED", "REJECTED"} or event.event_type.endswith(".terminal"):
                return
    finally:
        await iterator.aclose()


async def _session_stream(
    request: Request, owner_id: str, session_id: str, after_sequence: int
) -> AsyncIterator[str]:
    # Subscribe before replay so a concurrent append cannot fall into a gap.
    iterator = workbench_session_hub.subscribe(owner_id).__aiter__()
    replay = await workbench_session_store.events(
        owner_id=owner_id,
        session_id=session_id,
        after_sequence=after_sequence,
        limit=MAX_EVENT_LIST_LIMIT,
    )
    if replay is None:
        return
    last_sequence = after_sequence
    for event in replay:
        sequence = int(event["sequence"])
        if sequence > last_sequence:
            last_sequence = sequence
            yield _sse(event)
    # Keep the connection observable on mobile proxies even when a selected
    # session has no new work. The browser's reducer ignores comments.
    try:
        while True:
            if await request.is_disconnected():
                return
            try:
                envelope = await asyncio.wait_for(iterator.__anext__(), timeout=20)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:
                return
            if envelope.session_id != session_id or envelope.sequence <= last_sequence:
                continue
            last_sequence = envelope.sequence
            yield _sse(envelope.event)
    finally:
        await iterator.aclose()


# ----- Control-plane API used only by the server-side ChatGPT plugin -----


@router.post("/api/control/v1/workbench-sessions")
async def create_control_workbench_session(request: Request, body: CreateWorkbenchSessionRequest):
    user_id = await _control_user(request, "task:write")
    await _ensure_workspace_owner(user_id, body.workspace_id)
    session = await workbench_session_store.create(
        owner_id=user_id, name=body.name, workspace_id=body.workspace_id
    )
    await publish_workbench_session_event(
        owner_id=user_id,
        session_id=session["session_id"],
        source="workbench",
        actor="chatgpt_plugin",
        event_type="workbench.opened",
        state="OPEN",
        workspace_id=body.workspace_id,
        summary="CPTR Workbench Session is ready.",
    )
    return session


@router.get("/api/control/v1/workbench-sessions")
async def list_control_workbench_sessions(request: Request, limit: int = 50, include_archived: bool = False):
    user_id = await _control_user(request, "task:read")
    return {"sessions": await workbench_session_store.list(owner_id=user_id, limit=limit, include_archived=include_archived)}


@router.get("/api/control/v1/workbench-sessions/{session_id}")
async def get_control_workbench_session(request: Request, session_id: str):
    user_id = await _control_user(request, "task:read")
    session = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return session


@router.get("/api/control/v1/workbench-sessions/{session_id}/events")
async def get_control_workbench_session_events(
    request: Request, session_id: str, after_sequence: int = 0, limit: int = 100
):
    user_id = await _control_user(request, "task:read")
    events = await workbench_session_store.events(
        owner_id=user_id, session_id=session_id, after_sequence=after_sequence, limit=limit
    )
    if events is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return {"session_id": session_id, "events": events}


@router.post("/api/control/v1/workbench-sessions/{session_id}/bind")
async def bind_control_workbench_session(
    request: Request, session_id: str, body: BindWorkbenchSessionTargetRequest
):
    user_id = await _control_user(request, "task:write")
    await _ensure_target_owner(user_id, body.target_type, body.target_id, body.workspace_id)
    session = await workbench_session_store.bind_target(
        owner_id=user_id,
        session_id=session_id,
        target_type=body.target_type,
        target_id=body.target_id,
        workspace_id=body.workspace_id,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    await publish_workbench_session_event(
        owner_id=user_id,
        session_id=session_id,
        source="workbench",
        actor="chatgpt_plugin",
        event_type="workbench.target.bound",
        state=session["status"],
        target_type=body.target_type,
        target_id=body.target_id,
        workspace_id=body.workspace_id,
        summary=f"Workbench bound to {body.target_type} activity.",
    )
    return session


@router.post("/api/control/v1/workbench-sessions/{session_id}/events")
async def append_control_workbench_session_event(
    request: Request, session_id: str, body: AppendWorkbenchSessionEventRequest
):
    """Ingest a bounded plugin event after validating all referenced owner targets."""
    user_id = await _control_user(request, "task:write")
    if bool(body.target_type) != bool(body.target_id):
        raise HTTPException(status_code=422, detail="target_type and target_id must be supplied together")
    if body.target_type and body.target_id:
        await _ensure_target_owner(user_id, body.target_type, body.target_id, body.workspace_id)
    elif body.workspace_id:
        await _ensure_workspace_owner(user_id, body.workspace_id)
    envelope = await publish_workbench_session_event(
        owner_id=user_id,
        session_id=session_id,
        source="plugin",
        actor="chatgpt_plugin",
        event_type=body.event_type,
        state=body.state,
        target_type=body.target_type,
        target_id=body.target_id,
        workspace_id=body.workspace_id,
        tool_name=body.tool_name,
        summary=body.summary,
        details=body.details,
        metrics=body.metrics,
        policy=body.policy,
    )
    if envelope is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return envelope.event


@router.post("/api/control/v1/workbench-sessions/{session_id}/rename")
async def rename_control_workbench_session(
    request: Request, session_id: str, body: RenameWorkbenchSessionRequest
):
    user_id = await _control_user(request, "task:write")
    session = await workbench_session_store.rename(owner_id=user_id, session_id=session_id, name=body.name)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    await publish_workbench_session_event(
        owner_id=user_id,
        session_id=session_id,
        source="system",
        actor="user",
        event_type="session.renamed",
        state=session["status"],
        summary="Workbench Session was renamed.",
    )
    return session


@router.post("/api/control/v1/workbench-sessions/{session_id}/archive")
async def archive_control_workbench_session(request: Request, session_id: str):
    user_id = await _control_user(request, "task:write")
    session = await workbench_session_store.archive(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return session


@router.post("/api/control/v1/workbench-sessions/{session_id}/delete-request")
async def request_delete_control_workbench_session(request: Request, session_id: str):
    user_id = await _control_user(request, "task:write")
    result = await workbench_session_store.request_delete(owner_id=user_id, session_id=session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return result


@router.post("/api/control/v1/workbench-sessions/delete-confirm")
async def confirm_delete_control_workbench_session(
    request: Request, body: DeleteWorkbenchSessionRequest
):
    user_id = await _control_user(request, "task:write")
    result = await workbench_session_store.confirm_delete(
        owner_id=user_id, confirmation_id=body.confirmation_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return result


# ----- Native CPTR Plugin page API using normal CPTR web-session identity -----


@router.get("/api/plugin/v1/sessions")
async def list_plugin_sessions(request: Request, limit: int = 50, include_archived: bool = False):
    user_id = _web_user(request)
    return {"sessions": await workbench_session_store.list(owner_id=user_id, limit=limit, include_archived=include_archived)}


@router.get("/api/plugin/v1/sessions/{session_id}")
async def get_plugin_session(request: Request, session_id: str):
    user_id = _web_user(request)
    session = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return session


@router.get("/api/plugin/v1/sessions/{session_id}/events")
async def get_plugin_session_events(
    request: Request, session_id: str, after_sequence: int = 0, limit: int = 100
):
    user_id = _web_user(request)
    events = await workbench_session_store.events(
        owner_id=user_id, session_id=session_id, after_sequence=after_sequence, limit=limit
    )
    if events is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return {"session_id": session_id, "events": events}


@router.get("/api/plugin/v1/sessions/{session_id}/stream")
async def stream_plugin_session(request: Request, session_id: str, after_sequence: int = 0):
    user_id = _web_user(request)
    session = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    after = _after_sequence(request, after_sequence)
    return StreamingResponse(
        _session_stream(request, user_id, session_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/plugin/v1/sessions/{session_id}/terminal/stream")
async def stream_plugin_session_terminal(request: Request, session_id: str, after_sequence: int = 0):
    user_id = _web_user(request)
    session = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    target_key, snapshot = await _terminal_snapshot_and_key(user_id, session)
    return StreamingResponse(
        _terminal_stream(
            request,
            target_key=target_key,
            snapshot=snapshot,
            after_sequence=_after_sequence(request, after_sequence),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/api/plugin/v1/sessions/{session_id}/rename")
async def rename_plugin_session(request: Request, session_id: str, body: RenameWorkbenchSessionRequest):
    user_id = _web_user(request)
    session = await workbench_session_store.rename(owner_id=user_id, session_id=session_id, name=body.name)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    await publish_workbench_session_event(
        owner_id=user_id,
        session_id=session_id,
        source="system",
        actor="user",
        event_type="session.renamed",
        state=session["status"],
        summary="Workbench Session was renamed.",
    )
    return session


@router.post("/api/plugin/v1/sessions/{session_id}/archive")
async def archive_plugin_session(request: Request, session_id: str):
    user_id = _web_user(request)
    session = await workbench_session_store.archive(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return session


@router.post("/api/plugin/v1/sessions/{session_id}/delete-request")
async def request_delete_plugin_session(request: Request, session_id: str):
    user_id = _web_user(request)
    result = await workbench_session_store.request_delete(owner_id=user_id, session_id=session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return result


@router.post("/api/plugin/v1/sessions/delete-confirm")
async def confirm_delete_plugin_session(request: Request, body: DeleteWorkbenchSessionRequest):
    user_id = _web_user(request)
    result = await workbench_session_store.confirm_delete(
        owner_id=user_id, confirmation_id=body.confirmation_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return result
