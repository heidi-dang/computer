"""Owner-scoped Workbench Session control API used by the ChatGPT plugin."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.models import AutonomousMonitor, ControlTask, Workspace
from cptr.services.control_auth import require_control_user
from cptr.services.workbench_sessions import MAX_EVENT_LIST_LIMIT, workbench_session_store
from cptr.utils.db import get_db
from cptr.utils.tools import get_command_session

router = APIRouter(prefix="/api/control/v1", tags=["workbench-sessions"])


class CreateWorkbenchSessionRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    workspace_id: str | None = Field(default=None, max_length=200)


class RenameWorkbenchSessionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class BindWorkbenchSessionTargetRequest(BaseModel):
    target_type: Literal["task", "command", "monitor"]
    target_id: str = Field(min_length=1, max_length=200)
    workspace_id: str | None = Field(default=None, max_length=200)


class AppendWorkbenchSessionEventRequest(BaseModel):
    event_type: str = Field(default="mcp.tool.activity", min_length=1, max_length=120)
    summary: str = Field(default="CPTR plugin activity", min_length=1, max_length=4_000)
    state: str | None = Field(default=None, max_length=80)
    target_type: Literal["task", "command", "monitor"] | None = None
    target_id: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_id: str | None = Field(default=None, max_length=200)
    tool_name: str | None = Field(default=None, max_length=160)
    details: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class DeleteWorkbenchSessionRequest(BaseModel):
    confirmation_id: str = Field(min_length=16, max_length=200)


async def _user(request: Request, scope: str) -> str:
    return await require_control_user(request, scope)


async def _ensure_workspace_owner(user_id: str, workspace_id: str | None) -> Workspace | None:
    if not workspace_id:
        return None
    async with await get_db() as db:
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != user_id:
        raise HTTPException(status_code=404, detail="workspace not found")
    return workspace


async def _ensure_target_owner(
    user_id: str, target_type: str, target_id: str, workspace_id: str | None
) -> None:
    if target_type == "command":
        if not workspace_id:
            raise HTTPException(
                status_code=422, detail="workspace_id is required for a command target"
            )
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
    if workspace_id and target.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="target not found")


async def _reconcile_terminal_command_if_needed(
    *, user_id: str, command_id: str, workspace_id: str | None
) -> None:
    if not workspace_id:
        return
    command = get_command_session(None, command_id, context={"user_id": user_id})
    if not command or not command.get("done"):
        return
    raw_exit_code = command.get("exit_code")
    exit_code = raw_exit_code if isinstance(raw_exit_code, int) else None
    await workbench_session_store.reconcile_command_terminal(
        owner_id=user_id,
        workspace_id=workspace_id,
        command_id=command_id,
        status="COMPLETE" if exit_code == 0 else "FAILED",
        exit_code=exit_code,
    )


@router.post("/workbench-sessions")
async def create_workbench_session(request: Request, body: CreateWorkbenchSessionRequest):
    user_id = await _user(request, "task:write")
    await _ensure_workspace_owner(user_id, body.workspace_id)
    session = await workbench_session_store.create(
        owner_id=user_id, name=body.name, workspace_id=body.workspace_id
    )
    await workbench_session_store.append_event(
        owner_id=user_id,
        session_id=session["session_id"],
        source="workbench",
        actor="chatgpt_plugin",
        event_type="workbench.opened",
        state="OPEN",
        workspace_id=body.workspace_id,
        summary="CPTR Workbench Session is ready.",
    )
    current = await workbench_session_store.get(owner_id=user_id, session_id=session["session_id"])
    return current or session


@router.get("/workbench-sessions")
async def list_workbench_sessions(
    request: Request, limit: int = 50, include_archived: bool = False
):
    user_id = await _user(request, "task:read")
    return {
        "sessions": await workbench_session_store.list(
            owner_id=user_id,
            limit=max(1, min(limit, MAX_EVENT_LIST_LIMIT)),
            include_archived=include_archived,
        )
    }


@router.get("/workbench-sessions/{session_id}")
async def get_workbench_session(request: Request, session_id: str):
    user_id = await _user(request, "task:read")
    session = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return session


@router.get("/workbench-sessions/{session_id}/events")
async def get_workbench_session_events(
    request: Request,
    session_id: str,
    after_sequence: int = 0,
    limit: int = 100,
):
    user_id = await _user(request, "task:read")
    events = await workbench_session_store.events(
        owner_id=user_id,
        session_id=session_id,
        after_sequence=max(0, after_sequence),
        limit=max(1, min(limit, MAX_EVENT_LIST_LIMIT)),
    )
    if events is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    last_sequence = events[-1]["sequence"] if events else max(0, after_sequence)
    return {"session_id": session_id, "events": events, "last_sequence": last_sequence}


@router.post("/workbench-sessions/{session_id}/bind")
async def bind_workbench_session(
    request: Request, session_id: str, body: BindWorkbenchSessionTargetRequest
):
    user_id = await _user(request, "task:write")
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
    await workbench_session_store.append_event(
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
    if body.target_type == "command":
        await _reconcile_terminal_command_if_needed(
            user_id=user_id,
            command_id=body.target_id,
            workspace_id=body.workspace_id,
        )
    return await workbench_session_store.get(owner_id=user_id, session_id=session_id) or session


@router.post("/workbench-sessions/{session_id}/events")
async def append_workbench_session_event(
    request: Request, session_id: str, body: AppendWorkbenchSessionEventRequest
):
    user_id = await _user(request, "task:write")
    if bool(body.target_type) != bool(body.target_id):
        raise HTTPException(
            status_code=422, detail="target_type and target_id must be supplied together"
        )
    if body.target_type and body.target_id:
        await _ensure_target_owner(user_id, body.target_type, body.target_id, body.workspace_id)
    elif body.workspace_id:
        await _ensure_workspace_owner(user_id, body.workspace_id)
    try:
        event = await workbench_session_store.append_event(
            owner_id=user_id,
            session_id=session_id,
            event_type=body.event_type,
            summary=body.summary,
            state=body.state,
            target_type=body.target_type,
            target_id=body.target_id,
            workspace_id=body.workspace_id,
            tool_name=body.tool_name,
            details=body.details,
            metrics=body.metrics,
            policy=body.policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if event is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    if body.target_type == "command" and body.event_type == "command.started":
        await _reconcile_terminal_command_if_needed(
            user_id=user_id,
            command_id=body.target_id or "",
            workspace_id=body.workspace_id,
        )
    return event


@router.patch("/workbench-sessions/{session_id}")
async def rename_workbench_session(
    request: Request, session_id: str, body: RenameWorkbenchSessionRequest
):
    user_id = await _user(request, "task:write")
    session = await workbench_session_store.rename(
        owner_id=user_id, session_id=session_id, name=body.name
    )
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return session


@router.post("/workbench-sessions/{session_id}/archive")
async def archive_workbench_session(request: Request, session_id: str):
    user_id = await _user(request, "task:write")
    session = await workbench_session_store.archive(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return session


@router.post("/workbench-sessions/{session_id}/delete-request")
async def request_delete_workbench_session(request: Request, session_id: str):
    user_id = await _user(request, "task:write")
    result = await workbench_session_store.request_delete(owner_id=user_id, session_id=session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return result


@router.post("/workbench-sessions/delete-confirm")
async def confirm_delete_workbench_session(request: Request, body: DeleteWorkbenchSessionRequest):
    user_id = await _user(request, "task:write")
    result = await workbench_session_store.confirm_delete(
        owner_id=user_id, confirmation_id=body.confirmation_id
    )
    if result is None:
        raise HTTPException(
            status_code=404, detail="workbench session confirmation not found or expired"
        )
    return result
