"""Versioned CPTR Control API for MCP and other automation clients."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.models import Workspace
from cptr.services.agent_service import AgentService
from cptr.services.control_auth import authenticate_control_request
from cptr.services.control_store import SqlSupervisorStore
from cptr.services.supervisor import AutonomousSupervisor, MonitorState, MonitorStatus
from cptr.services.supervisor_director import LocalSupervisorDirector, OpenAISupervisorDirector
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_external, redact_sensitive

router = APIRouter(prefix="/api/control/v1", tags=["control"])


class TaskCreateRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=100_000)
    model_id: str = Field(min_length=1, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=200)


class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class AutonomousCreateRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=100_000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=200)


class ApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=200)
    approved: bool


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
        "scopes": [
            {
                "scope_id": scope.scope_id,
                "title": scope.title,
                "status": scope.status.value,
                "attempt_count": scope.attempt_count,
                "failure_signature_counts": dict(scope.failure_signature_counts),
                "worker_task_ids": list(scope.worker_task_ids),
                "next_action": scope.next_action,
            }
            for scope in monitor.scopes
        ],
    }


def _raise_auth(exc: PermissionError) -> None:
    if str(exc).startswith("missing required scope"):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    raise HTTPException(status_code=401, detail="control-plane authentication failed") from exc


async def _user(request: Request, scope: str) -> str:
    try:
        return await authenticate_control_request(request, scope)
    except PermissionError as exc:
        _raise_auth(exc)
        raise AssertionError("unreachable")


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
    return workspace


async def _monitor_loop(app: Any, monitor_id: str) -> None:
    supervisor = getattr(app.state, "control_supervisor", None)
    if supervisor is None:
        return
    interval = float(os.environ.get("CPTR_SUPERVISOR_POLL_INTERVAL", "2"))
    try:
        while True:
            monitor = await supervisor.run_once(monitor_id)
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
async def list_workspaces(request: Request):
    user_id = await _user(request, "workspace:read")
    workspaces = await Workspace.get_by_user(user_id)
    return {
        "workspaces": [
            {"workspace_id": workspace.id, "name": workspace.name} for workspace in workspaces
        ]
    }


@router.get("/workspaces/{workspace_id}")
async def get_workspace(request: Request, workspace_id: str):
    user_id = await _user(request, "workspace:read")
    workspace = await _ensure_workspace(user_id, workspace_id)
    return {"workspace_id": workspace.id, "name": workspace.name}


@router.post("/tasks")
async def create_task(request: Request, body: TaskCreateRequest):
    user_id = await _user(request, "task:write")
    await _ensure_workspace(user_id, body.workspace_id)
    agent, _ = _services(request)
    try:
        return await agent.start_task(
            user_id=user_id,
            workspace_id=body.workspace_id,
            prompt=body.prompt,
            model_id=body.model_id,
            idempotency_key=body.idempotency_key,
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
        return await agent.get_task(task_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@router.get("/tasks/{task_id}/output")
async def get_task_output(request: Request, task_id: str):
    user_id = await _user(request, "task:read")
    agent, _ = _services(request)
    try:
        return await agent.get_output(task_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@router.post("/tasks/{task_id}/messages")
async def send_task_message(request: Request, task_id: str, body: MessageRequest):
    user_id = await _user(request, "task:write")
    agent, _ = _services(request)
    try:
        return await agent.send_message(
            task_id,
            user_id=user_id,
            content=body.content,
            idempotency_key=body.idempotency_key,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str):
    user_id = await _user(request, "task:write")
    agent, _ = _services(request)
    try:
        return await agent.cancel_task(task_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@router.get("/workspaces/{workspace_id}/git/status")
async def get_git_status(request: Request, workspace_id: str):
    user_id = await _user(request, "git:read")
    workspace = await _ensure_workspace(user_id, workspace_id)
    from cptr.utils.git import is_repo, status
    from cptr.utils.identity import identity_for_user_id

    identity = await identity_for_user_id(user_id)
    if not await is_repo(workspace.path, identity):
        return {"is_repo": False, "files": []}
    result = await status(workspace.path, identity)
    result["is_repo"] = True
    return result


@router.get("/workspaces/{workspace_id}/git/diff")
async def get_git_diff(request: Request, workspace_id: str):
    user_id = await _user(request, "git:read")
    await _ensure_workspace(user_id, workspace_id)
    agent, _ = _services(request)
    try:
        return await agent.get_diff(workspace_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@router.post("/autonomous")
async def create_autonomous(request: Request, body: AutonomousCreateRequest):
    user_id = await _user(request, "autonomous:run")
    await _ensure_workspace(user_id, body.workspace_id)
    _, supervisor = _services(request)
    try:
        monitor = await supervisor.create_goal(
            user_id=user_id,
            workspace_id=body.workspace_id,
            goal=body.goal,
            acceptance_criteria=body.acceptance_criteria,
            model_id=body.model_id,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _schedule_monitor(request.app, monitor.monitor_id)
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
async def get_autonomous_events(request: Request, monitor_id: str):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    monitor = await supervisor.store.get_monitor(monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    events = []
    for scope in monitor.scopes:
        events.extend(
            {"scope_id": scope.scope_id, "status": status.value} for status in scope.history
        )
    if monitor.status in {
        MonitorStatus.COMPLETE,
        MonitorStatus.BLOCKED,
        MonitorStatus.FAILED,
        MonitorStatus.CANCELLED,
    }:
        events.append({"monitor_id": monitor.monitor_id, "status": monitor.status.value})
    return {"monitor_id": monitor_id, "events": events}


@router.get("/autonomous/{monitor_id}/evidence")
async def get_autonomous_evidence(request: Request, monitor_id: str):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    monitor = await supervisor.store.get_monitor(monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    evidence = await supervisor.store.list_evidence(monitor_id)
    return {
        "monitor_id": monitor_id,
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "scope_id": item.scope_id,
                "kind": item.kind,
                "payload": redact_external(item.payload),
                "created_at": item.created_at,
            }
            for item in evidence
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
        if str(worker_task.status).upper() in {"COMPLETE", "FAILED", "CANCELLED", "ERROR"}:
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
            intended_generation_id=worker_task.message_id,
            baseline_diff_fingerprint=baseline_diff_fingerprint,
            baseline_workspace_snapshot=baseline_workspace_snapshot,
        )
        return response
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
    return _monitor_summary(await supervisor.cancel(monitor_id))


@router.post("/autonomous/{monitor_id}/approve")
async def approve_autonomous(request: Request, monitor_id: str, body: ApprovalRequest):
    user_id = await _user(request, "autonomous:run")
    _, supervisor = _services(request)
    monitor = await supervisor.store.get_monitor(monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="monitor not found")
    try:
        monitor = await supervisor.approve(
            monitor_id, approval_id=body.approval_id, approved=body.approved
        )
        if monitor.status == MonitorStatus.RUNNING:
            _schedule_monitor(request.app, monitor.monitor_id)
        return _monitor_summary(monitor)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
