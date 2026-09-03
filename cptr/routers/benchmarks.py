"""Owner-scoped standardized coding benchmark lifecycle API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from cptr.services.coding_benchmark import SUITE_ID, coding_benchmark_store
from cptr.services.control_auth import ControlMemoryUnavailable, authenticate_control_request

router = APIRouter(prefix="/api/control/v1/benchmarks", tags=["coding-benchmarks"])


class StartBenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: str = Field(default=SUITE_ID, min_length=1, max_length=80)
    model_reported: str | None = Field(default=None, max_length=120)


async def _user(request: Request, scope: str) -> str:
    try:
        return await authenticate_control_request(request, scope)
    except PermissionError as exc:
        if isinstance(exc, ControlMemoryUnavailable):
            raise HTTPException(
                status_code=503,
                detail={"code": "MEMORY_REQUIRED", "message": str(exc)},
            ) from exc
        message = str(exc)
        raise HTTPException(
            status_code=403 if message.startswith("missing required scope") else 401,
            detail="control-plane access denied",
        ) from exc


def _validation_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc)[:200])


@router.post("/runs")
async def start_benchmark(request: Request, body: StartBenchmarkRequest):
    owner_id = await _user(request, "task:write")
    try:
        return await coding_benchmark_store.start(
            owner_id,
            model_reported=body.model_reported,
            suite_id=body.suite_id,
        )
    except ValueError as exc:
        raise _validation_error(exc) from exc


@router.post("/runs/{run_id}/submit")
async def submit_benchmark(request: Request, run_id: str):
    owner_id = await _user(request, "task:write")
    try:
        return await coding_benchmark_store.submit(owner_id, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="benchmark run not found") from exc


@router.get("/runs/{run_id}")
async def get_benchmark(request: Request, run_id: str):
    owner_id = await _user(request, "task:read")
    run = await coding_benchmark_store.get(owner_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="benchmark run not found")
    return run


@router.get("/leaderboard")
async def get_benchmark_leaderboard(
    request: Request,
    suite_id: str = Query(default=SUITE_ID, min_length=1, max_length=80),
):
    owner_id = await _user(request, "task:read")
    try:
        return await coding_benchmark_store.leaderboard(owner_id, suite_id=suite_id)
    except ValueError as exc:
        raise _validation_error(exc) from exc
