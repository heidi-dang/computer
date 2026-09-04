"""Versioned CPTR Control API for MCP and other automation clients."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from cptr.models import Workspace, ControlTask, Config, AutonomousMonitor
from cptr.services.workspace_availability import is_workspace_available
from cptr.services.agent_service import AgentService
from cptr.services.control_auth import require_control_user
from cptr.services.control_store import SqlSupervisorStore
from cptr.services.direct_coding_workers import DirectCodingWorkerError, resolve_direct_worker_root
from cptr.services.supervisor import AutonomousSupervisor, MonitorState, MonitorStatus
from cptr.services.supervisor_director import LocalSupervisorDirector, OpenAISupervisorDirector
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_external, redact_sensitive

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


def _require_explicit_delegation(model_id: str | None, delegation_text: str) -> str:
    """Fail closed unless the delegated request is authorized and resolves to a qualified model/profile."""
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
    execution_policy: TaskExecutionPolicy = Field(default_factory=TaskExecutionPolicy)


class ApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=200)
    approved: bool
    note: str | None = Field(default=None, max_length=50_000)


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


@router.get("/workspaces")
async def list_workspaces(request: Request, include_unavailable: bool = False):
    user_id = await _user(request, "workspace:read")
    workspaces = await Workspace.get_by_user(user_id)
    rows = []
    for workspace in workspaces:
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
    _require_delegation_marker(body.prompt)
    selected_model = body.model_id or await _default_model()
    model_id = _require_explicit_delegation(selected_model, body.prompt)
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
    model_id = _require_explicit_delegation(body.model_id, body.goal)
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
