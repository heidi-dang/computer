"""Workspace instructions API endpoints.

Exposes versioned workspace instructions with optimistic concurrency,
history inspection, specific version retrieval, and compiled previews.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from cptr.services.workspace_instructions import (
    WorkspaceAccessDeniedError,
    WorkspaceInstructionConflictError,
    WorkspaceInstructionError,
    WorkspaceInstructionNotFoundError,
    WorkspaceNotFoundError,
    service as workspace_instruction_service,
)

router = APIRouter(
    prefix="/api/workspaces/{workspace_id}/instructions", tags=["workspace-instructions"]
)


class SaveWorkspaceInstructionRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Instructions markdown/text")
    expected_version: int | None = Field(
        default=None,
        ge=0,
        description="Expected current version for optimistic concurrency (0 for fresh creation)",
    )
    change_summary: str | None = Field(
        default=None,
        max_length=500,
        description="Optional brief summary of the revision",
    )


class PreviewWorkspaceInstructionRequest(BaseModel):
    content: str | None = Field(
        default=None,
        description="Candidate content to compile preview for; if omitted, previews active instruction",
    )


async def _resolve_user_id(request: Request) -> str:
    auth = getattr(getattr(request, "state", None), "auth", None)
    user_id = str(getattr(auth, "user_id", "") or "").strip()
    if user_id:
        return user_id
    from cptr.services.control_auth import require_control_user

    return await require_control_user(request, "coding:read")


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, WorkspaceInstructionConflictError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": str(exc),
                "workspace_id": exc.workspace_id,
                "current_version": exc.current_version,
                "expected_version": exc.expected_version,
            },
        )
    if isinstance(exc, WorkspaceAccessDeniedError):
        raise HTTPException(
            status_code=403,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, (WorkspaceNotFoundError, WorkspaceInstructionNotFoundError)):
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, WorkspaceInstructionError):
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": str(exc)},
        )
    raise exc


@router.get("")
async def get_current_instruction(
    workspace_id: str,
    request: Request,
) -> dict[str, Any]:
    """Retrieve the current active instruction version for the workspace."""
    user_id = await _resolve_user_id(request)
    try:
        current = await workspace_instruction_service.get_current_instruction(
            user_id=user_id,
            workspace_id=workspace_id,
        )
        return {
            "workspace_id": workspace_id,
            "current": current.to_dict() if current is not None else None,
        }
    except Exception as exc:
        _raise_http_error(exc)


@router.put("")
async def save_instruction(
    workspace_id: str,
    body: SaveWorkspaceInstructionRequest,
    request: Request,
) -> dict[str, Any]:
    """Save a new version of instructions with optimistic concurrency."""
    user_id = await _resolve_user_id(request)
    try:
        saved = await workspace_instruction_service.save_instruction_version(
            user_id=user_id,
            workspace_id=workspace_id,
            content=body.content,
            expected_version=body.expected_version,
            change_summary=body.change_summary,
        )
        return {
            "workspace_id": workspace_id,
            "version": saved.to_dict(),
        }
    except Exception as exc:
        _raise_http_error(exc)


@router.get("/history")
async def get_instruction_history(
    workspace_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List historical instruction versions for the workspace."""
    user_id = await _resolve_user_id(request)
    try:
        items, total = await workspace_instruction_service.get_instruction_history(
            user_id=user_id,
            workspace_id=workspace_id,
            limit=limit,
            offset=offset,
        )
        return {
            "workspace_id": workspace_id,
            "items": [item.to_dict() for item in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except Exception as exc:
        _raise_http_error(exc)


@router.get("/versions/{version}")
async def get_instruction_version(
    workspace_id: str,
    version: int,
    request: Request,
) -> dict[str, Any]:
    """Fetch a specific historical instruction version."""
    user_id = await _resolve_user_id(request)
    try:
        ver = await workspace_instruction_service.get_instruction_version(
            user_id=user_id,
            workspace_id=workspace_id,
            version=version,
        )
        if ver is None:
            raise WorkspaceInstructionNotFoundError(
                f"Version {version} not found for workspace {workspace_id}"
            )
        return {
            "workspace_id": workspace_id,
            "version": ver.to_dict(),
        }
    except Exception as exc:
        _raise_http_error(exc)


@router.post("/preview")
async def preview_instruction(
    workspace_id: str,
    body: PreviewWorkspaceInstructionRequest,
    request: Request,
) -> dict[str, Any]:
    """Compile and preview instructions for prompt integration."""
    user_id = await _resolve_user_id(request)
    try:
        preview = await workspace_instruction_service.compile_preview(
            user_id=user_id,
            workspace_id=workspace_id,
            candidate_content=body.content,
        )
        return {
            "workspace_id": workspace_id,
            "preview": preview.to_dict(),
        }
    except Exception as exc:
        _raise_http_error(exc)
