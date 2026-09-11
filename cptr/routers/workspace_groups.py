"""WorkspaceGroup REST API router.

Exposes owner-scoped CRUD and membership operations for workspace groups.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from cptr.services.control_auth import raise_control_auth_error, require_control_user
from cptr.services.workspace_groups import (
    WorkspaceGroupDuplicateMemberError,
    WorkspaceGroupError,
    WorkspaceGroupNotFoundError,
    WorkspaceGroupService,
    WorkspaceGroupSlugConflictError,
    WorkspaceGroupValidationError,
    WorkspaceGroupWorkspaceNotFoundError,
)

router = APIRouter(prefix="/api/workspace-groups", tags=["workspace-groups"])


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, (WorkspaceGroupNotFoundError, WorkspaceGroupWorkspaceNotFoundError)):
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, (WorkspaceGroupDuplicateMemberError, WorkspaceGroupSlugConflictError)):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, WorkspaceGroupValidationError):
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, WorkspaceGroupError):
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": str(exc), "retriable": False},
        ) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _authenticated_user(request: Request, scope: str = "workspace:read") -> str:
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.startswith("Bearer "):
        try:
            return await require_control_user(request, scope)
        except PermissionError as exc:
            raise_control_auth_error(exc)
    auth = getattr(request.state, "auth", None)
    user_id = str(getattr(auth, "user_id", "") or "").strip()
    if user_id:
        return user_id
    raise HTTPException(
        status_code=401,
        detail={
            "code": "AUTHENTICATION_REQUIRED",
            "message": "authentication required",
            "retriable": False,
        },
    )


class CreateWorkspaceGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    group_type: str = Field(default="cross-repo", max_length=80)
    config: dict[str, Any] = Field(default_factory=dict)
    members: list[dict[str, Any]] | None = None


class UpdateWorkspaceGroupRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    group_type: str | None = None
    config: dict[str, Any] | None = None


class AddWorkspaceGroupMemberRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=200)
    role: str = Field(default="member", max_length=80)
    primary: bool = False
    alias: str | None = Field(default=None, max_length=120)
    sort_order: int | None = None
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class UpdateWorkspaceGroupMemberRequest(BaseModel):
    role: str | None = None
    primary: bool | None = None
    alias: str | None = None
    sort_order: int | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class ReorderMembersRequest(BaseModel):
    workspace_ids: list[str] = Field(min_length=1)


@router.post("")
async def create_group(request: Request, body: CreateWorkspaceGroupRequest):
    """Create a new workspace group."""
    user_id = await _authenticated_user(request, "coding:write")
    try:
        return await WorkspaceGroupService.create_group(
            user_id=user_id,
            name=body.name,
            slug=body.slug,
            description=body.description,
            group_type=body.group_type,
            config=body.config,
            members=body.members,
        )
    except Exception as exc:
        _raise_http_error(exc)


@router.get("")
async def list_groups(
    request: Request,
    group_type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List groups owned by current user."""
    user_id = await _authenticated_user(request, "workspace:read")
    try:
        groups = await WorkspaceGroupService.list_groups(
            user_id=user_id,
            group_type=group_type,
            limit=limit,
            offset=offset,
        )
        return {"groups": groups, "total": len(groups)}
    except Exception as exc:
        _raise_http_error(exc)


@router.get("/{group_ref}")
async def get_group(request: Request, group_ref: str):
    """Get group details with ordered members."""
    user_id = await _authenticated_user(request, "workspace:read")
    try:
        return await WorkspaceGroupService.get_group(
            user_id=user_id,
            group_ref=group_ref,
        )
    except Exception as exc:
        _raise_http_error(exc)


@router.patch("/{group_ref}")
async def update_group(request: Request, group_ref: str, body: UpdateWorkspaceGroupRequest):
    """Update workspace group details."""
    user_id = await _authenticated_user(request, "coding:write")
    try:
        return await WorkspaceGroupService.update_group(
            user_id=user_id,
            group_ref=group_ref,
            name=body.name,
            slug=body.slug,
            description=body.description,
            group_type=body.group_type,
            config=body.config,
        )
    except Exception as exc:
        _raise_http_error(exc)


@router.delete("/{group_ref}")
async def delete_group(request: Request, group_ref: str):
    """Delete a workspace group."""
    user_id = await _authenticated_user(request, "coding:write")
    try:
        return await WorkspaceGroupService.delete_group(
            user_id=user_id,
            group_ref=group_ref,
        )
    except Exception as exc:
        _raise_http_error(exc)


@router.post("/{group_ref}/members")
async def add_member(request: Request, group_ref: str, body: AddWorkspaceGroupMemberRequest):
    """Add a member workspace to the group with duplicate protection."""
    user_id = await _authenticated_user(request, "coding:write")
    try:
        return await WorkspaceGroupService.add_member(
            user_id=user_id,
            group_ref=group_ref,
            workspace_ref=body.workspace_id,
            role=body.role,
            primary=body.primary,
            alias=body.alias,
            sort_order=body.sort_order,
            enabled=body.enabled,
            config=body.config,
        )
    except Exception as exc:
        _raise_http_error(exc)


@router.delete("/{group_ref}/members/{workspace_ref}")
async def remove_member(request: Request, group_ref: str, workspace_ref: str):
    """Remove a workspace member from the group."""
    user_id = await _authenticated_user(request, "coding:write")
    try:
        return await WorkspaceGroupService.remove_member(
            user_id=user_id,
            group_ref=group_ref,
            workspace_ref=workspace_ref,
        )
    except Exception as exc:
        _raise_http_error(exc)


@router.patch("/{group_ref}/members/{workspace_ref}")
async def update_member(
    request: Request, group_ref: str, workspace_ref: str, body: UpdateWorkspaceGroupMemberRequest
):
    """Update a member workspace's attributes in the group."""
    user_id = await _authenticated_user(request, "coding:write")
    try:
        return await WorkspaceGroupService.update_member(
            user_id=user_id,
            group_ref=group_ref,
            workspace_ref=workspace_ref,
            role=body.role,
            primary=body.primary,
            alias=body.alias,
            sort_order=body.sort_order,
            enabled=body.enabled,
            config=body.config,
        )
    except Exception as exc:
        _raise_http_error(exc)


@router.put("/{group_ref}/order")
async def reorder_members(request: Request, group_ref: str, body: ReorderMembersRequest):
    """Deterministically reorder member workspaces in the group."""
    user_id = await _authenticated_user(request, "coding:write")
    try:
        return await WorkspaceGroupService.reorder_members(
            user_id=user_id,
            group_ref=group_ref,
            ordered_workspace_refs=body.workspace_ids,
        )
    except Exception as exc:
        _raise_http_error(exc)
