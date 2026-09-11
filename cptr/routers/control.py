"""Versioned CPTR Control API for MCP and other automation clients."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cptr.memory.domain import RetrievalFeedback
from cptr.memory.mcp_adapter import MemoryMcpAdapter
from cptr.memory.service import MemoryUnavailableError
from cptr.models import Workspace, ControlTask, Config, AutonomousMonitor, ControlIdempotency
from cptr.services.workspace_actions import (
    READ_ACTIONS as WORKSPACE_READ_ACTIONS,
    WRITE_ACTIONS as WORKSPACE_WRITE_ACTIONS,
    WorkspaceActionError,
    workspace_action_service,
)
from cptr.services.workspace_availability import is_workspace_available
from cptr.services.workbench_sessions import workbench_session_store
from cptr.routers.state import _resolve_request_workspace_path
from cptr.services.agent_service import AgentService
from cptr.services.control_auth import require_control_user
from cptr.services.control_store import SqlSupervisorStore
from cptr.services.direct_coding_workers import DirectCodingWorkerError, resolve_direct_worker_root
from cptr.services.guard_controls import guard_policy_service
from cptr.services.supervisor import AutonomousSupervisor, MonitorState, MonitorStatus
from cptr.services.supervisor_director import LocalSupervisorDirector, OpenAISupervisorDirector
from cptr.utils.db import get_db
from cptr.utils.git import init_repo, is_repo
from cptr.utils.identity import identity_for_request
from cptr.utils.redaction import redact_external, redact_sensitive
from cptr.utils.runtime import FileError, Runtime

router = APIRouter(prefix="/api/control/v1", tags=["control"])


async def _default_model() -> str | None:
    value = await Config.get("chat.default_model")
    return str(value).strip() if value else None


_DELEGATION_MARKER_RE = re.compile(r"(?<![\w:])allow:delegate(?![\w:])", re.IGNORECASE)


def _is_qualified_model_id(model_id: str) -> bool:
    candidate = model_id.strip()
    if candidate.startswith("agent:"):
        profile_and_model = candidate[len("agent:") :]
        return (
            "/" in profile_and_model
            and not profile_and_model.startswith("/")
            and not profile_and_model.endswith("/")
        )
    return "/" in candidate and not candidate.startswith("/") and not candidate.endswith("/")


def _require_delegation_marker(delegation_text: str) -> None:
    if not _DELEGATION_MARKER_RE.search(delegation_text):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "DELEGATION_NOT_ALLOWED",
                "message": "delegated CPTR/model execution is disabled by default; the user prompt must contain the exact token allow:delegate",
                "retriable": False,
                "field": "prompt",
            },
        )


def _require_explicit_delegation(
    model_id: str | None, delegation_text: str, *, require_marker: bool = True
) -> str:
    """Require the configured prompt approval plus a qualified model/profile."""
    if require_marker:
        _require_delegation_marker(delegation_text)
    candidate = (model_id or "").strip()
    if not candidate:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "DELEGATION_MODEL_REQUIRED",
                "message": "delegated execution requires model_id or a configured qualified CPTR default model",
                "retriable": False,
                "field": "model_id",
            },
        )
    if not _is_qualified_model_id(candidate):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "DELEGATION_MODEL_NOT_QUALIFIED",
                "message": "delegated execution requires a fully qualified model_id: provider/model or agent:profile/model",
                "retriable": False,
                "field": "model_id",
            },
        )
    return candidate


def _safe_diff_path(value: str) -> bool:
    from pathlib import Path, PureWindowsPath

    candidate = value.strip()
    if not candidate:
        return False
    path = Path(candidate)
    windows = PureWindowsPath(candidate)
    return not path.is_absolute() and not windows.is_absolute() and ".." not in path.parts


def _bound_diff_result(
    value: dict[str, Any],
    *,
    max_bytes: int,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    selected = set(paths or [])
    source_files = list(value.get("files") or [])
    omitted_paths: list[str] = []
    if selected:
        filtered = []
        for item in source_files:
            path = str(item.get("path") or "") if isinstance(item, dict) else ""
            if path in selected:
                filtered.append(item)
            elif path:
                omitted_paths.append(path)
        source_files = filtered
    output_files: list[dict[str, Any]] = []
    used = 0
    truncated = bool(value.get("truncated", False))
    for item in source_files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        encoded = json.dumps(item, sort_keys=True, default=str).encode("utf-8")
        if used + len(encoded) > max_bytes:
            output_files.append({"path": path, "omitted": True})
            if path:
                omitted_paths.append(path)
            truncated = True
            continue
        output_files.append(item)
        used += len(encoded)
    result = {key: item for key, item in value.items() if key != "files"}
    result.update(
        {
            "files": output_files,
            "max_bytes": max_bytes,
            "bytes_returned": used,
            "truncated": truncated,
            "omitted_paths": sorted(set(omitted_paths)),
        }
    )
    return result


def _is_quiesced_status(status: str) -> bool:
    return status.upper() in {
        "COMPLETE",
        "COMPLETE_WITH_TOOL_ERRORS",
        "FAILED",
        "CANCELLED",
        "REVIEW_REQUIRED",
        "REJECTED",
        "BLOCKED",
    }


class TaskExecutionPolicy(BaseModel):
    """Server-enforced capability limits for one control-plane worker task."""

    allow_file_writes: bool = True
    allow_commands: bool = True
    allow_network: bool = False
    allow_package_install: bool = False


class TaskCreateRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=100_000)
    model_id: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=200)
    workbench_session_id: str | None = Field(default=None, pattern=r"^wbs_[A-Za-z0-9_-]{16,80}$")
    execution_policy: TaskExecutionPolicy = Field(default_factory=TaskExecutionPolicy)


class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ReviewDecisionRequest(BaseModel):
    decision: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=50_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class AutonomousCreateRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=100_000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=200)
    workbench_session_id: str | None = Field(default=None, pattern=r"^wbs_[A-Za-z0-9_-]{16,80}$")
    execution_policy: TaskExecutionPolicy = Field(default_factory=TaskExecutionPolicy)


class ApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=200)
    approved: bool
    note: str | None = Field(default=None, max_length=50_000)


class MemoryReadRequest(BaseModel):
    """Bounded, read-only persistent memory request for authenticated control clients."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "search",
        "inspect",
        "timeline",
        "health",
        "compact_summary",
        "summary",
        "checkpoint",
    ]
    workspace_id: str | None = Field(default=None, min_length=1, max_length=200)
    query: str | None = Field(default=None, min_length=1, max_length=12_000)
    memory_id: str | None = Field(default=None, min_length=1, max_length=200)
    task_key: str | None = Field(default=None, max_length=200)
    at_ms: int | None = Field(default=None, ge=0)
    known_at_ms: int | None = Field(default=None, ge=0)
    limit: int = Field(default=8, ge=1, le=20)
    include_historical: bool = False


