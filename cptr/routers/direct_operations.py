"""Durable agent-free workspace operations for the versioned Control API.

The official ChatGPT connector can create direct operations, but it cannot send
raw shell text. CPTR owns the durable lifecycle, policy, lease, and revision
checks. This router intentionally does not invoke AgentService.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.models import DirectOperation, Workspace
from cptr.services.control_auth import authenticate_control_request
from cptr.services.direct_operations import (
    DirectOperationStore,
    IdempotencyConflict,
    WorkspaceBusy,
)
from cptr.utils.db import get_db
from cptr.utils.runtime import FileError, Runtime

router = APIRouter(prefix="/api/control/v2", tags=["direct-operations"])
store = DirectOperationStore()

MAX_READ_BYTES = 500_000
MAX_WRITE_BYTES = 1_000_000
MAX_LIST_ENTRIES = 200
MAX_EVENT_LIMIT = 100

# This is a registry of declared actions, not a command-string allowlist. An
# executor must be configured by deployment policy; CPTR never falls back to a
# host shell just because an action has been requested.
ACTION_PROFILES: dict[str, dict[str, Any]] = {
    "lint": {"argv": ["npm", "run", "lint"], "may_mutate": False, "network": False},
    "test": {"argv": ["npm", "test", "--", "--runInBand"], "may_mutate": True, "network": False},
    "typecheck": {"argv": ["npm", "run", "typecheck"], "may_mutate": False, "network": False},
    "build": {"argv": ["npm", "run", "build"], "may_mutate": True, "network": False},
}


class InspectListRequest(BaseModel):
    model_config = {"extra": "forbid"}

    path: str = Field(default=".", min_length=1, max_length=1_000)
    cursor: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=100, ge=1, le=MAX_LIST_ENTRIES)


class InspectReadRequest(BaseModel):
    model_config = {"extra": "forbid"}

    path: str = Field(min_length=1, max_length=1_000)
    start_line: int = Field(default=0, ge=0, le=1_000_000)
    end_line: int = Field(default=0, ge=0, le=1_000_000)


class OperationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    kind: Literal[
        "WRITE_FILE",
        "EDIT_FILE",
        "RUN_ACTION",
        "RUN_CODE_BLOCK",
        "SSH_EXECUTE",
    ]
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_revision: str | None = Field(default=None, min_length=1, max_length=200)
    path: str | None = Field(default=None, min_length=1, max_length=1_000)
    content: str | None = Field(default=None, max_length=MAX_WRITE_BYTES)
    target: str | None = Field(default=None, min_length=1, max_length=MAX_WRITE_BYTES)
    replacement: str | None = Field(default=None, max_length=MAX_WRITE_BYTES)
    action: str | None = Field(default=None, min_length=1, max_length=100)
    language: Literal["python", "javascript", "typescript", "bash"] | None = None
    code: str | None = Field(default=None, min_length=1, max_length=200_000)
    ssh_profile: str | None = Field(default=None, min_length=1, max_length=100)
    ssh_action: str | None = Field(default=None, min_length=1, max_length=100)


class CancelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="cancel requested", max_length=1_000)


class ApprovalRequest(BaseModel):
    model_config = {"extra": "forbid"}

    approved: bool
    idempotency_key: str = Field(min_length=1, max_length=200)


def _public_error(code: str, status_code: int = 409) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _snapshot(operation: DirectOperation) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "workspace_id": operation.workspace_id,
        "kind": operation.kind,
        "state": operation.state,
        "approval_id": operation.approval_id,
        "result": dict(operation.public_result or {}),
        "error_code": operation.public_error_code,
        "created_at": operation.created_at,
        "updated_at": operation.updated_at,
        "finished_at": operation.finished_at,
    }


def _relative_path(path: str, root: Path) -> tuple[Path, str]:
    value = path.strip()
    if not value:
        raise _public_error("PATH_REQUIRED", 422)
    supplied = Path(value)
    if supplied.is_absolute() or PureWindowsPath(value).is_absolute():
        raise _public_error("PATH_MUST_BE_WORKSPACE_RELATIVE", 422)
    resolved = (root / supplied).resolve()
    if not resolved.is_relative_to(root):
        raise _public_error("PATH_TRAVERSAL_REJECTED", 422)
    relative = resolved.relative_to(root).as_posix()
    if any(part.startswith(".env") for part in resolved.relative_to(root).parts):
        raise _public_error("SENSITIVE_PATH_REJECTED", 403)
    return resolved, relative


def _revision(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _line_slice(content: str, start_line: int, end_line: int) -> tuple[str, int, int, int]:
    lines = content.splitlines(keepends=True)
    total = len(lines)
    if start_line == 0 and end_line == 0:
        return content, 1 if total else 0, total, total
    start = max(1, start_line)
    end = min(total, end_line) if end_line else total
    if start > end and total:
        raise _public_error("INVALID_LINE_RANGE", 422)
    return "".join(lines[start - 1 : end]), start, end, total


async def _user(request: Request, required_scope: str | None = None) -> str:
    try:
        return await authenticate_control_request(request, required_scope)
    except PermissionError as exc:
        if str(exc).startswith("missing required scope"):
            raise _public_error("MISSING_REQUIRED_SCOPE", 403) from exc
        raise _public_error("CONTROL_AUTHENTICATION_FAILED", 401) from exc


async def _require_any_scope(request: Request, scopes: set[str]) -> str:
    user_id = await _user(request)
    granted = set(getattr(request.state, "control_scopes", set()))
    if not granted.intersection(scopes):
        raise _public_error("MISSING_REQUIRED_SCOPE", 403)
    return user_id


async def _workspace(user_id: str, workspace_id: str) -> Workspace:
    async with await get_db() as db:
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != user_id:
        raise _public_error("WORKSPACE_NOT_FOUND", 404)
    return workspace


async def _read_text(request: Request, full_path: Path) -> tuple[str, str, int]:
    try:
        stat = await Runtime.stat(request, str(full_path))
        if stat.get("type") != "file":
            raise _public_error("FILE_NOT_FOUND", 404)
        size = int(stat.get("size") or 0)
        if size > MAX_READ_BYTES:
            raise _public_error("FILE_TOO_LARGE", 413)
        data = await Runtime.read_file(request, str(full_path))
    except FileError as exc:
        if exc.status_code == 404:
            raise _public_error("FILE_NOT_FOUND", 404) from exc
        raise _public_error("FILE_ACCESS_FAILED", exc.status_code) from exc
    if data.get("binary"):
        raise _public_error("BINARY_FILE_REJECTED", 415)
    content = str(data.get("content") or "")
    return content, _revision(content), size


async def _current_revision(request: Request, full_path: Path) -> str:
    try:
        content, revision, _ = await _read_text(request, full_path)
        del content
        return revision
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") == "FILE_NOT_FOUND":
            return "MISSING"
        raise


async def _execute_file_operation(
    request: Request, operation: DirectOperation, workspace: Workspace
) -> DirectOperation:
    root = Path(workspace.path).resolve()
    path = str(operation.request.get("path") or "")
    full_path, relative_path = _relative_path(path, root)
    try:
        grant = await store.acquire_workspace_lease(
            workspace_id=workspace.id,
            holder_type="DIRECT_OPERATION",
            holder_id=operation.id,
        )
    except WorkspaceBusy as exc:
        rejected = await store.transition(
            operation.id,
            expected_states={"REQUESTED", "QUEUED"},
            state="REJECTED",
            event_type="LEASE_REJECTED",
            public_error_code=exc.code,
        )
        return rejected or operation

    try:
        running = await store.transition(
            operation.id,
            expected_states={"REQUESTED", "QUEUED"},
            state="RUNNING",
            event_type="STARTED",
            payload={"path": relative_path},
            lease_fencing_token=grant.fencing_token,
        )
        if running is None:
            current = await store.get(operation.id, operation.user_id)
            return current or operation
        if running.state == "CANCEL_REQUESTED":
            cancelled = await store.complete_cancel(operation.id)
            return cancelled or running

        expected = operation.expected_revision
        current_revision = await _current_revision(request, full_path)
        if expected != current_revision:
            rejected = await store.transition(
                operation.id,
                expected_states={"RUNNING"},
                state="REJECTED",
                event_type="REVISION_CONFLICT",
                payload={"path": relative_path},
                public_error_code="REVISION_CONFLICT",
            )
            return rejected or running

        if operation.kind == "WRITE_FILE":
            content = str(operation.request.get("content") or "")
            if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
                rejected = await store.transition(
                    operation.id,
                    expected_states={"RUNNING"},
                    state="REJECTED",
                    event_type="WRITE_REJECTED",
                    public_error_code="FILE_TOO_LARGE",
                )
                return rejected or running
            result = {"path": relative_path, "revision": _revision(content), "bytes_written": len(content.encode("utf-8"))}
        else:
            content, _, _ = await _read_text(request, full_path)
            target = str(operation.request.get("target") or "")
            replacement = str(operation.request.get("replacement") or "")
            if content.count(target) != 1:
                rejected = await store.transition(
                    operation.id,
                    expected_states={"RUNNING"},
                    state="REJECTED",
                    event_type="EDIT_REJECTED",
                    public_error_code="EDIT_TARGET_NOT_UNIQUE",
                )
                return rejected or running
            content = content.replace(target, replacement, 1)
            if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
                rejected = await store.transition(
                    operation.id,
                    expected_states={"RUNNING"},
                    state="REJECTED",
                    event_type="EDIT_REJECTED",
                    public_error_code="FILE_TOO_LARGE",
                )
                return rejected or running
            result = {
                "path": relative_path,
                "revision": _revision(content),
                "replaced_characters": len(target),
                "inserted_characters": len(replacement),
            }

        try:
            await Runtime.write_file(request, str(full_path), content)
        except FileError as exc:
            failed = await store.transition(
                operation.id,
                expected_states={"RUNNING"},
                state="FAILED",
                event_type="WRITE_FAILED",
                public_error_code="FILE_WRITE_FAILED",
            )
            del exc
            return failed or running
        succeeded = await store.transition(
            operation.id,
            expected_states={"RUNNING"},
            state="SUCCEEDED",
            event_type="COMPLETED",
            payload={"path": relative_path},
            public_result=result,
        )
        return succeeded or running
    finally:
        await store.release_workspace_lease(
            workspace_id=workspace.id,
            holder_type="DIRECT_OPERATION",
            holder_id=operation.id,
            fencing_token=grant.fencing_token,
        )


@router.post("/workspaces/{workspace_id}/inspect/list")
async def inspect_list(request: Request, workspace_id: str, body: InspectListRequest):
    user_id = await _user(request, "direct:inspect")
    workspace = await _workspace(user_id, workspace_id)
    full_path, relative_path = _relative_path(body.path, Path(workspace.path).resolve())
    try:
        result = await Runtime.list_directory(request, str(full_path))
    except FileError as exc:
        raise _public_error("DIRECTORY_ACCESS_FAILED", exc.status_code) from exc
    entries = list(result.get("entries") or [])
    page = entries[body.cursor : body.cursor + body.limit]
    sanitized = [
        {
            "name": str(item.get("name") or ""),
            "type": str(item.get("type") or "file"),
            "size": item.get("size"),
            "modified": item.get("modified"),
        }
        for item in page
        if not str(item.get("name") or "").startswith(".env")
    ]
    next_cursor = body.cursor + body.limit if body.cursor + body.limit < len(entries) else None
    return {
        "workspace_id": workspace_id,
        "path": relative_path,
        "entries": sanitized,
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
    }


@router.post("/workspaces/{workspace_id}/inspect/read")
async def inspect_read(request: Request, workspace_id: str, body: InspectReadRequest):
    user_id = await _user(request, "direct:inspect")
    workspace = await _workspace(user_id, workspace_id)
    full_path, relative_path = _relative_path(body.path, Path(workspace.path).resolve())
    content, revision, size = await _read_text(request, full_path)
    page, start_line, end_line, total_lines = _line_slice(content, body.start_line, body.end_line)
    return {
        "workspace_id": workspace_id,
        "path": relative_path,
        "content": page,
        "revision": revision,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "size": size,
    }


@router.post("/workspaces/{workspace_id}/operations")
async def create_operation(request: Request, workspace_id: str, body: OperationRequest):
    scope = "direct:mutate" if body.kind in {"WRITE_FILE", "EDIT_FILE"} else "direct:execute"
    user_id = await _user(request, scope)
    workspace = await _workspace(user_id, workspace_id)

    if body.kind == "WRITE_FILE":
        if body.path is None or body.content is None or body.expected_revision is None:
            raise _public_error("INVALID_WRITE_OPERATION", 422)
    elif body.kind == "EDIT_FILE":
        if (
            body.path is None
            or body.target is None
            or body.replacement is None
            or body.expected_revision is None
        ):
            raise _public_error("INVALID_EDIT_OPERATION", 422)
    elif body.kind == "RUN_ACTION" and body.action not in ACTION_PROFILES:
        raise _public_error("UNSUPPORTED_ACTION", 422)
    elif body.kind == "RUN_CODE_BLOCK" and (body.language is None or body.code is None):
        raise _public_error("INVALID_CODE_BLOCK_OPERATION", 422)
    elif body.kind == "SSH_EXECUTE" and (
        body.ssh_profile is None or body.ssh_action is None
    ):
        raise _public_error("INVALID_SSH_OPERATION", 422)

    request_data = body.model_dump(exclude_none=True)
    try:
        operation, replayed = await store.create_or_replay(
            user_id=user_id,
            workspace_id=workspace_id,
            kind=body.kind,
            request=request_data,
            idempotency_key=body.idempotency_key,
            expected_revision=body.expected_revision,
        )
    except IdempotencyConflict as exc:
        raise _public_error(exc.code, 409) from exc

    if replayed:
        return {**_snapshot(operation), "replayed": True}

    if body.kind in {"WRITE_FILE", "EDIT_FILE"}:
        operation = await _execute_file_operation(request, operation, workspace)
    elif body.kind in {"RUN_CODE_BLOCK", "SSH_EXECUTE"}:
        approval = await store.create_approval(
            operation.id,
            request_digest=operation.request_digest,
            reason=(
                "sandboxed code-block execution requested"
                if body.kind == "RUN_CODE_BLOCK"
                else "SSH remote execution requested through an approved profile"
            ),
        )
        operation = await store.get(operation.id, user_id) or operation
        operation.approval_id = approval.id
    else:
        # A deployment must register an isolated executor. Deliberately refuse
        # any fallback to create_subprocess_shell or host command execution.
        operation = await store.transition(
            operation.id,
            expected_states={"REQUESTED"},
            state="REJECTED",
            event_type="EXECUTOR_REJECTED",
            payload={"action": body.action},
            public_error_code="SANDBOX_EXECUTOR_UNAVAILABLE",
        ) or operation
    return {**_snapshot(operation), "replayed": False}


@router.get("/operations/{operation_id}")
async def get_operation(request: Request, operation_id: str):
    user_id = await _require_any_scope(request, {"direct:inspect", "direct:mutate", "direct:execute"})
    operation = await store.get(operation_id, user_id)
    if operation is None:
        raise _public_error("OPERATION_NOT_FOUND", 404)
    return _snapshot(operation)


@router.get("/operations/{operation_id}/events")
async def get_operation_events(request: Request, operation_id: str, cursor: int = 0, limit: int = 50):
    user_id = await _require_any_scope(request, {"direct:inspect", "direct:mutate", "direct:execute"})
    operation = await store.get(operation_id, user_id)
    if operation is None:
        raise _public_error("OPERATION_NOT_FOUND", 404)
    if cursor < 0 or limit < 1 or limit > MAX_EVENT_LIMIT:
        raise _public_error("INVALID_EVENT_PAGE", 422)
    events = await store.list_events(operation_id, cursor=cursor, limit=limit)
    return {
        "operation_id": operation_id,
        "events": [
            {
                "event_type": item.event_type,
                "state": item.state,
                "payload": dict(item.payload or {}),
                "created_at": item.created_at,
            }
            for item in events
        ],
        "next_cursor": events[-1].created_at + 1 if len(events) == limit else None,
    }


@router.post("/operations/{operation_id}/cancel")
async def cancel_operation(request: Request, operation_id: str, body: CancelRequest):
    user_id = await _require_any_scope(request, {"direct:mutate", "direct:execute"})
    operation = await store.get(operation_id, user_id)
    if operation is None:
        raise _public_error("OPERATION_NOT_FOUND", 404)
    cancelled = await store.request_cancel(operation_id, reason=body.reason)
    if cancelled is None:
        raise _public_error("OPERATION_NOT_FOUND", 404)
    if cancelled.state == "CANCEL_REQUESTED":
        executor = getattr(request.app.state, "direct_executor", None)
        owns_process = bool(executor and await executor.cancel(operation_id))
        # File mutations are synchronous. An executor-owned process completes
        # cancellation only after it has been terminated and reaped.
        if not owns_process:
            cancelled = await store.complete_cancel(operation_id) or cancelled
    return _snapshot(cancelled)


@router.post("/operations/{operation_id}/approval")
async def approve_operation(request: Request, operation_id: str, body: ApprovalRequest):
    user_id = await _user(request, "direct:approve")
    operation = await store.get(operation_id, user_id)
    if operation is None:
        raise _public_error("OPERATION_NOT_FOUND", 404)
    decided = await store.decide_approval(operation_id, approved=body.approved, decided_by=user_id)
    if decided is None:
        raise _public_error("APPROVAL_NOT_PENDING", 409)
    if decided.state == "QUEUED" and decided.kind in {"RUN_CODE_BLOCK", "SSH_EXECUTE"}:
        executor = getattr(request.app.state, "direct_executor", None)
        if executor is None:
            decided = await store.transition(
                operation_id,
                expected_states={"QUEUED"},
                state="REJECTED",
                event_type="EXECUTOR_UNAVAILABLE",
                public_error_code="SANDBOX_EXECUTOR_UNAVAILABLE",
            ) or decided
        else:
            await executor.schedule(operation_id)
    return _snapshot(decided)


async def recover_direct_operations(_: Any) -> int:
    """Startup hook: never fabricate a successful outcome after a restart."""
    return await store.reconcile_after_restart()
