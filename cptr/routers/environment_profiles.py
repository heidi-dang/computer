"""Environment Profile and Target Resolution REST API.

Authorization model: same as guard_controls / workbench — requires an
authenticated session (owner user).  All operations are scoped to the
authenticated user's own profiles; cross-user access is rejected.

Router dependency: requires a live AsyncSession from cptr.utils.db.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.services.environment_profile import (
    EnvironmentProfileError,
    EnvironmentProfileNotFoundError,
    EnvironmentProfileService,
    EnvironmentProfileVersionNotFoundError,
)
from cptr.services.environment_target import (
    EnvironmentTargetError,
    TargetNotFoundError,
    TargetOwnershipError,
    TargetUnanchoredError,
    target_registry,
)
from cptr.utils.db import get_db

router = APIRouter(prefix="/api/environment-profiles", tags=["environment-profiles"])


# ---------------------------------------------------------------------------
# Auth helpers (same pattern as guard_controls.py)
# ---------------------------------------------------------------------------


def _owner_user(request: Request) -> str:
    auth = getattr(getattr(request, "state", None), "auth", None)
    user_id = str(getattr(auth, "user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="authentication required")
    return user_id


def _raise_profile_error(exc: Exception) -> None:
    if isinstance(exc, EnvironmentProfileNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": "PROFILE_NOT_FOUND", "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, EnvironmentProfileVersionNotFoundError):
        raise HTTPException(
            status_code=409,
            detail={"code": "VERSION_NOT_FOUND", "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, TargetNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": "TARGET_NOT_FOUND", "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, TargetOwnershipError):
        raise HTTPException(
            status_code=403,
            detail={"code": "TARGET_OWNERSHIP", "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, TargetUnanchoredError):
        raise HTTPException(
            status_code=409,
            detail={"code": "TARGET_UNANCHORED", "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, (EnvironmentTargetError, EnvironmentProfileError)):
        raise HTTPException(
            status_code=422,
            detail={"code": "ENVIRONMENT_PROFILE_ERROR", "message": str(exc), "retriable": False},
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": str(exc), "retriable": False},
        ) from exc
    raise exc


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------


class CreateProfileRequest(BaseModel):
    name: str = Field(..., description="Profile name")
    description: str | None = Field(default=None, description="Profile description")
    workspace_id: str | None = Field(default=None, description="Source workspace ID (scoping)")
    target_name: str | None = Field(default=None, description="Initial target name")
    initial_spec: dict[str, Any] | None = Field(default=None, description="Initial version spec")


class CreateVersionRequest(BaseModel):
    spec: dict[str, Any] = Field(..., description="Profile version specification")
    make_active: bool = Field(default=True, description="Make this the active version")


class SetActiveVersionRequest(BaseModel):
    version_id: str = Field(..., description="Version ID to make active")


class SetTargetRequest(BaseModel):
    target_name: str | None = Field(
        default=None,
        description="Stable target name (e.g. 'local', 'aws', 'production'). "
        "Null or omitted un-anchors the profile.",
    )


class ResolveTargetRequest(BaseModel):
    target_name_override: str | None = Field(
        default=None,
        description="Override the profile's stored target_name for this resolution "
        "request. Must still be a registered target.",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/targets")
async def list_targets():
    """List all registered environment targets."""
    return {
        "targets": [t.to_dict() for t in target_registry.list_targets()],
    }


@router.get("/targets/{target_name}")
async def get_target(target_name: str):
    """Get a single registered environment target by name."""
    try:
        target = target_registry.get(target_name)
        return target.to_dict()
    except TargetNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "TARGET_NOT_FOUND", "message": str(exc), "retriable": False},
        ) from exc


@router.get("")
async def list_profiles(
    request: Request, workspace_id: str | None = None, include_archived: bool = False
):
    """List the authenticated user's environment profiles."""
    user_id = _owner_user(request)
    async with await get_db() as db:
        try:
            profiles = await EnvironmentProfileService.list_profiles(
                db,
                user_id=user_id,
                workspace_id=workspace_id,
                include_archived=include_archived,
            )
            return {"profiles": [p.to_dict() for p in profiles]}
        except Exception as exc:
            _raise_profile_error(exc)


@router.post("")
async def create_profile(request: Request, body: CreateProfileRequest):
    """Create a new environment profile, optionally with initial version spec and target."""
    user_id = _owner_user(request)
    async with await get_db() as db:
        try:
            profile, version = await EnvironmentProfileService.create_profile(
                db,
                user_id=user_id,
                name=body.name,
                description=body.description,
                workspace_id=body.workspace_id,
                initial_spec=body.initial_spec,
            )
            if body.target_name:
                profile = await EnvironmentProfileService.set_profile_target(
                    db,
                    profile_id=profile.id,
                    target_name=body.target_name,
                    user_id=user_id,
                )
            await db.commit()
            return {
                "profile": profile.to_dict(),
                "version": version.to_dict() if version else None,
            }
        except Exception as exc:
            _raise_profile_error(exc)