def _monitor_summary(monitor: MonitorState) -> dict[str, Any]:
    verified = sum(scope.status.value == "VERIFIED" for scope in monitor.scopes)
    return {
        "monitor_id": monitor.monitor_id,
        "goal_id": monitor.goal_id,
        "workspace_id": monitor.workspace_id,
        "status": monitor.status.value,
        "scope_count": len(monitor.scopes),
        "verified_count": verified,
        "current_scope": monitor.current_scope_id,
        "approval_id": monitor.approval_id,
        "original_goal": monitor.original_goal,
        "acceptance_criteria": list(monitor.original_acceptance_criteria),
        "created_at": monitor.created_at,
        "updated_at": monitor.updated_at,
        "scopes": [
            {
                "scope_id": scope.scope_id,
                "title": scope.title,
                "status": scope.status.value,
                "verified": scope.status.value == "VERIFIED",
                "attempt_count": scope.attempt_count,
                "failure_signature_counts": dict(scope.failure_signature_counts),
                "worker_task_ids": list(scope.worker_task_ids),
                "next_action": scope.next_action,
            }
            for scope in monitor.scopes
        ],
    }


async def _user(request: Request, scope: str) -> str:
    return await require_control_user(request, scope)


async def _are_workspaces_equivalent(user_id: str, ws_a: str, ws_b: str) -> bool:
    if ws_a == ws_b:
        return True
    try:
        from cptr.services.workspace_refs import resolve_workspace_ref

        res_a = await resolve_workspace_ref(user_id=user_id, reference=ws_a)
        res_b = await resolve_workspace_ref(user_id=user_id, reference=ws_b)
        return str(res_a.workspace.id) == str(res_b.workspace.id)
    except Exception:
        return False


async def _ensure_workbench_routing(
    *, user_id: str, workspace_id: str, session_id: str | None
) -> dict[str, Any] | None:
    if not session_id:
        return None
    session = await workbench_session_store.get(owner_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="workbench session not found")
    if str(session.get("status") or "").upper() == "ARCHIVED" or session.get("archived_at"):
        raise HTTPException(status_code=409, detail="workbench session is archived")
    bound_workspace = session.get("workspace_id")
    if bound_workspace and str(bound_workspace) != workspace_id:
        if not await _are_workspaces_equivalent(user_id, str(bound_workspace), workspace_id):
            raise HTTPException(status_code=404, detail="workbench session not found")
    return session


def _services(request: Request) -> tuple[AgentService, AutonomousSupervisor]:
    agent = getattr(request.app.state, "control_agent_service", None)
    supervisor = getattr(request.app.state, "control_supervisor", None)
    if agent is None:
        agent = AgentService()
        request.app.state.control_agent_service = agent
    if supervisor is None:
        if os.environ.get("CPTR_SUPERVISOR_OPENAI_API_KEY") and os.environ.get(
            "CPTR_SUPERVISOR_OPENAI_MODEL"
        ):
            director = OpenAISupervisorDirector()
        else:
            director = LocalSupervisorDirector()
        supervisor = AutonomousSupervisor(
            store=SqlSupervisorStore(),
            agent=agent,
            director=director,
            max_attempts=int(os.environ.get("CPTR_SUPERVISOR_MAX_ATTEMPTS", "5")),
        )
        request.app.state.control_supervisor = supervisor
    if not hasattr(request.app.state, "control_monitor_tasks"):
        request.app.state.control_monitor_tasks = {}
    return agent, supervisor


async def _ensure_workspace(user_id: str, workspace_id: str) -> Workspace:
    async with await get_db() as db:
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != user_id:
        raise HTTPException(status_code=404, detail="workspace not found")
    if not is_workspace_available(workspace):
        raise HTTPException(status_code=409, detail="workspace is unavailable")
    return workspace


async def _monitor_loop(app: Any, monitor_id: str) -> None:
    supervisor = getattr(app.state, "control_supervisor", None)
    if supervisor is None:
        return
    interval = float(os.environ.get("CPTR_SUPERVISOR_POLL_INTERVAL", "2"))
    try:
        while True:
            monitor = await supervisor.run_once(monitor_id)
            from cptr.services.live_events import safe_publish_monitor_event

            status = monitor.status.value
            await safe_publish_monitor_event(
                user_id=monitor.user_id,
                monitor_id=monitor.monitor_id,
                event_type="monitor.terminal"
                if status
                in {
                    MonitorStatus.COMPLETE.value,
                    MonitorStatus.BLOCKED.value,
                    MonitorStatus.FAILED.value,
                    MonitorStatus.CANCELLED.value,
                }
                else "monitor.status",
                payload={
                    "status": status,
                    "current_scope": monitor.current_scope_id,
                    "scope_count": len(monitor.scopes),
                    "verified_count": sum(
                        item.status.value == "VERIFIED" for item in monitor.scopes
                    ),
                },
            )
            if monitor.status != MonitorStatus.RUNNING:
                return
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception:
        import logging

        logging.getLogger(__name__).exception("autonomous monitor loop failed: %s", monitor_id)


def _schedule_monitor(app: Any, monitor_id: str) -> None:
    tasks = app.state.control_monitor_tasks
    existing = tasks.get(monitor_id)
    if existing and not existing.done():
        return
    tasks[monitor_id] = asyncio.create_task(_monitor_loop(app, monitor_id))


