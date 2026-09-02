"""Extended Memory API endpoints.

DELETE /api/memory                          – clear all memory entries for the current user
GET    /api/memory/entries/{entry_id}       – fetch a single memory entry by ID
DELETE /api/memory/entries/{entry_id}       – delete a specific memory entry
PUT    /api/memory/entries/{entry_id}       – edit the content of a memory entry
POST   /api/memory/import                   – bulk-import memory entries from JSON
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from cptr.utils.config import AuthResult, check_access
from cptr.utils.memory import (
    read_memory_file_state,
    remember,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["memory-extended"])
COOKIE_NAME = "cptr_session"


def _get_auth(request: Request) -> AuthResult:
    token = request.cookies.get(COOKIE_NAME)
    client_host = request.client.host if request.client else "127.0.0.1"
    auth = check_access(client_host=client_host, jwt_token=token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "authentication required")
    return auth


def _get_user(request: Request) -> str:
    return _get_auth(request).user_id or ""


class MemoryEntryUpdateRequest(BaseModel):
    content: str
    workspace: str = ""
    scope: Literal["user", "workspace"] = "user"
    path: Optional[str] = None


class MemoryImportRequest(BaseModel):
    entries: list[dict[str, Any]]
    workspace: str = ""
    scope: Literal["user", "workspace"] = "user"


# ── Clear all memory ──────────────────────────────────────────────────────────


@router.delete("")
async def clear_all_memory(
    request: Request,
    workspace: str = Query("", description="Workspace scope; omit for user-global memory"),
    scope: Literal["user", "workspace", "both"] = Query("both"),
):
    """Clear / wipe all memory entries for the current user (or workspace)."""
    user_id = _get_user(request)
    from cptr.utils.memory import _user_memory_root, _workspace_memory_root
    import shutil

    cleared = []
    errors = []

    async def _clear_scope(s: Literal["user", "workspace"]) -> None:
        try:
            if s == "user":
                memory_path = _user_memory_root(user_id)
            else:
                if not workspace:
                    errors.append(
                        {"scope": s, "error": "workspace path required for workspace scope"}
                    )
                    return
                memory_path = _workspace_memory_root(user_id, workspace)
            if memory_path.exists():
                await asyncio.to_thread(shutil.rmtree, str(memory_path))
                cleared.append(s)
        except Exception as exc:
            errors.append({"scope": s, "error": str(exc)})

    if scope in ("user", "both"):
        await _clear_scope("user")
    if scope in ("workspace", "both"):
        await _clear_scope("workspace")

    return {"ok": True, "cleared_scopes": cleared, "errors": errors}


# ── Get single memory entry ───────────────────────────────────────────────────


@router.get("/entries/{entry_id:path}")
async def get_memory_entry(
    request: Request,
    entry_id: str,
    workspace: str = Query(""),
    scope: Literal["user", "workspace"] = Query("user"),
):
    """Fetch a single memory entry/file by ID (path within the memory store)."""
    user_id = _get_user(request)
    try:
        return await read_memory_file_state(
            request,
            user_id=user_id,
            workspace=workspace,
            scope=scope,
            path=entry_id,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Delete single memory entry ────────────────────────────────────────────────


@router.delete("/entries/{entry_id:path}")
async def delete_memory_entry(
    request: Request,
    entry_id: str,
    workspace: str = Query(""),
    scope: Literal["user", "workspace"] = Query("user"),
):
    """Delete a specific memory file/entry."""
    user_id = _get_user(request)
    from cptr.utils.memory import _user_memory_root, _workspace_memory_root

    try:
        if scope == "user":
            memory_path = _user_memory_root(user_id)
        else:
            if not workspace:
                raise HTTPException(400, "workspace path required for workspace scope")
            memory_path = _workspace_memory_root(user_id, workspace)
        target = (memory_path / entry_id).resolve()
        # Security: stay inside memory root
        if not str(target).startswith(str(memory_path.resolve())):
            raise HTTPException(400, "Invalid entry path")
        if target.exists():
            if target.is_dir():
                import shutil

                await asyncio.to_thread(shutil.rmtree, str(target))
            else:
                await asyncio.to_thread(target.unlink)
        return {"ok": True, "entry_id": entry_id, "scope": scope}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Update memory entry content ───────────────────────────────────────────────


@router.put("/entries/{entry_id:path}")
async def update_memory_entry(
    request: Request,
    entry_id: str,
    body: MemoryEntryUpdateRequest,
):
    """Edit the content of a specific memory entry using the remember() operation."""
    user_id = _get_user(request)
    # Use remember() with a set operation targeting this specific path
    operations = [
        {
            "type": "set",
            "path": body.path or entry_id,
            "content": body.content,
        }
    ]
    try:
        result = await remember(
            request,
            user_id=user_id,
            workspace=body.workspace,
            scope=body.scope,
            operations=operations,
        )
        return {"ok": True, "entry_id": entry_id, "result": result}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Bulk import memory entries ────────────────────────────────────────────────


@router.post("/import")
async def import_memory_entries(request: Request, body: MemoryImportRequest):
    """Bulk-import memory entries from a list of {path, content, heading?} objects."""
    user_id = _get_user(request)
    if not body.entries:
        return {"ok": True, "imported": 0, "errors": []}

    imported = 0
    errors: list[dict] = []

    for entry in body.entries:
        path = entry.get("path") or ""
        content = entry.get("content") or ""
        if not path or not content:
            errors.append({"path": path, "error": "path and content are required"})
            continue
        operations = [{"type": "set", "path": path, "content": content}]
        try:
            await remember(
                request,
                user_id=user_id,
                workspace=body.workspace,
                scope=body.scope,
                operations=operations,
            )
            imported += 1
        except Exception as exc:
            errors.append({"path": path, "error": str(exc)})

    return {
        "ok": True,
        "imported": imported,
        "total": len(body.entries),
        "errors": errors,
    }
