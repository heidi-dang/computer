"""Scoped, workspace-confined direct-coding Control API.

This API is intentionally separate from CPTR's agent loop. It lets a trusted
MCP adapter expose a small set of coding primitives to an LLM while CPTR still
enforces bearer scopes, workspace ownership, identity-aware runtime access, and
bounded command-session management.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.models import Workspace
from cptr.services.workspace_availability import is_workspace_available
from cptr.services.control_auth import authenticate_control_request
from cptr.utils.db import get_db
from cptr.utils.runtime import FileError, Runtime
from cptr.utils.tools import (
    command_session_bytes_since,
    get_command_session,
    run_command,
    search_files,
    stop_command_session,
)

router = APIRouter(prefix="/api/control/v1", tags=["direct-coding"])

MAX_READ_BYTES = 500_000
MAX_WRITE_BYTES = 1_000_000
MAX_COMMAND_CHARS = 20_000
MAX_COMMAND_OUTPUT_CHARS = 20_000

# Direct coding supports local development and validation. Deliberately refuse
# operations that publish, deploy, destroy state, or obtain credentials. Network
# package installation is possible only when the caller explicitly opts in.
_DESTRUCTIVE_COMMAND = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:rm\s+-[^\n]*[rf]|rmdir\b|del\s+/[fq]|"
    r"git\s+(?:reset\s+--hard|clean\b)|shred\b|mkfs\b|dd\b)",
    re.IGNORECASE,
)
_EXTERNAL_COMMAND = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:git\s+(?:push|fetch|pull|clone|remote\s+add)\b|"
    r"(?:npm|pnpm|yarn)\s+(?:publish|login|logout|install|add)\b|"
    r"pip(?:3)?\s+install\b|uv\s+(?:pip\s+install|sync)\b|"
    r"curl\b|wget\b|ssh\b|scp\b|rsync\b|"
    r"docker\s+(?:push|login)\b|kubectl\b|terraform\s+(?:apply|destroy)\b|"
    r"(?:aws|gcloud|az)\b)",
    re.IGNORECASE,
)


class ListRequest(BaseModel):
    path: str = Field(default=".", min_length=1, max_length=1_000)
    recursive: bool = False


class ReadRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)
    start_line: int = Field(default=0, ge=0, le=1_000_000)
    end_line: int = Field(default=0, ge=0, le=1_000_000)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    path: str = Field(default=".", min_length=1, max_length=1_000)
    regex: bool = False
    case_insensitive: bool = False
    include: str = Field(default="", max_length=1_000)
    filenames_only: bool = False


class WriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)
    content: str = Field(max_length=MAX_WRITE_BYTES)


class EditRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)
    target: str = Field(min_length=1, max_length=MAX_WRITE_BYTES)
    replacement: str = Field(max_length=MAX_WRITE_BYTES)
    start_line: int = Field(default=0, ge=0, le=1_000_000)
    end_line: int = Field(default=0, ge=0, le=1_000_000)


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    cwd: str = Field(default=".", min_length=1, max_length=1_000)
    wait_seconds: int = Field(default=30, ge=0, le=60)
    allow_network: bool = False


class CreateDirectoryRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)


class MoveRequest(BaseModel):
    source: str = Field(min_length=1, max_length=1_000)
    destination: str = Field(min_length=1, max_length=1_000)


class DeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)


def _raise_auth(exc: PermissionError) -> None:
    if str(exc).startswith("missing required scope"):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    raise HTTPException(status_code=401, detail="control-plane authentication failed") from exc


async def _user(request: Request, required_scope: str) -> str:
    try:
        return await authenticate_control_request(request, required_scope)
    except PermissionError as exc:
        _raise_auth(exc)
        raise AssertionError("unreachable")


async def _workspace(user_id: str, workspace_id: str) -> Workspace:
    async with await get_db() as db:
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != user_id:
        raise HTTPException(status_code=404, detail="workspace not found")
    if not is_workspace_available(workspace):
        raise HTTPException(status_code=409, detail="workspace is unavailable")
    return workspace


def _relative_path(path: str, root: Path) -> tuple[Path, str]:
    value = path.strip()
    if not value:
        raise HTTPException(status_code=422, detail="path must not be blank")
    supplied = Path(value)
    if supplied.is_absolute() or PureWindowsPath(value).is_absolute():
        raise HTTPException(status_code=422, detail="path must be relative to the workspace")
    resolved = (root / supplied).resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=422, detail="path traversal rejected")
    relative = resolved.relative_to(root).as_posix()
    if any(part.startswith(".env") for part in resolved.relative_to(root).parts):
        raise HTTPException(status_code=403, detail="environment files are not available through direct coding")
    return resolved, relative


def _truncate(text: str, max_chars: int = MAX_COMMAND_OUTPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return f"{text[:half]}\n\n... [output truncated] ...\n\n{text[-half:]}"


def _validate_command(command: str, allow_network: bool) -> None:
    if "\x00" in command:
        raise HTTPException(status_code=422, detail="command contains an invalid NUL byte")
    if _DESTRUCTIVE_COMMAND.search(command):
        raise HTTPException(
            status_code=403,
            detail="destructive commands are not available through direct coding",
        )
    if not allow_network and _EXTERNAL_COMMAND.search(command):
        raise HTTPException(
            status_code=403,
            detail="command may contact an external service; obtain explicit user approval and set allow_network",
        )


def _line_slice(content: str, start_line: int, end_line: int) -> tuple[str, int, int, int]:
    lines = content.splitlines(keepends=True)
    total = len(lines)
    if start_line == 0 and end_line == 0:
        return content, 1 if total else 0, total, total
    start = max(1, start_line)
    end = min(total, end_line) if end_line else total
    if start > end and total:
        raise HTTPException(status_code=422, detail="start_line must not be after end_line")
    return "".join(lines[start - 1 : end]), start, end, total


async def _command_snapshot(
    request: Request,
    *,
    workspace_path: str,
    command_id: str,
    offset: int = 0,
    wait_seconds: int = 0,
) -> dict[str, Any]:
    session = get_command_session(request, command_id)
    if session is None or session.get("workspace") != workspace_path:
        raise HTTPException(status_code=404, detail="command not found")
    if wait_seconds > 0 and not session.get("done"):
        task = session.get("log_task")
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
    raw, next_offset = command_session_bytes_since(session, max(0, offset))
    output = _truncate(raw.decode(errors="replace"))
    return {
        "command_id": command_id,
        "status": "COMPLETE" if session.get("done") else "RUNNING",
        "exit_code": session.get("exit_code"),
        "output": output,
        "next_offset": next_offset,
    }


@router.post("/workspaces/{workspace_id}/coding/list")
async def list_workspace_files(request: Request, workspace_id: str, body: ListRequest):
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    root = Path(workspace.path).resolve()
    full, relative = _relative_path(body.path, root)
    try:
        result = await Runtime.list_tree(request, str(full), body.recursive)
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "path": relative, "entries": str(result.get("text") or "")}


@router.post("/workspaces/{workspace_id}/coding/read")
async def read_workspace_file(request: Request, workspace_id: str, body: ReadRequest):
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    root = Path(workspace.path).resolve()
    full, relative = _relative_path(body.path, root)
    try:
        stat = await Runtime.stat(request, str(full))
        if stat.get("type") != "file":
            raise HTTPException(status_code=404, detail="file not found")
        size = int(stat.get("size") or 0)
        if size > MAX_READ_BYTES:
            raise HTTPException(status_code=413, detail=f"file is too large (max {MAX_READ_BYTES} bytes)")
        data = await Runtime.read_file(request, str(full))
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if data.get("binary"):
        raise HTTPException(status_code=415, detail="binary files are not available through direct coding")
    content, start_line, end_line, total_lines = _line_slice(
        str(data.get("content") or ""), body.start_line, body.end_line
    )
    return {
        "workspace_id": workspace_id,
        "path": relative,
        "content": content,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "size": size,
    }


@router.post("/workspaces/{workspace_id}/coding/search")
async def search_workspace_files(request: Request, workspace_id: str, body: SearchRequest):
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    root = Path(workspace.path).resolve()
    _, relative = _relative_path(body.path, root)
    result = await search_files(
        body.query,
        relative,
        body.regex,
        body.case_insensitive,
        body.include,
        body.filenames_only,
        __context__={"workspace": workspace.path, "request": request, "user_id": user_id},
    )
    return {"workspace_id": workspace_id, "path": relative, "matches": result}


@router.post("/workspaces/{workspace_id}/coding/write")
async def write_workspace_file(request: Request, workspace_id: str, body: WriteRequest):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = Path(workspace.path).resolve()
    full, relative = _relative_path(body.path, root)
    try:
        await Runtime.write_file(request, str(full), body.content)
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "path": relative, "bytes_written": len(body.content.encode("utf-8"))}


@router.post("/workspaces/{workspace_id}/coding/edit")
async def edit_workspace_file(request: Request, workspace_id: str, body: EditRequest):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = Path(workspace.path).resolve()
    full, relative = _relative_path(body.path, root)
    try:
        data = await Runtime.read_file(request, str(full))
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if data.get("binary"):
        raise HTTPException(status_code=415, detail="binary files are not available through direct coding")
    content = str(data.get("content") or "")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail=f"file is too large (max {MAX_WRITE_BYTES} bytes)")

    if body.start_line or body.end_line:
        lines = content.splitlines(keepends=True)
        start = max(1, body.start_line) - 1
        end = min(len(lines), body.end_line) if body.end_line else len(lines)
        region = "".join(lines[start:end])
        count = region.count(body.target)
        if count != 1:
            raise HTTPException(status_code=409, detail="target must occur exactly once in the requested line range")
        updated = "".join(lines[:start]) + region.replace(body.target, body.replacement, 1) + "".join(lines[end:])
    else:
        count = content.count(body.target)
        if count != 1:
            raise HTTPException(status_code=409, detail="target must occur exactly once in the file")
        updated = content.replace(body.target, body.replacement, 1)
    if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail=f"edited file exceeds {MAX_WRITE_BYTES} bytes")
    try:
        await Runtime.write_file(request, str(full), updated)
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "workspace_id": workspace_id,
        "path": relative,
        "replaced_characters": len(body.target),
        "inserted_characters": len(body.replacement),
    }


@router.post("/workspaces/{workspace_id}/coding/directories")
async def create_workspace_directory(
    request: Request, workspace_id: str, body: CreateDirectoryRequest
):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = Path(workspace.path).resolve()
    full, relative = _relative_path(body.path, root)
    try:
        await Runtime.create_item(request, str(full), type="directory")
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "path": relative, "type": "directory"}


@router.post("/workspaces/{workspace_id}/coding/move")
async def move_workspace_file(request: Request, workspace_id: str, body: MoveRequest):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = Path(workspace.path).resolve()
    source, source_relative = _relative_path(body.source, root)
    destination, destination_relative = _relative_path(body.destination, root)
    try:
        source_stat = await Runtime.stat(request, str(source))
        if source_stat.get("type") != "file":
            raise HTTPException(status_code=422, detail="only files may be moved through direct coding")
        try:
            await Runtime.stat(request, str(destination))
        except FileError as exc:
            if exc.status_code != 404:
                raise
        else:
            raise HTTPException(status_code=409, detail="destination already exists")
        await Runtime.move_item(request, str(source), str(destination))
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "source": source_relative, "destination": destination_relative}


@router.post("/workspaces/{workspace_id}/coding/delete")
async def delete_workspace_file(request: Request, workspace_id: str, body: DeleteRequest):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = Path(workspace.path).resolve()
    full, relative = _relative_path(body.path, root)
    try:
        file_stat = await Runtime.stat(request, str(full))
        if file_stat.get("type") != "file":
            raise HTTPException(status_code=422, detail="only files may be deleted through direct coding")
        await Runtime.delete_item(request, str(full))
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "path": relative, "deleted": True}


@router.post("/workspaces/{workspace_id}/coding/commands")
async def start_workspace_command(request: Request, workspace_id: str, body: CommandRequest):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = Path(workspace.path).resolve()
    _, relative_cwd = _relative_path(body.cwd, root)
    _validate_command(body.command, body.allow_network)
    scopes = set(getattr(getattr(request, "state", None), "control_scopes", set()))
    if body.allow_network and "command:external" not in scopes:
        raise HTTPException(
            status_code=403,
            detail="external commands require the command:external scope",
        )
    response = await run_command(
        body.command,
        relative_cwd,
        body.wait_seconds,
        __context__={
            "workspace": workspace.path,
            "workspace_id": workspace_id,
            "request": request,
            "user_id": user_id,
        },
    )
    match = re.match(r"^Task ([0-9a-f]{8}):", response)
    if match is None:
        raise HTTPException(status_code=422, detail=response)
    return await _command_snapshot(
        request,
        workspace_path=workspace.path,
        command_id=match.group(1),
    )


@router.get("/workspaces/{workspace_id}/coding/commands/{command_id}")
async def get_workspace_command(
    request: Request,
    workspace_id: str,
    command_id: str,
    offset: int = 0,
    wait_seconds: int = 0,
):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    if offset < 0 or wait_seconds < 0 or wait_seconds > 60:
        raise HTTPException(status_code=422, detail="offset and wait_seconds must be within their allowed range")
    return await _command_snapshot(
        request,
        workspace_path=workspace.path,
        command_id=command_id,
        offset=offset,
        wait_seconds=wait_seconds,
    )


@router.post("/workspaces/{workspace_id}/coding/commands/{command_id}/cancel")
async def cancel_workspace_command(request: Request, workspace_id: str, command_id: str):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    session = get_command_session(request, command_id)
    if session is None or session.get("workspace") != workspace.path:
        raise HTTPException(status_code=404, detail="command not found")
    error = stop_command_session(request, command_id)
    if error:
        raise HTTPException(status_code=409, detail=error)
    return await _command_snapshot(
        request,
        workspace_path=workspace.path,
        command_id=command_id,
    )