async def recover_monitors(app: Any) -> None:
    """Resume persisted active monitors after CPTR startup."""
    request = getattr(app, "state", None)
    if request is None:
        return
    supervisor = getattr(app.state, "control_supervisor", None)
    if supervisor is None:
        if os.environ.get("CPTR_SUPERVISOR_OPENAI_API_KEY") and os.environ.get(
            "CPTR_SUPERVISOR_OPENAI_MODEL"
        ):
            director = OpenAISupervisorDirector()
        else:
            director = LocalSupervisorDirector()
        supervisor = AutonomousSupervisor(
            store=SqlSupervisorStore(),
            agent=AgentService(),
            director=director,
            max_attempts=int(os.environ.get("CPTR_SUPERVISOR_MAX_ATTEMPTS", "5")),
        )
        app.state.control_agent_service = supervisor.agent
        app.state.control_supervisor = supervisor
    if not hasattr(app.state, "control_monitor_tasks"):
        app.state.control_monitor_tasks = {}
    for monitor in await supervisor.store.list_active():
        if monitor.status == MonitorStatus.RUNNING:
            _schedule_monitor(app, monitor.monitor_id)


@router.get("/runtime/metrics")
async def get_runtime_metrics(request: Request):
    """Return bounded owner-scoped lifecycle/resource counters for benchmarking."""
    user_id = await _user(request, "task:read")
    from cptr.services.browser_device_connections import browser_device_connections
    from cptr.services.browser_devices import browser_device_store
    from cptr.services.live_events import live_event_hub
    from cptr.services.runtime_metrics import runtime_metrics
    from cptr.utils.browser.proxy import manager as browser_manager
    from cptr.utils.mcp.stdio_manager import stdio_manager
    from cptr.utils.terminal import manager as terminal_manager
    from cptr.utils.tools import command_session_metrics

    owned_device_ids = {
        str(device["device_id"])
        for device in await browser_device_store.list_devices(user_id=user_id)
        if isinstance(device.get("device_id"), str)
    }

    return {
        "version": 1,
        "timestamp_ms": int(time.time() * 1000),
        "runtime": runtime_metrics.snapshot(),
        "commands": command_session_metrics(),
        "terminal_sessions": len(terminal_manager._sessions),
        "browser_proxy_sessions": browser_manager.count(),
        "browser_device": {
            **(await browser_device_store.runtime_metrics(user_id=user_id)),
            "connected_devices": await browser_device_connections.count(
                device_ids=owned_device_ids
            ),
        },
        "mcp_stdio_sessions": len(stdio_manager._instances),
        "live_events": live_event_hub.stats(),
    }


class WorkspaceActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkspaceCreateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4000)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    create_directory: bool = False
    initialize_git: bool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


def _workspace_create_idempotency_db_key(key: str) -> str:
    return f"workspace-create:{key}"


def _workspace_create_request_fingerprint(body: WorkspaceCreateRequest) -> str:
    payload = {
        "path": body.path,
        "name": body.name,
        "create_directory": body.create_directory,
        "initialize_git": body.initialize_git,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _workspace_create_idempotency_conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": code,
            "message": message,
            "retriable": False,
            "field": "idempotency_key",
        },
    )


def _workspace_create_replay(record: Any, request_fingerprint: str) -> dict[str, Any]:
    if record.resource_type != "workspace":
        raise _workspace_create_idempotency_conflict(
            "WORKSPACE_IDEMPOTENCY_KEY_COLLISION",
            "workspace idempotency key is already owned by a different resource type",
        )
    response = dict(record.response or {})
    if response.get("request_fingerprint") != request_fingerprint:
        raise _workspace_create_idempotency_conflict(
            "WORKSPACE_IDEMPOTENCY_REQUEST_MISMATCH",
            "workspace idempotency key was already used with different request arguments",
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise _workspace_create_idempotency_conflict(
            "WORKSPACE_IDEMPOTENCY_REPLAY_INVALID",
            "workspace idempotency record cannot be replayed safely",
        )
    return dict(result)


async def _workspace_create_idempotency_get(
    user_id: str, key: str, request_fingerprint: str
) -> dict[str, Any] | None:
    durable_key = _workspace_create_idempotency_db_key(key)
    async with await get_db() as db:
        record = (
            await db.execute(
                select(ControlIdempotency).where(
                    ControlIdempotency.user_id == user_id,
                    ControlIdempotency.key == durable_key,
                )
            )
        ).scalar_one_or_none()
    if record is None:
        return None
    return _workspace_create_replay(record, request_fingerprint)


async def _workspace_create_idempotency_put(
    user_id: str,
    key: str,
    request_fingerprint: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    durable_key = _workspace_create_idempotency_db_key(key)
    now_ms = int(time.time() * 1000)
    async with await get_db() as db:
        db.add(
            ControlIdempotency(
                user_id=user_id,
                key=durable_key,
                resource_type="workspace",
                resource_id=str(result["workspace_id"]),
                response={
                    "request_fingerprint": request_fingerprint,
                    "result": dict(result),
                },
                created_at=now_ms,
            )
        )
        try:
            await db.commit()
            return result
        except IntegrityError as exc:
            await db.rollback()
            existing = (
                await db.execute(
                    select(ControlIdempotency).where(
                        ControlIdempotency.user_id == user_id,
                        ControlIdempotency.key == durable_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            try:
                return _workspace_create_replay(existing, request_fingerprint)
            except HTTPException as conflict:
                raise conflict from exc


@router.post("/workspace-os/action")
async def workspace_os_action(request: Request, body: WorkspaceActionRequest):
    action = body.action.strip().lower()
    if action in WORKSPACE_READ_ACTIONS:
        scope = "workspace:read"
    elif action in WORKSPACE_WRITE_ACTIONS:
        scope = "workspace:write"
    else:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "WORKSPACE_ACTION_UNSUPPORTED",
                "message": f"unsupported Workspace OS action: {action}",
                "retriable": False,
                "supported_actions": sorted(WORKSPACE_READ_ACTIONS | WORKSPACE_WRITE_ACTIONS),
            },
        )

    user_id = await _user(request, scope)
    try:
        return await workspace_action_service.execute(
            user_id=user_id,
            action=action,
            payload=body.payload,
        )
    except WorkspaceActionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": str(exc),
                "retriable": exc.status_code >= 500,
                **({"details": exc.details} if exc.details else {}),
            },
        ) from exc
    except TypeError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "WORKSPACE_ACTION_INVALID_PAYLOAD",
                "message": str(exc),
                "retriable": False,
            },
        ) from exc


