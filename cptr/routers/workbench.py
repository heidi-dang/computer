"""Owner-scoped Workbench Session control API used by the ChatGPT plugin."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.models import AutonomousMonitor, ControlTask, Workspace
from cptr.services.action_traces import action_trace_store, trace_context_from_request
from cptr.services.control_auth import require_control_user, require_owner_session_or_control_user
from cptr.services.local_root_grants import local_root_grant_store, local_root_grants_enabled
from cptr.services.privilege_broker import (
    AdminSessionGrantDenied,
    DEFAULT_ADMIN_TTL_SECONDS,
    MAX_ADMIN_TTL_SECONDS,
    admin_session_grant_store,
    privilege_broker,
)
from cptr.services.workbench_sessions import MAX_EVENT_LIST_LIMIT, workbench_session_store
from cptr.utils.db import get_db
from cptr.utils.tools import get_command_session

router = APIRouter(prefix="/api/control/v1", tags=["workbench-sessions"])


class CreateWorkbenchSessionRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    workspace_id: str | None = Field(default=None, max_length=200)
    workspace_ref: str | None = Field(default=None, max_length=200)
    environment_profile_id: str | None = Field(default=None, max_length=200)
    environment_profile_ref: str | None = Field(default=None, max_length=200)
    environment_profile_override: dict[str, Any] | None = Field(default=None)
    admin_role: str | None = Field(default=None, max_length=120)
    admin_role_ref: str | None = Field(default=None, max_length=120)
    role_context: dict[str, Any] | None = Field(default=None)
    last_context_snapshot_id: str | None = Field(default=None, max_length=200)


class RenameWorkbenchSessionRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    workspace_id: str | None = Field(default=None, max_length=200)
    workspace_ref: str | None = Field(default=None, max_length=200)
    environment_profile_id: str | None = Field(default=None, max_length=200)
    environment_profile_ref: str | None = Field(default=None, max_length=200)
    environment_profile_override: dict[str, Any] | None = Field(default=None)
    admin_role: str | None = Field(default=None, max_length=120)
    admin_role_ref: str | None = Field(default=None, max_length=120)
    role_context: dict[str, Any] | None = Field(default=None)
    last_context_snapshot_id: str | None = Field(default=None, max_length=200)


UpdateWorkbenchSessionRequest = RenameWorkbenchSessionRequest


class BindWorkbenchSessionTargetRequest(BaseModel):
    target_type: Literal["task", "command", "monitor"]
    target_id: str = Field(min_length=1, max_length=200)
    workspace_id: str | None = Field(default=None, max_length=200)
    workspace_ref: str | None = Field(default=None, max_length=200)
    last_context_snapshot_id: str | None = Field(default=None, max_length=200)
    make_sticky: bool = Field(default=False)


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


class AdminGrantRequest(BaseModel):
    ttl_seconds: int = Field(
        default=DEFAULT_ADMIN_TTL_SECONDS,
        ge=1,
        le=MAX_ADMIN_TTL_SECONDS,
    )


async def _user(request: Request, scope: str) -> str:
    return await require_control_user(request, scope)


async def _ui_user(request: Request, scope: str) -> str:
    return await require_owner_session_or_control_user(request, scope)


async def _ensure_workspace_owner(user_id: str, workspace_id: str | None) -> Workspace | None:
    if not workspace_id:
        return None
    try:
        from cptr.services.workspace_refs import resolve_workspace_ref

        resolution = await resolve_workspace_ref(user_id=user_id, reference=workspace_id)
        return resolution.workspace
    except Exception:
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
        from cptr.routers.control import _are_workspaces_equivalent

        if not await _are_workspaces_equivalent(user_id, str(target.workspace_id), workspace_id):
            raise HTTPException(status_code=404, detail="target not found")


async def _trace_workbench_event(
    request: Request,
    *,
    user_id: str,
    session_id: str,
    event_type: str,
    status: str,
    target_type: str | None = None,
    target_id: str | None = None,
    workspace_id: str | None = None,
    tool_name: str | None = None,
) -> None:
    """Join Workbench lifecycle to an existing command trace or the current MCP trace."""
    try:
        context = trace_context_from_request(request)
        trace_id = context.trace_id if context is not None else None
        if target_type == "command" and target_id:
            command_trace = await action_trace_store.resolve_entity(
                owner_id=user_id,
                entity_type="command",
                entity_id=target_id,
            )
            trace_id = command_trace or trace_id
        if trace_id is None:
            return
        await action_trace_store.link_entity(
            owner_id=user_id,
            trace_id=trace_id,
            entity_type="workbench",
            entity_id=session_id,
        )
        normalized = str(status or "").upper()
        trace_status = (
            "error"
            if normalized in {"ERROR", "FAILED", "FAILURE"}
            else "ok"
            if normalized in {"COMPLETE", "COMPLETED", "SUCCEEDED", "ARCHIVED"}
            else "running"
        )
        await action_trace_store.append(
            owner_id=user_id,
            trace_id=trace_id,
            layer="workbench",
            name=event_type,
            status=trace_status,
            request_id=context.request_id if context is not None else None,
            tool_name=tool_name or (context.tool_name if context is not None else None),
            workspace_id=workspace_id,
            entity_type="workbench",
            entity_id=session_id,
            dedupe_key=f"workbench:{session_id}:{event_type}:{target_id or ''}",
        )
    except Exception:
        return


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
    target_ref = body.workspace_ref or body.workspace_id
    workspace = await _ensure_workspace_owner(user_id, target_ref) if target_ref else None
    canonical_ws = getattr(workspace, "id", None) or target_ref
    env_profile = body.environment_profile_id or body.environment_profile_ref
    admin_role_val = body.admin_role or body.admin_role_ref
    create_kwargs: dict[str, Any] = {
        "owner_id": user_id,
        "name": body.name,
        "workspace_id": canonical_ws,
    }
    if env_profile is not None:
        create_kwargs["environment_profile_id"] = env_profile
    if body.environment_profile_override is not None:
        create_kwargs["environment_profile_override"] = body.environment_profile_override
    if admin_role_val is not None:
        create_kwargs["admin_role"] = admin_role_val
    if body.role_context is not None:
        create_kwargs["role_context"] = body.role_context
    if body.last_context_snapshot_id is not None:
        create_kwargs["last_context_snapshot_id"] = body.last_context_snapshot_id

    session = await workbench_session_store.create(**create_kwargs)
    await workbench_session_store.append_event(
        owner_id=user_id,
        session_id=session["session_id"],
        source="workbench",
        actor="chatgpt_plugin",
        event_type="workbench.opened",
        state="OPEN",
        workspace_id=canonical_ws,
        summary="CPTR Workbench Session is ready.",
    )
    await _trace_workbench_event(
        request,
        user_id=user_id,
        session_id=session["session_id"],
        event_type="workbench.opened",
        status="OPEN",
        workspace_id=canonical_ws,
    )
    current = await workbench_session_store.get(owner_id=user_id, session_id=session["session_id"])
    return current or session


@router.get("/workbench-sessions")
async def list_workbench_sessions(
    request: Request, limit: int = 50, include_archived: bool = False
):
    user_id = await _ui_user(request, "task:read")
    return {
        "sessions": await workbench_session_store.list(
            owner_id=user_id,
            limit=max(1, min(limit, MAX_EVENT_LIST_LIMIT)),
            include_archived=include_archived,
        )
    }


@router.get("/workbench-sessions/{session_id}")
async def get_workbench_session(request: Request, session_id: str):
    user_id = await _ui_user(request, "task:read")
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
    user_id = await _ui_user(request, "task:read")
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
    target_ws = body.workspace_ref or body.workspace_id
    if not target_ws:
        existing = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
        if existing and existing.get("workspace_id"):
            target_ws = existing["workspace_id"]
    canonical_ws = target_ws
    if body.workspace_ref:
        workspace = await _ensure_workspace_owner(user_id, body.workspace_ref)
        canonical_ws = getattr(workspace, "id", None) or body.workspace_ref
    await _ensure_target_owner(user_id, body.target_type, body.target_id, canonical_ws)
    bind_kwargs: dict[str, Any] = {
        "owner_id": user_id,
        "session_id": session_id,
        "target_type": body.target_type,
        "target_id": body.target_id,
        "workspace_id": canonical_ws,
    }
    if body.last_context_snapshot_id is not None:
        bind_kwargs["last_context_snapshot_id"] = body.last_context_snapshot_id
    if body.make_sticky:
        bind_kwargs["make_sticky"] = True

    session = await workbench_session_store.bind_target(**bind_kwargs)
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
        workspace_id=canonical_ws,
        summary=f"Workbench bound to {body.target_type} activity.",
    )
    await _trace_workbench_event(
        request,
        user_id=user_id,
        session_id=session_id,
        event_type="workbench.target.bound",
        status=session["status"],
        target_type=body.target_type,
        target_id=body.target_id,
        workspace_id=canonical_ws,
    )
    if body.target_type == "command":
        await _reconcile_terminal_command_if_needed(
            user_id=user_id,
            command_id=body.target_id,
            workspace_id=canonical_ws,
        )
    return await workbench_session_store.get(owner_id=user_id, session_id=session_id) or session


@router.post("/workbench-sessions/{session_id}/events")
async def append_workbench_session_event(
    request: Request, session_id: str, body: AppendWorkbenchSessionEventRequest
):
    user_id = await _user(request, "task:write")
    target_ws = body.workspace_id
    if not target_ws:
        existing = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
        if existing:
            target_ws = existing.get("active_workspace_id") or existing.get("workspace_id")
    canonical_ws = target_ws
    if bool(body.target_type) != bool(body.target_id):
        raise HTTPException(
            status_code=422, detail="target_type and target_id must be supplied together"
        )
    if body.target_type and body.target_id:
        await _ensure_target_owner(user_id, body.target_type, body.target_id, canonical_ws)
    elif canonical_ws:
        workspace = await _ensure_workspace_owner(user_id, canonical_ws)
        canonical_ws = getattr(workspace, "id", None) or canonical_ws
    try:
        event = await workbench_session_store.append_event(
            owner_id=user_id,
            session_id=session_id,
            event_type=body.event_type,
            summary=body.summary,
            state=body.state,
            target_type=body.target_type,
            target_id=body.target_id,
            workspace_id=canonical_ws,
            tool_name=body.tool_name,
            details=body.details,
            metrics=body.metrics,
            policy=body.policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if event is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    await _trace_workbench_event(
        request,
        user_id=user_id,
        session_id=session_id,
        event_type=body.event_type,
        status=body.state or "RUNNING",
        target_type=body.target_type,
        target_id=body.target_id,
        workspace_id=canonical_ws,
        tool_name=body.tool_name,
    )
    if body.target_type == "command" and body.event_type == "command.started":
        await _reconcile_terminal_command_if_needed(
            user_id=user_id,
            command_id=body.target_id or "",
            workspace_id=canonical_ws,
        )
    return event


@router.patch("/workbench-sessions/{session_id}")
async def rename_workbench_session(
    request: Request, session_id: str, body: RenameWorkbenchSessionRequest
):
    user_id = await _ui_user(request, "task:write")
    has_extended = any(
        getattr(body, k, None) is not None
        for k in (
            "workspace_id",
            "workspace_ref",
            "environment_profile_id",
            "environment_profile_ref",
            "environment_profile_override",
            "admin_role",
            "admin_role_ref",
            "role_context",
            "last_context_snapshot_id",
        )
    )
    if not has_extended and body.name is not None:
        session = await workbench_session_store.rename(
            owner_id=user_id, session_id=session_id, name=body.name
        )
        if session is None:
            raise HTTPException(status_code=404, detail="workbench session not found")
        return session

    ws_ref = body.workspace_ref or body.workspace_id
    canonical_ws = None
    if ws_ref:
        workspace = await _ensure_workspace_owner(user_id, ws_ref)
        canonical_ws = getattr(workspace, "id", None) or ws_ref

    from cptr.services.workbench_sessions import _UNSET

    session = await workbench_session_store.update(
        owner_id=user_id,
        session_id=session_id,
        name=body.name,
        workspace_id=canonical_ws if (body.workspace_id or body.workspace_ref) else _UNSET,
        environment_profile_id=(body.environment_profile_id or body.environment_profile_ref)
        if (body.environment_profile_id or body.environment_profile_ref) is not None
        else _UNSET,
        environment_profile_override=body.environment_profile_override
        if body.environment_profile_override is not None
        else _UNSET,
        admin_role=(body.admin_role or body.admin_role_ref)
        if (body.admin_role or body.admin_role_ref) is not None
        else _UNSET,
        role_context=body.role_context if body.role_context is not None else _UNSET,
        last_context_snapshot_id=body.last_context_snapshot_id
        if body.last_context_snapshot_id is not None
        else _UNSET,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    return session


@router.post("/workbench-sessions/{session_id}/admin-grant")
async def grant_workbench_session_admin(
    request: Request,
    session_id: str,
    body: AdminGrantRequest = AdminGrantRequest(),
):
    user_id = await _ui_user(request, "task:write")
    try:
        return await admin_session_grant_store.grant(
            owner_id=user_id,
            session_id=session_id,
            ttl_seconds=body.ttl_seconds,
            actor="control_api",
        )
    except (AdminSessionGrantDenied, ValueError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/workbench-sessions/{session_id}/admin-revoke")
async def revoke_workbench_session_admin(request: Request, session_id: str):
    user_id = await _ui_user(request, "task:write")
    try:
        count = await admin_session_grant_store.revoke(
            owner_id=user_id,
            session_id=session_id,
            actor="control_api",
        )
    except AdminSessionGrantDenied as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "session_id": session_id,
        "revoked": count > 0,
        "revoked_count": count,
    }


@router.get("/workbench-sessions/{session_id}/privilege")
async def get_workbench_session_privilege(request: Request, session_id: str):
    user_id = await _ui_user(request, "task:read")
    session = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")

    admin_status = await admin_session_grant_store.status(
        owner_id=user_id,
        session_id=session_id,
    )
    root_active = bool(
        local_root_grants_enabled()
        and await local_root_grant_store.is_active(
            owner_id=user_id,
            session_id=session_id,
        )
    )
    return {
        "session_id": session_id,
        "privilege": await privilege_broker.resolve_privilege(
            owner_id=user_id,
            session_id=session_id,
        ),
        "admin_grant": admin_status,
        "local_root_grant_active": root_active,
    }


@router.post("/workbench-sessions/{session_id}/archive")
async def archive_workbench_session(request: Request, session_id: str):
    user_id = await _ui_user(request, "task:write")
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