@router.get("/{profile_id}")
async def get_profile(request: Request, profile_id: str):
    """Get a single environment profile by ID (owner-scoped)."""
    user_id = _owner_user(request)
    async with await get_db() as db:
        try:
            profile = await EnvironmentProfileService.get_profile(db, profile_id)
            if profile is None or profile.user_id != user_id:
                raise HTTPException(status_code=404, detail="profile not found")
            return profile.to_dict()
        except HTTPException:
            raise
        except Exception as exc:
            _raise_profile_error(exc)


@router.post("/{profile_id}/versions")
async def create_version(request: Request, profile_id: str, body: CreateVersionRequest):
    """Publish a new immutable version for an owned profile."""
    user_id = _owner_user(request)
    async with await get_db() as db:
        try:
            profile = await EnvironmentProfileService.get_profile(db, profile_id)
            if profile is None or profile.user_id != user_id:
                raise HTTPException(status_code=404, detail="profile not found")
            version = await EnvironmentProfileService.create_version(
                db,
                profile_id=profile_id,
                spec=body.spec,
                created_by=user_id,
                make_active=body.make_active,
            )
            await db.commit()
            return version.to_dict()
        except HTTPException:
            raise
        except Exception as exc:
            _raise_profile_error(exc)


@router.get("/{profile_id}/versions")
async def list_versions(request: Request, profile_id: str):
    """List all versions for an owned profile."""
    user_id = _owner_user(request)
    async with await get_db() as db:
        try:
            profile = await EnvironmentProfileService.get_profile(db, profile_id)
            if profile is None or profile.user_id != user_id:
                raise HTTPException(status_code=404, detail="profile not found")
            versions = await EnvironmentProfileService.list_versions(db, profile_id)
            return {"versions": [v.to_dict() for v in versions]}
        except HTTPException:
            raise
        except Exception as exc:
            _raise_profile_error(exc)


@router.post("/{profile_id}/active-version")
async def set_active_version(request: Request, profile_id: str, body: SetActiveVersionRequest):
    """Switch the active version of an owned profile."""
    user_id = _owner_user(request)
    async with await get_db() as db:
        try:
            profile = await EnvironmentProfileService.get_profile(db, profile_id)
            if profile is None or profile.user_id != user_id:
                raise HTTPException(status_code=404, detail="profile not found")
            updated = await EnvironmentProfileService.set_active_version(
                db,
                profile_id=profile_id,
                version_id=body.version_id,
            )
            await db.commit()
            return updated.to_dict()
        except HTTPException:
            raise
        except Exception as exc:
            _raise_profile_error(exc)


@router.post("/{profile_id}/archive")
async def archive_profile(request: Request, profile_id: str):
    """Archive an owned profile."""
    user_id = _owner_user(request)
    async with await get_db() as db:
        try:
            profile = await EnvironmentProfileService.get_profile(db, profile_id)
            if profile is None or profile.user_id != user_id:
                raise HTTPException(status_code=404, detail="profile not found")
            archived = await EnvironmentProfileService.archive_profile(db, profile_id)
            await db.commit()
            return archived.to_dict()
        except HTTPException:
            raise
        except Exception as exc:
            _raise_profile_error(exc)


@router.patch("/{profile_id}/target")
async def set_profile_target(request: Request, profile_id: str, body: SetTargetRequest):
    """Set or clear the environment target on an owned profile.

    Setting target_name to null un-anchors the profile.
    """
    user_id = _owner_user(request)
    async with await get_db() as db:
        try:
            profile = await EnvironmentProfileService.get_profile(db, profile_id)
            if profile is None or profile.user_id != user_id:
                raise HTTPException(status_code=404, detail="profile not found")
            updated = await EnvironmentProfileService.set_profile_target(
                db,
                profile_id=profile_id,
                target_name=body.target_name,
                user_id=user_id,
            )
            await db.commit()
            return updated.to_dict()
        except HTTPException:
            raise
        except Exception as exc:
            _raise_profile_error(exc)


@router.post("/{profile_id}/resolve-target")
async def resolve_profile_target(request: Request, profile_id: str, body: ResolveTargetRequest):
    """Resolve the active environment target for a profile.

    Returns the resolved target descriptor without making any deployment action.
    Fails explicitly on:
    - No active version (409 VERSION_NOT_FOUND)
    - No target name set and no override (409 TARGET_UNANCHORED)
    - Unknown target name (404 TARGET_NOT_FOUND)
    - Ownership mismatch on restricted targets (403 TARGET_OWNERSHIP)
    - runtime_profile not accepted by the target (422 ENVIRONMENT_PROFILE_ERROR)
    """
    user_id = _owner_user(request)
    async with await get_db() as db:
        try:
            profile = await EnvironmentProfileService.get_profile(db, profile_id)
            if profile is None or profile.user_id != user_id:
                raise HTTPException(status_code=404, detail="profile not found")
            target = await EnvironmentProfileService.resolve_profile_target(
                db,
                profile_id=profile_id,
                caller_user_id=user_id,
                target_name_override=body.target_name_override,
            )
            return {
                "profile_id": profile_id,
                "resolved_target": target.to_dict(),
            }
        except HTTPException:
            raise
        except Exception as exc:
            _raise_profile_error(exc)