@router.post("/workspaces")
async def create_workspace(request: Request, body: WorkspaceCreateRequest):
    user_id = await _user(request, "coding:write")
    request_fingerprint = _workspace_create_request_fingerprint(body)
    if body.idempotency_key:
        replay = await _workspace_create_idempotency_get(
            user_id, body.idempotency_key, request_fingerprint
        )
        if replay is not None:
            return replay
    workspace_path = await _resolve_request_workspace_path(request, body.path)
    created_directory = False

    try:
        stat = await Runtime.stat(request, workspace_path)
    except FileError as exc:
        if exc.status_code != 404:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if not body.create_directory:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "WORKSPACE_DIRECTORY_NOT_FOUND",
                    "message": "workspace directory does not exist; set create_directory=true to create it",
                    "retriable": False,
                    "field": "path",
                },
            ) from exc
        try:
            await Runtime.create_item(request, workspace_path, type="directory")
            stat = await Runtime.stat(request, workspace_path)
        except FileError as create_exc:
            raise HTTPException(
                status_code=create_exc.status_code,
                detail=str(create_exc),
            ) from create_exc
        created_directory = True

    if stat.get("type") != "directory":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WORKSPACE_PATH_CONFLICT",
                "message": "workspace path exists and is not a directory",
                "retriable": False,
                "field": "path",
            },
        )

    existing = await Workspace.get_by_path(user_id, workspace_path)
    identity = await identity_for_request(request)
    git_repo = await is_repo(workspace_path, identity)
    initialized_git = False
    if body.initialize_git and not git_repo:
        await init_repo(workspace_path, identity)
        initialized_git = True
        git_repo = await is_repo(workspace_path, identity)

    restored_workspace = False
    if existing is not None:
        existing_data = dict(existing.data or {})
        if existing_data.pop("_cptr_archived", False):
            workspace = await Workspace.upsert(
                user_id,
                workspace_path,
                existing.name or body.name or Path(workspace_path).name or workspace_path,
                existing_data,
            )
            restored_workspace = True
        else:
            workspace = existing
        created_workspace = False
    else:
        name = body.name or Path(workspace_path).name or workspace_path
        workspace = await Workspace.upsert(user_id, workspace_path, name, {})
        created_workspace = True

    result = {
        "workspace_id": workspace.id,
        "name": workspace.name,
        "available": True,
        "is_git_repo": git_repo,
        "created_workspace": created_workspace,
        "restored_workspace": restored_workspace,
        "created_directory": created_directory,
        "initialized_git": initialized_git,
    }
    if body.idempotency_key:
        return await _workspace_create_idempotency_put(
            user_id,
            body.idempotency_key,
            request_fingerprint,
            result,
        )
    return result


@router.get("/workspaces")
async def list_workspaces(request: Request, include_unavailable: bool = False):
    user_id = await _user(request, "workspace:read")
    workspaces = await Workspace.get_by_user(user_id)
    rows = []
    for workspace in workspaces:
        if bool(dict(workspace.data or {}).get("_cptr_archived")):
            continue
        available = is_workspace_available(workspace)
        if not include_unavailable and not available:
            continue
        rows.append(
            {
                "workspace_id": workspace.id,
                "name": workspace.name,
                "available": available,
                "last_used_at": workspace.updated_at or workspace.created_at,
            }
        )
    return {"workspaces": rows}


def _public_memory_record(value: Any, *, text_limit: int = 20_000) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = (
        "memory_id",
        "scope",
        "kind",
        "canonical_text",
        "status",
        "trust_level",
        "confidence_ppm",
        "importance_ppm",
        "valid_from_ms",
        "valid_until_ms",
        "observed_at_ms",
        "superseded_at_ms",
        "superseded_by_id",
        "parent_memory_id",
        "branch_id",
        "verified_at_ms",
        "verification_expires_at_ms",
        "created_at_ms",
        "updated_at_ms",
    )
    result = {key: value.get(key) for key in allowed if key in value}
    text = str(result.get("canonical_text") or "")
    if len(text) > text_limit:
        result["canonical_text"] = text[: max(0, text_limit - 1)].rstrip() + "…"
        result["text_truncated"] = True
    return result


