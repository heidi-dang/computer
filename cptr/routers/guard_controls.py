"""Owner Guard Controls for the /mcp UI and read-only Control API projection."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.services.control_auth import require_control_user
from cptr.services.guard_controls import (
    GuardImmutableError,
    GuardNotFoundError,
    GuardVersionConflict,
    guard_policy_service,
)
from cptr.services.local_root_grants import local_root_grants_enabled

ui_router = APIRouter(prefix="/api/mcp/guards", tags=["mcp-guard-controls"])
control_router = APIRouter(prefix="/api/control/v1", tags=["control-guard-controls"])


class GuardUpdateRequest(BaseModel):
    enabled: bool
    expected_version: int = Field(ge=0)


class GuardResetRequest(BaseModel):
    expected_versions: dict[str, int] = Field(default_factory=dict)


def _owner_user(request: Request) -> str:
    auth = getattr(getattr(request, "state", None), "auth", None)
    user_id = str(getattr(auth, "user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="authentication required")
    return user_id


def _host_capabilities() -> dict[str, bool]:
    return {"local_root_grants": bool(local_root_grants_enabled())}


def _raise_guard_error(exc: Exception) -> None:
    if isinstance(exc, GuardNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": "GUARD_NOT_FOUND", "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, GuardImmutableError):
        raise HTTPException(
            status_code=409,
            detail={"code": "GUARD_IMMUTABLE", "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, GuardVersionConflict):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "GUARD_VERSION_CONFLICT",
                "message": str(exc),
                "retriable": True,
                "current": exc.current,
            },
        ) from exc
    raise exc


@ui_router.get("")
async def get_mcp_guards(request: Request):
    user_id = _owner_user(request)
    catalog = await guard_policy_service.catalog(user_id)
    return {**catalog, "host_capabilities": _host_capabilities()}


@ui_router.patch("/{guard_id}")
async def update_mcp_guard(request: Request, guard_id: str, body: GuardUpdateRequest):
    user_id = _owner_user(request)
    try:
        return await guard_policy_service.set_guard(
            user_id,
            guard_id,
            enabled=body.enabled,
            expected_version=body.expected_version,
            source="mcp_ui",
        )
    except (GuardNotFoundError, GuardImmutableError, GuardVersionConflict) as exc:
        _raise_guard_error(exc)
        raise AssertionError("unreachable")


@ui_router.post("/reset")
async def reset_mcp_guards(request: Request, body: GuardResetRequest):
    user_id = _owner_user(request)
    try:
        catalog = await guard_policy_service.reset(
            user_id,
            expected_versions=body.expected_versions,
            source="mcp_ui",
        )
        return {**catalog, "host_capabilities": _host_capabilities()}
    except (GuardNotFoundError, GuardImmutableError, GuardVersionConflict) as exc:
        _raise_guard_error(exc)
        raise AssertionError("unreachable")


@control_router.get("/guards")
async def get_control_guards(request: Request):
    """Read the authenticated Control API owner's effective Guard Controls."""
    user_id = await require_control_user(request, "coding:read")
    return await guard_policy_service.catalog(user_id)
