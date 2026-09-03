"""Authenticated compact Control API for durable Dark Factory runs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from cptr.env import TASK_CANCELLATION_TIMEOUT_SECONDS
from cptr.models import Workspace
from cptr.services.control_auth import authenticate_control_request
from cptr.services.factory_control import (
    FactoryControlConflict,
    FactoryControlNotFound,
    FactoryControlService,
)
from cptr.services.factory_store import FactoryIdempotencyConflict, FactoryPayloadTooLarge
from cptr.services.workspace_availability import is_workspace_available
from cptr.utils.db import get_db

factory_router = APIRouter(prefix="/api/control/v1/factory", tags=["control", "factory"])
router = factory_router


class FactoryRunStartRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=200)
    mission: str = Field(min_length=1, max_length=100_000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=100)
    policy: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    model_id: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class FactoryMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class FactoryControlRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)


class FactoryApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=200)
    approved: bool
    note: str | None = Field(default=None, max_length=4_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class FactoryStopRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    timeout_ms: int | None = Field(default=None, ge=100, le=120_000)


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


async def _ensure_workspace(user_id: str, workspace_id: str) -> Workspace:
    async with await get_db() as db:
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != user_id:
        raise HTTPException(status_code=404, detail="workspace not found")
    if not is_workspace_available(workspace):
        raise HTTPException(status_code=409, detail="workspace is unavailable")
    return workspace


def _service(request: Request) -> FactoryControlService:
    service = getattr(request.app.state, "factory_control_service", None)
    if service is None:
        service = FactoryControlService()
        request.app.state.factory_control_service = service
    return service


def _schedule(request: Request, run_id: str) -> None:
    runner = getattr(request.app.state, "factory_production_runner", None)
    if runner is not None:
        runner.schedule(run_id)


def _public_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FactoryControlNotFound):
        return HTTPException(status_code=404, detail="factory run not found")
    if isinstance(exc, (FactoryControlConflict, FactoryIdempotencyConflict)):
        detail: Any
        if isinstance(exc, FactoryControlConflict):
            detail = {"code": exc.code, "message": str(exc)}
        else:
            detail = {"code": "FACTORY_IDEMPOTENCY_CONFLICT", "message": str(exc)}
        return HTTPException(status_code=409, detail=detail)
    if isinstance(exc, FactoryPayloadTooLarge):
        return HTTPException(status_code=413, detail="factory payload exceeds the bounded limit")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="factory control operation failed")


@factory_router.post("/runs")
async def start_factory_run(request: Request, body: FactoryRunStartRequest):
    user_id = await _user(request, "autonomous:run")
    await _ensure_workspace(user_id, body.workspace_id)
    try:
        run = await _service(request).start(
            user_id=user_id,
            workspace_id=body.workspace_id,
            mission=body.mission,
            acceptance_criteria=tuple(body.acceptance_criteria),
            policy=body.policy,
            budget=body.budget,
            model_id=body.model_id,
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    _schedule(request, run.id)
    return {"run_id": run.id, "state": run.state}


@factory_router.get("/runs/{run_id}")
async def status_factory_run(request: Request, run_id: str):
    user_id = await _user(request, "autonomous:run")
    try:
        return await _service(request).status(user_id=user_id, run_id=run_id)
    except Exception as exc:
        raise _public_error(exc) from exc


@factory_router.get("/runs/{run_id}/events")
async def events_factory_run(
    request: Request,
    run_id: str,
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
):
    user_id = await _user(request, "autonomous:run")
    try:
        return await _service(request).events(
            user_id=user_id,
            run_id=run_id,
            cursor=cursor,
            limit=limit,
        )
    except Exception as exc:
        raise _public_error(exc) from exc


@factory_router.get("/runs/{run_id}/evidence")
async def evidence_factory_run(
    request: Request,
    run_id: str,
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
):
    user_id = await _user(request, "autonomous:run")
    try:
        return await _service(request).evidence(
            user_id=user_id,
            run_id=run_id,
            cursor=cursor,
            limit=limit,
        )
    except Exception as exc:
        raise _public_error(exc) from exc


@factory_router.post("/runs/{run_id}/messages")
async def message_factory_run(request: Request, run_id: str, body: FactoryMessageRequest):
    user_id = await _user(request, "autonomous:run")
    try:
        return await _service(request).message(
            user_id=user_id,
            run_id=run_id,
            content=body.content,
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        raise _public_error(exc) from exc


@factory_router.post("/runs/{run_id}/pause")
async def pause_factory_run(request: Request, run_id: str, body: FactoryControlRequest):
    user_id = await _user(request, "autonomous:run")
    try:
        run = await _service(request).pause(
            user_id=user_id,
            run_id=run_id,
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    return {"run_id": run.id, "state": run.state}


@factory_router.post("/runs/{run_id}/resume")
async def resume_factory_run(request: Request, run_id: str, body: FactoryControlRequest):
    user_id = await _user(request, "autonomous:run")
    try:
        run = await _service(request).resume(
            user_id=user_id,
            run_id=run_id,
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    _schedule(request, run.id)
    return {"run_id": run.id, "state": run.state}


@factory_router.post("/runs/{run_id}/approve")
async def approve_factory_run(request: Request, run_id: str, body: FactoryApprovalRequest):
    user_id = await _user(request, "autonomous:run")
    try:
        result = await _service(request).approve(
            user_id=user_id,
            run_id=run_id,
            approval_id=body.approval_id,
            approved=body.approved,
            note=body.note,
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    if body.approved:
        _schedule(request, run_id)
    return result


@factory_router.post("/runs/{run_id}/stop")
async def stop_factory_run(request: Request, run_id: str, body: FactoryStopRequest):
    user_id = await _user(request, "autonomous:run")
    try:
        run = await _service(request).stop(
            user_id=user_id,
            run_id=run_id,
            idempotency_key=body.idempotency_key,
            timeout_ms=(
                body.timeout_ms
                if body.timeout_ms is not None
                else max(100, int(TASK_CANCELLATION_TIMEOUT_SECONDS * 1000))
            ),
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    return {"run_id": run.id, "state": run.state}