@router.post("/memory/read")
async def read_memory(request: Request, body: MemoryReadRequest):
    """Read owner/workspace-scoped persistent knowledge without exposing mutation capability."""
    user_id = await _user(request, "memory:read")
    workspace_path = ""
    if body.workspace_id:
        workspace = await _ensure_workspace(user_id, body.workspace_id)
        workspace_path = str(workspace.path or "")
    adapter = MemoryMcpAdapter(
        user_id=user_id,
        workspace=workspace_path,
        allow_mutations=False,
    )

    try:
        if body.action == "search":
            query = str(body.query or "").strip()
            if not query:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "MEMORY_QUERY_REQUIRED",
                        "message": "query is required for memory search",
                        "retriable": False,
                        "field": "query",
                    },
                )
            raw = await adapter.call_tool(
                "memory.search",
                {
                    "query": query,
                    "limit": body.limit,
                    "include_historical": body.include_historical,
                },
            )
            results = []
            feedback_items: list[dict[str, Any]] = []
            context_id = (
                "mcpctx_"
                + hashlib.sha256(
                    f"{user_id}:{workspace_path}:{time.time_ns()}".encode("utf-8")
                ).hexdigest()[:24]
            )
            query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
            for rank, item in enumerate(list(raw.get("results") or [])[: body.limit], start=1):
                if not isinstance(item, dict):
                    continue
                shaped = {
                    key: item.get(key)
                    for key in (
                        "memory_id",
                        "scope",
                        "kind",
                        "score",
                        "reason",
                        "confidence",
                        "trust_level",
                        "verification_stale",
                    )
                    if key in item
                }
                text = str(item.get("text") or "")
                shaped["text"] = text[:4_000] + ("…" if len(text) > 4_000 else "")
                results.append(shaped)
                memory_id = str(item.get("memory_id") or "").strip()
                features = item.get("features") if isinstance(item.get("features"), dict) else {}
                if memory_id:
                    feedback_items.append(
                        {
                            "memory_id": memory_id,
                            "rank": rank,
                            "score": max(0.0, min(1.0, float(item.get("score") or 0.0))),
                            "features": {
                                str(key): max(0.0, min(1.0, float(value)))
                                for key, value in features.items()
                                if isinstance(value, (int, float))
                            },
                            "verification_stale": bool(item.get("verification_stale", False)),
                        }
                    )
            context_chars = sum(len(str(item.get("text") or "")) for item in results)
            try:
                # A returned result is known to have entered ChatGPT's tool context, so
                # persist exposure/use telemetry immediately. Helpfulness remains unknown
                # until a later backend-observed action supplies a real terminal outcome.
                for item in feedback_items[:8]:
                    await adapter.service.feedback(
                        RetrievalFeedback(
                            user_id=user_id,
                            workspace=workspace_path,
                            memory_id=str(item["memory_id"]),
                            context_id=context_id,
                            query=query,
                            rank=int(item["rank"]),
                            score=float(item["score"]),
                            used=True,
                            helpful=None,
                            outcome=None,
                            features=dict(item["features"]),
                        )
                    )
                await adapter.service.record_event(
                    user_id=user_id,
                    workspace=workspace_path,
                    event_type="recall",
                    reason="retrieved by ChatGPT MCP persistent-memory bridge",
                    trust_level="verified_system_fact",
                    confidence_ppm=1_000_000,
                    payload={
                        "items": [
                            {
                                "node_id": str(item.get("memory_id") or ""),
                                "scope": str(item.get("scope") or ""),
                                "path": "canonical",
                                "heading": str(item.get("kind") or "memory"),
                                "memory_id": str(item.get("memory_id") or ""),
                                "reason": str(item.get("reason") or "memory search"),
                            }
                            for item in results
                            if item.get("memory_id")
                        ][:50],
                        "context_chars": context_chars,
                        "item_count": len(results),
                        "feedback_context_id": context_id,
                        "query_hash": query_hash,
                        "feedback_items": feedback_items[:8],
                    },
                )
            except Exception:
                pass
            result = {"results": results, "count": len(results)}
        elif body.action == "inspect":
            memory_id = str(body.memory_id or "").strip()
            if not memory_id:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "MEMORY_ID_REQUIRED",
                        "message": "memory_id is required for memory inspection",
                        "retriable": False,
                        "field": "memory_id",
                    },
                )
            result = _public_memory_record(
                await adapter.call_tool("memory.inspect", {"memory_id": memory_id})
            )
        elif body.action == "timeline":
            if body.at_ms is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "MEMORY_TIME_REQUIRED",
                        "message": "at_ms is required for memory timeline reads",
                        "retriable": False,
                        "field": "at_ms",
                    },
                )
            raw = await adapter.call_tool(
                "memory.timeline",
                {
                    "at_ms": body.at_ms,
                    **({"known_at_ms": body.known_at_ms} if body.known_at_ms is not None else {}),
                },
            )
            result = {
                "at_ms": raw.get("at_ms"),
                "known_at_ms": raw.get("known_at_ms"),
                "records": [
                    _public_memory_record(item)
                    for item in list(raw.get("records") or [])[: body.limit]
                    if isinstance(item, dict)
                ],
            }
        elif body.action in ("compact_summary", "summary"):
            result = await adapter.call_tool(
                "memory.compact_summary",
                {
                    **({"task_key": body.task_key} if body.task_key else {}),
                    "limit": body.limit,
                },
            )
        elif body.action == "checkpoint":
            result = await adapter.call_tool(
                "memory.checkpoint",
                {
                    **({"task_key": body.task_key} if body.task_key else {}),
                },
            )
        else:
            result = await adapter.call_tool("memory.health", {})
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "MEMORY_NOT_FOUND", "message": "memory not found", "retriable": False},
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "MEMORY_READ_FORBIDDEN", "message": str(exc), "retriable": False},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "MEMORY_READ_INVALID", "message": str(exc), "retriable": False},
        ) from exc
    except MemoryUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MEMORY_UNAVAILABLE",
                "message": "persistent memory is temporarily unavailable",
                "retriable": True,
            },
        ) from exc

    return {"action": body.action, "workspace_id": body.workspace_id, "result": result}


@router.get("/models")
async def list_models(request: Request):
    await _user(request, "task:read")
    from cptr.routers.chat import _get_connections, _get_connection_models
    from cptr.utils.agents.detection import get_available_agent_model_entries

    connections = [c for c in await _get_connections() if c.get("enabled", True)]
    entries = []
    for conn in connections:
        prefix = (conn.get("prefix_id") or "").strip()
        for model in await _get_connection_models(conn, request.app.state):
            model_id = f"{prefix}/{model}" if prefix else model
            if _is_qualified_model_id(model_id):
                entries.append({"model_id": model_id, "name": model, "default": False})
    for item in await get_available_agent_model_entries(request.app.state):
        model_id = str(item.get("id") or item.get("model_id") or "").strip()
        if _is_qualified_model_id(model_id):
            entries.append(
                {
                    "model_id": model_id,
                    "name": str(item.get("name") or model_id),
                    "default": False,
                }
            )
    default = await _default_model()
    for item in entries:
        item["default"] = item["model_id"] == default
    return {"models": entries}


@router.get("/tasks")
async def list_tasks(
    request: Request, workspace_id: str | None = None, status: str | None = None, limit: int = 20
):
    user_id = await _user(request, "task:read")
    limit = max(1, min(limit, 100))
    async with await get_db() as db:
        query = select(ControlTask).where(ControlTask.user_id == user_id)
        if workspace_id:
            query = query.where(ControlTask.workspace_id == workspace_id)
        if status:
            query = query.where(ControlTask.status == status)
        query = query.order_by(ControlTask.created_at.desc()).limit(limit)
        rows = (await db.execute(query)).scalars().all()
    return {
        "tasks": [
            {
                "id": row.id,
                "task_id": row.id,
                "workspace_id": row.workspace_id,
                "status": row.status,
                "review_status": row.review_status,
                "error": redact_external(row.error) if row.error else None,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]
    }


@router.get("/tasks/{task_id}/events")
async def get_task_events(
    request: Request,
    task_id: str,
    after_sequence: int = 0,
    max_events: int = 50,
):
    user_id = await _user(request, "task:read")
    agent, _ = _services(request)
    try:
        task = await agent.get_task(task_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    if after_sequence < 0 or max_events < 1 or max_events > 500:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_PAGINATION",
                "message": "after_sequence/max_events are out of range",
                "retriable": False,
            },
        )
    from cptr.services.live_events import live_event_hub

    events = await live_event_hub.store.replay(
        f"task:{task['id']}",
        after_sequence=after_sequence,
        limit=max_events + 1,
    )
    truncated = len(events) > max_events
    page = events[:max_events]
    return {
        "task_id": task_id,
        "after_sequence": after_sequence,
        "last_sequence": page[-1].sequence if page else after_sequence,
        "max_events": max_events,
        "truncated": truncated,
        "events": [event.to_dict() for event in page],
    }


@router.get("/autonomous")
async def list_autonomous(
    request: Request,
    workspace_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
):
    user_id = await _user(request, "autonomous:run")
    limit = max(1, min(limit, 100))
    async with await get_db() as db:
        query = select(AutonomousMonitor).where(AutonomousMonitor.user_id == user_id)
        if workspace_id:
            query = query.where(AutonomousMonitor.workspace_id == workspace_id)
        if status:
            query = query.where(AutonomousMonitor.status == status)
        query = query.order_by(AutonomousMonitor.updated_at.desc()).limit(limit)
        rows = list((await db.execute(query)).scalars().all())
    _, supervisor = _services(request)
    monitors = []
    for row in rows:
        monitor = await supervisor.store.get_monitor(row.id)
        if monitor is not None:
            monitors.append(_monitor_summary(monitor))
    return {"monitors": monitors}


@router.get("/workspaces/{workspace_id}")
async def get_workspace(request: Request, workspace_id: str):
    user_id = await _user(request, "workspace:read")
    async with await get_db() as db:
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != user_id:
        raise HTTPException(status_code=404, detail="workspace not found")
    available = is_workspace_available(workspace)
    is_git_repo = False
    dirty_file_count = 0
    if available:
        from cptr.utils.git import is_repo, status
        from cptr.utils.identity import identity_for_user_id

        identity = await identity_for_user_id(user_id)
        is_git_repo = await is_repo(workspace.path, identity)
        if is_git_repo:
            git_status = await status(workspace.path, identity)
            dirty_file_count = len(git_status.get("files", []))
    return {
        "workspace_id": workspace.id,
        "name": workspace.name,
        "available": available,
        "is_git_repo": is_git_repo,
        "dirty_file_count": dirty_file_count,
        "last_used_at": workspace.updated_at or workspace.created_at,
    }


@router.post("/tasks")
async def create_task(request: Request, body: TaskCreateRequest):
    user_id = await _user(request, "task:write")
    await _ensure_workspace(user_id, body.workspace_id)
    await _ensure_workbench_routing(
        user_id=user_id,
        workspace_id=body.workspace_id,
        session_id=body.workbench_session_id,
    )
    delegation_approval_required = await guard_policy_service.is_enabled(
        user_id, "delegation_prompt_approval"
    )
    review_required = await guard_policy_service.is_enabled(user_id, "task_review_approval")
    selected_model = body.model_id or await _default_model()
    model_id = _require_explicit_delegation(
        selected_model, body.prompt, require_marker=delegation_approval_required
    )
    agent, _ = _services(request)
    try:
        return await agent.start_task(
            user_id=user_id,
            workspace_id=body.workspace_id,
            prompt=body.prompt,
            model_id=model_id,
            idempotency_key=body.idempotency_key,
            execution_policy=body.execution_policy.model_dump(),
            request=request,
            **({"review_required": False} if not review_required else {}),
            **(
                {"workbench_session_id": body.workbench_session_id}
                if body.workbench_session_id
                else {}
            ),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    user_id = await _user(request, "task:read")
    agent, _ = _services(request)
    try:
        task = await agent.get_task(task_id, user_id=user_id)
        task["review_status"] = str((task.get("review") or {}).get("status") or "NOT_REQUIRED")
        return task
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@router.get("/tasks/{task_id}/output")
async def get_task_output(request: Request, task_id: str, offset: int = 0, max_chars: int = 20_000):
    user_id = await _user(request, "task:read")
    if offset < 0 or max_chars < 1 or max_chars > 200_000:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_PAGINATION",
                "message": "offset/max_chars are out of range",
                "retriable": False,
            },
        )
    agent, _ = _services(request)
    try:
        output = await agent.get_output(task_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    content = str(output.get("content") or "")
    page = content[offset : offset + max_chars]
    return {
        "task_id": task_id,
        "status": str(output.get("status") or "UNKNOWN"),
        "content": page,
        "offset": offset,
        "max_chars": max_chars,
        "total_chars": len(content),
        "truncated": offset + len(page) < len(content),
        "completion_integrity": output.get("completion_integrity"),
        "review": output.get("review"),
    }


@router.post("/tasks/{task_id}/messages")
async def send_task_message(request: Request, task_id: str, body: MessageRequest):
    user_id = await _user(request, "task:write")
    agent, _ = _services(request)
    try:
        response = await agent.send_message(
            task_id,
            user_id=user_id,
            content=body.content,
            idempotency_key=body.idempotency_key,
        )
        task = await agent.get_task(task_id, user_id=user_id)
        return {
            **response,
            "accepted": True,
            "status": task["status"],
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/review")
async def get_task_review(request: Request, task_id: str, max_diff_bytes: int = 100_000):
    user_id = await _user(request, "task:read")
    if max_diff_bytes < 1 or max_diff_bytes > 2_000_000:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_LIMIT",
                "message": "max_diff_bytes is out of range",
                "retriable": False,
                "field": "max_diff_bytes",
            },
        )
    agent, _ = _services(request)
    try:
        review = await agent.get_task_review(task_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    diff = review.get("diff")
    if isinstance(diff, dict):
        review["diff"] = _bound_diff_result(diff, max_bytes=max_diff_bytes)
    return review


@router.post("/tasks/{task_id}/review")
async def decide_task_review(request: Request, task_id: str, body: ReviewDecisionRequest):
    user_id = await _user(request, "task:write")
    agent, _ = _services(request)
    try:
        result = await agent.decide_review(
            task_id,
            user_id=user_id,
            decision=body.decision,
            note=body.note,
            idempotency_key=body.idempotency_key,
        )
        if body.decision.strip().upper() == "REQUEST_CHANGES":
            result["follow_up_task_id"] = str(
                (result.get("review_message") or {}).get("task_id") or task_id
            )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str):
    user_id = await _user(request, "task:write")
    agent, _ = _services(request)
    try:
        result = await agent.cancel_task(task_id, user_id=user_id)
        result["quiesced"] = _is_quiesced_status(str(result.get("status") or ""))
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@router.get("/workspaces/{workspace_id}/git/status")
async def get_git_status(request: Request, workspace_id: str, worker_id: str | None = None):
    user_id = await _user(request, "git:read")
    workspace = await _ensure_workspace(user_id, workspace_id)
    from cptr.utils.git import is_repo, status
    from cptr.utils.identity import identity_for_user_id

    identity = await identity_for_user_id(user_id)
    root = workspace.path
    if worker_id:
        try:
            root = str(
                await resolve_direct_worker_root(
                    user_id=user_id, workspace_id=workspace_id, worker_id=worker_id
                )
            )
        except DirectCodingWorkerError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc), "retriable": exc.status_code >= 409},
            ) from exc
    if not await is_repo(root, identity):
        return {"is_repo": False, "files": []}
    result = await status(root, identity)
    result["is_repo"] = True
    return result


@router.get("/workspaces/{workspace_id}/git/diff")
async def get_git_diff(
    request: Request,
    workspace_id: str,
    paths: list[str] | None = Query(default=None),
    max_bytes: int = 100_000,
    worker_id: str | None = None,
):
    user_id = await _user(request, "git:read")
    await _ensure_workspace(user_id, workspace_id)
    if max_bytes < 1 or max_bytes > 2_000_000:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_LIMIT",
                "message": "max_bytes is out of range",
                "retriable": False,
                "field": "max_bytes",
            },
        )
    for path in paths or []:
        if not _safe_diff_path(path):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_PATH",
                    "message": "diff paths must be workspace-relative",
                    "retriable": False,
                    "field": "paths",
                },
            )
    if worker_id:
        from cptr.utils.git import diff as git_diff, is_repo
        from cptr.utils.identity import identity_for_user_id

        try:
            root = str(
                await resolve_direct_worker_root(
                    user_id=user_id, workspace_id=workspace_id, worker_id=worker_id
                )
            )
        except DirectCodingWorkerError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc), "retriable": exc.status_code >= 409},
            ) from exc
        identity = await identity_for_user_id(user_id)
        if not await is_repo(root, identity):
            value = {"is_repo": False, "files": [], "diagnostic": "not a git repository"}
        else:
            value = await git_diff(root, None, False, True, False, identity)
            value["is_repo"] = True
        return _bound_diff_result(value, max_bytes=max_bytes, paths=paths)
    agent, _ = _services(request)
    try:
        value = await agent.get_diff(workspace_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    return _bound_diff_result(value, max_bytes=max_bytes, paths=paths)


@router.post("/autonomous")
async def create_autonomous(request: Request, body: AutonomousCreateRequest):
    user_id = await _user(request, "autonomous:run")
    await _ensure_workspace(user_id, body.workspace_id)
    await _ensure_workbench_routing(
        user_id=user_id,
        workspace_id=body.workspace_id,
        session_id=body.workbench_session_id,
    )
    delegation_approval_required = await guard_policy_service.is_enabled(
        user_id, "delegation_prompt_approval"
    )
    model_id = _require_explicit_delegation(
        body.model_id, body.goal, require_marker=delegation_approval_required
    )
    _, supervisor = _services(request)
    try:
        monitor = await supervisor.create_goal(
            user_id=user_id,
            workspace_id=body.workspace_id,
            goal=body.goal,
            acceptance_criteria=body.acceptance_criteria,
            model_id=model_id,
            idempotency_key=body.idempotency_key,
            execution_policy=body.execution_policy.model_dump(),
            **(
                {"workbench_session_id": body.workbench_session_id}
                if body.workbench_session_id
                else {}
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _schedule_monitor(request.app, monitor.monitor_id)
    from cptr.services.live_events import safe_publish_monitor_event

    await safe_publish_monitor_event(
        user_id=user_id,
        monitor_id=monitor.monitor_id,
        event_type="monitor.started",
        payload={"status": monitor.status.value, "scope_count": len(monitor.scopes)},
        workbench_session_id=body.workbench_session_id,
        workspace_id=body.workspace_id,
    )
    return _monitor_summary(monitor)


@router.get("/autonomous/{monitor_id}")
async def get_autonomous(request: Request, monitor_id: str):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    try:
        monitor = await supervisor.store.get_monitor(monitor_id)
    except KeyError:
        monitor = None
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    summary = _monitor_summary(monitor)
    if monitor.approval_id:
        approval = await supervisor.store.get_approval(monitor.approval_id)
        if approval:
            summary["approval"] = {
                "approval_id": approval.approval_id,
                "operation": approval.operation,
                "reason": approval.reason,
                "status": approval.status,
                "requested_at": approval.requested_at,
            }
    return summary


@router.get("/autonomous/{monitor_id}/events")
async def get_autonomous_events(
    request: Request,
    monitor_id: str,
    after_sequence: int = 0,
    max_events: int = 100,
):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    monitor = await supervisor.store.get_monitor(monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    if after_sequence < 0 or max_events < 1 or max_events > 500:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_PAGINATION",
                "message": "after_sequence/max_events are out of range",
                "retriable": False,
            },
        )
    from cptr.services.live_events import live_event_hub

    events = await live_event_hub.store.replay(
        f"monitor:{monitor_id}",
        after_sequence=after_sequence,
        limit=max_events + 1,
    )
    truncated = len(events) > max_events
    page = events[:max_events]
    return {
        "monitor_id": monitor_id,
        "after_sequence": after_sequence,
        "last_sequence": page[-1].sequence if page else after_sequence,
        "max_events": max_events,
        "truncated": truncated,
        "events": [event.to_dict() for event in page],
    }


@router.get("/autonomous/{monitor_id}/evidence")
async def get_autonomous_evidence(request: Request, monitor_id: str, scope: str | None = None):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    monitor = await supervisor.store.get_monitor(monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    evidence = await supervisor.store.list_evidence(monitor_id)
    return {
        "monitor_id": monitor_id,
        "scope": scope,
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "scope_id": item.scope_id,
                "kind": item.kind,
                "payload": redact_external(item.payload),
                "created_at": item.created_at,
            }
            for item in evidence
            if scope is None or item.scope_id == scope
        ],
    }


@router.post("/autonomous/{monitor_id}/messages")
async def send_autonomous_message(request: Request, monitor_id: str, body: MessageRequest):
    user_id = await _user(request, "autonomous:run")
    agent, supervisor = _services(request)
    if not await supervisor.store.claim_monitor(monitor_id):
        raise HTTPException(status_code=409, detail="monitor is busy; retry steering")
    try:
        monitor = await supervisor.store.get_monitor(monitor_id)
        if monitor is None or monitor.user_id != user_id:
            raise HTTPException(status_code=404, detail="monitor not found")
        scope = next(
            (item for item in monitor.scopes if item.scope_id == monitor.current_scope_id), None
        )
        if monitor.status != MonitorStatus.RUNNING or scope is None:
            raise HTTPException(status_code=409, detail="monitor has no steerable active worker")
        task_id = scope.worker_task_ids[-1] if scope.worker_task_ids else None
        if not task_id:
            raise HTTPException(status_code=409, detail="monitor has no active worker task")
        worker_task = await agent.store.get(task_id)
        if worker_task is None or worker_task.user_id != user_id:
            raise HTTPException(status_code=404, detail="worker task not found")
        if str(worker_task.status).upper() in {
            "COMPLETE",
            "COMPLETE_WITH_TOOL_ERRORS",
            "FAILED",
            "CANCELLED",
            "ERROR",
        }:
            raise HTTPException(status_code=409, detail="monitor worker is no longer steerable")
        from cptr.utils.chat_task import is_running

        if not is_running(worker_task.message_id):
            raise HTTPException(status_code=409, detail="monitor worker is not actively running")
        get_workspace_fingerprint = getattr(agent, "get_workspace_fingerprint", None)
        baseline_workspace_snapshot = (
            await get_workspace_fingerprint(monitor.workspace_id, user_id=user_id)
            if callable(get_workspace_fingerprint)
            else None
        )
        baseline_workspace_snapshot = redact_sensitive(baseline_workspace_snapshot)
        baseline_diff = await agent.get_diff(monitor.workspace_id, user_id=user_id)
        baseline_diff_fingerprint = hashlib.sha256(
            json.dumps(redact_sensitive(baseline_diff), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        response = await agent.send_message(
            task_id,
            user_id=user_id,
            content=body.content,
            idempotency_key=body.idempotency_key,
            provenance={
                "monitor_id": monitor.monitor_id,
                "scope_id": scope.scope_id,
                "intended_message_id": worker_task.message_id,
            },
        )
        await supervisor.record_steering(
            monitor.monitor_id,
            scope_id=scope.scope_id,
            control_message_id=response["control_message_id"],
            intended_task_id=task_id,
            intended_generation_id=response.get("target_message_id") or worker_task.message_id,
            baseline_diff_fingerprint=baseline_diff_fingerprint,
            baseline_workspace_snapshot=baseline_workspace_snapshot,
            setup_readiness_status=response.get("setup_readiness_status"),
        )
        from cptr.services.live_events import safe_publish_monitor_event

        await safe_publish_monitor_event(
            user_id=user_id,
            monitor_id=monitor.monitor_id,
            event_type="control.queued",
            task_id=task_id,
            payload={
                "status": response.get("delivery_status", response.get("status", "QUEUED")),
                "control_message_id": response.get("control_message_id"),
                "task_id": task_id,
            },
        )
        return {
            "message_id": str(
                response.get("message_id") or response.get("control_message_id") or ""
            ),
            "status": str(response.get("delivery_status") or response.get("status") or "QUEUED"),
            "accepted": True,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="worker task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        await supervisor.store.release_monitor(monitor_id)


@router.post("/autonomous/{monitor_id}/cancel")
async def cancel_autonomous(request: Request, monitor_id: str):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    monitor = await supervisor.store.get_monitor(monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    result = await supervisor.cancel(monitor_id)
    from cptr.services.live_events import safe_publish_monitor_event

    await safe_publish_monitor_event(
        user_id=user_id,
        monitor_id=monitor_id,
        event_type="monitor.terminal"
        if result.status
        in {
            MonitorStatus.CANCELLED,
            MonitorStatus.BLOCKED,
            MonitorStatus.FAILED,
        }
        else "monitor.status",
        payload={"status": result.status.value},
    )
    summary = _monitor_summary(result)
    summary["quiesced"] = _is_quiesced_status(result.status.value)
    return summary


@router.post("/autonomous/{monitor_id}/approve")
async def approve_autonomous(request: Request, monitor_id: str, body: ApprovalRequest):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    monitor = await supervisor.store.get_monitor(monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    try:
        monitor = await supervisor.approve(
            monitor_id,
            approval_id=body.approval_id,
            approved=body.approved,
            note=body.note,
        )
        if monitor.status == MonitorStatus.RUNNING:
            _schedule_monitor(request.app, monitor.monitor_id)
        from cptr.services.live_events import safe_publish_monitor_event

        await safe_publish_monitor_event(
            user_id=user_id,
            monitor_id=monitor.monitor_id,
            event_type="monitor.approval",
            payload={"status": monitor.status.value, "approved": body.approved},
        )
        return _monitor_summary(monitor)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
