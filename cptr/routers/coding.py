"""Scoped, workspace-confined direct-coding Control API.

This API is intentionally separate from CPTR's agent loop. It lets a trusted
MCP adapter expose a small set of coding primitives to an LLM while CPTR still
enforces bearer scopes, workspace ownership, identity-aware runtime access, and
bounded command-session management.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shlex
import shutil
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.models import Workspace
from cptr.services.workspace_availability import is_workspace_available
from cptr.services.command_policy import command_policy_violation
from cptr.services.control_auth import authenticate_control_request
from cptr.utils.db import get_db
from cptr.utils.identity import IdentityUnavailable, env_for, expand_user_path, identity_for_context
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
MAX_SSH_ALIAS_CHARS = 128
_SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_BROWSER_URL_CHARS = 4_096
MAX_BROWSER_TEXT_CHARS = 20_000
MAX_BROWSER_SNAPSHOT_CHARS = 24_000
_BROWSER_OPERATION_TIMEOUT_SECONDS = 45
_BROWSER_CONTROL_LOCKS: dict[str, asyncio.Lock] = {}

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
_SSH_TRANSPORT_COMMAND = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:ssh|scp|rsync)\b",
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


class SshCommandRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=MAX_SSH_ALIAS_CHARS)
    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    wait_seconds: int = Field(default=0, ge=0, le=60)


class BrowserControlRequest(BaseModel):
    action: Literal[
        "status",
        "navigate",
        "snapshot",
        "click",
        "type",
        "press_key",
        "scroll",
        "screenshot",
        "close",
    ]
    url: str | None = Field(default=None, max_length=MAX_BROWSER_URL_CHARS)
    ref: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, max_length=MAX_BROWSER_TEXT_CHARS)
    key: str | None = Field(default=None, max_length=128)
    modifiers: list[Literal["Alt", "Control", "Meta", "Shift"]] = Field(
        default_factory=list, max_length=4
    )
    direction: Literal["up", "down"] = "down"
    amount: int = Field(default=3, ge=1, le=20)
    width: int | None = Field(default=None, ge=320, le=3_840)
    height: int | None = Field(default=None, ge=240, le=2_160)
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
    violation = command_policy_violation(
        command,
        allow_network=allow_network,
        # Direct coding intentionally has no package-install capability. Future
        # elevated workspace grants must use a separate controlled runner.
        allow_package_install=False,
    )
    if violation:
        status_code = 422 if violation.startswith("command ") else 403
        raise HTTPException(status_code=status_code, detail=violation)
    # External permission is not SSH permission. Remote execution stays behind
    # the separate literal-alias SSH API and its dedicated scope/identity checks.
    if _SSH_TRANSPORT_COMMAND.search(command):
        raise HTTPException(
            status_code=403,
            detail="SSH transport is only available through the dedicated SSH API",
        )


def _require_external_scope(request: Request) -> None:
    scopes = set(getattr(getattr(request, "state", None), "control_scopes", set()))
    if "command:external" not in scopes:
        raise HTTPException(status_code=403, detail="SSH commands require the command:external scope")


def _browser_session_key(user_id: str, workspace_id: str) -> str:
    return f"control:{user_id}:{workspace_id}"


def _browser_lock(session_key: str) -> asyncio.Lock:
    lock = _BROWSER_CONTROL_LOCKS.get(session_key)
    if lock is None:
        lock = asyncio.Lock()
        _BROWSER_CONTROL_LOCKS[session_key] = lock
    return lock


def _is_loopback_browser_host(hostname: str) -> bool:
    host = hostname.strip().lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_browser_url(request: Request, url: str, *, allow_network: bool) -> str:
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise HTTPException(status_code=422, detail="browser URL must be an absolute http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(status_code=422, detail="browser URL must not contain embedded credentials")
    if not _is_loopback_browser_host(parsed.hostname):
        if not allow_network:
            raise HTTPException(
                status_code=403,
                detail="external browser navigation requires explicit allow_network=true",
            )
        _require_external_scope(request)
    return value


def _bounded_browser_snapshot(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_BROWSER_SNAPSHOT_CHARS:
        return value, False
    return (
        f"{value[:MAX_BROWSER_SNAPSHOT_CHARS]}\n\n[Browser snapshot truncated by CPTR.]",
        True,
    )


async def _managed_browser_client(session_key: str):
    from cptr.utils.browser.launcher import ensure_managed_browser
    from cptr.utils.browser.session import session_manager

    cdp_url = await asyncio.wait_for(
        ensure_managed_browser(), timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS
    )
    return await asyncio.wait_for(
        session_manager.get_or_create(session_key, cdp_url),
        timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS,
    )


def _parse_ssh_aliases(content: str) -> list[str]:
    """Return literal SSH Host aliases without exposing config values or wildcard patterns."""
    aliases: set[str] = set()
    for raw_line in content.splitlines():
        try:
            parts = shlex.split(raw_line, comments=True, posix=True)
        except ValueError:
            continue
        if len(parts) < 2 or parts[0].lower() != "host":
            continue
        for candidate in parts[1:]:
            if _SSH_ALIAS_RE.fullmatch(candidate):
                aliases.add(candidate)
    return sorted(aliases, key=str.casefold)


async def _ssh_runtime(request: Request, *, user_id: str, workspace_path: str) -> tuple[str, list[str]]:
    try:
        identity = await identity_for_context(
            {"request": request, "user_id": user_id, "workspace": workspace_path}
        )
    except IdentityUnavailable as exc:
        raise HTTPException(status_code=503, detail="SSH execution identity is unavailable") from exc

    environment = env_for(identity, Path(workspace_path))
    ssh_executable = shutil.which("ssh", path=environment.get("PATH"))
    if ssh_executable is None:
        raise HTTPException(status_code=503, detail="OpenSSH client is not available")

    config_path = expand_user_path("~/.ssh/config", identity)
    try:
        data = await Runtime.read_file(request, str(config_path))
    except FileError as exc:
        if exc.status_code == 404:
            return ssh_executable, []
        raise HTTPException(status_code=exc.status_code, detail="SSH config is not available") from exc
    if data.get("binary"):
        raise HTTPException(status_code=415, detail="SSH config is not a text file")
    return ssh_executable, _parse_ssh_aliases(str(data.get("content") or ""))


def _ssh_session(request: Request, command_id: str, workspace_path: str) -> dict[str, Any]:
    session = get_command_session(request, command_id)
    if (
        session is None
        or session.get("workspace") != workspace_path
        or session.get("transport") != "ssh"
    ):
        raise HTTPException(status_code=404, detail="SSH command not found")
    return session


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


@router.post("/workspaces/{workspace_id}/browser")
async def control_managed_browser(
    request: Request, workspace_id: str, body: BrowserControlRequest
):
    """Control CPTR's isolated Chrome session through the scoped Control API."""
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    session_key = _browser_session_key(user_id, workspace_id)

    from cptr.utils.browser.launcher import find_browser
    from cptr.utils.browser.session import session_manager

    if body.action == "status":
        browser = find_browser()
        return {
            "workspace_id": workspace_id,
            "action": body.action,
            "status": "ready" if browser else "unavailable",
            "available": bool(browser),
            "active": session_manager.has(session_key),
            "managed": True,
            "browser": Path(browser).name if browser else "",
        }

    lock = _browser_lock(session_key)
    try:
        async with lock:
            if body.action == "close":
                await session_manager.close(session_key)
                return {
                    "workspace_id": workspace_id,
                    "action": body.action,
                    "status": "closed",
                    "active": False,
                    "managed": True,
                }

            client = await _managed_browser_client(session_key)

            if body.action == "navigate":
                if not body.url:
                    raise HTTPException(status_code=422, detail="navigate requires url")
                url = _validate_browser_url(request, body.url, allow_network=body.allow_network)
                navigation = await asyncio.wait_for(
                    client.navigate(url), timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS
                )
                return {
                    "workspace_id": workspace_id,
                    "action": body.action,
                    "status": "ok",
                    "managed": True,
                    "url": url,
                    "title": str(navigation.get("title", ""))[:512],
                }

            if body.action == "snapshot":
                snapshot, truncated = _bounded_browser_snapshot(
                    await asyncio.wait_for(
                        client.snapshot(), timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS
                    )
                )
                return {
                    "workspace_id": workspace_id,
                    "action": body.action,
                    "status": "ok",
                    "managed": True,
                    "snapshot": snapshot,
                    "truncated": truncated,
                }

            if body.action == "click":
                if not body.ref:
                    raise HTTPException(status_code=422, detail="click requires ref")
                await asyncio.wait_for(
                    client.click(body.ref), timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS
                )
                snapshot, truncated = _bounded_browser_snapshot(
                    await asyncio.wait_for(
                        client.snapshot(), timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS
                    )
                )
                return {
                    "workspace_id": workspace_id,
                    "action": body.action,
                    "status": "ok",
                    "managed": True,
                    "snapshot": snapshot,
                    "truncated": truncated,
                }

            if body.action == "type":
                if not body.ref:
                    raise HTTPException(status_code=422, detail="type requires ref")
                if body.text is None:
                    raise HTTPException(status_code=422, detail="type requires text")
                await asyncio.wait_for(
                    client.type_text(body.ref, body.text),
                    timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS,
                )
                snapshot, truncated = _bounded_browser_snapshot(
                    await asyncio.wait_for(
                        client.snapshot(), timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS
                    )
                )
                return {
                    "workspace_id": workspace_id,
                    "action": body.action,
                    "status": "ok",
                    "managed": True,
                    "snapshot": snapshot,
                    "truncated": truncated,
                }

            if body.action == "press_key":
                key = (body.key or "").strip()
                if not key:
                    raise HTTPException(status_code=422, detail="press_key requires key")
                await asyncio.wait_for(
                    client.press_key(key, body.modifiers),
                    timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS,
                )
                return {
                    "workspace_id": workspace_id,
                    "action": body.action,
                    "status": "ok",
                    "managed": True,
                }

            if body.action == "scroll":
                await asyncio.wait_for(
                    client.scroll(body.direction, body.amount),
                    timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS,
                )
                snapshot, truncated = _bounded_browser_snapshot(
                    await asyncio.wait_for(
                        client.snapshot(), timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS
                    )
                )
                return {
                    "workspace_id": workspace_id,
                    "action": body.action,
                    "status": "ok",
                    "managed": True,
                    "snapshot": snapshot,
                    "truncated": truncated,
                }

            if body.action == "screenshot":
                if (body.width is None) != (body.height is None):
                    raise HTTPException(
                        status_code=422,
                        detail="screenshot width and height must be provided together",
                    )
                png = await asyncio.wait_for(
                    client.screenshot(width=body.width, height=body.height),
                    timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS,
                )
                relative = Path(".cptr") / "screenshots" / f"browser-control-{uuid.uuid4().hex}.png"
                await Runtime.write_file(request, str(Path(workspace.path) / relative), png)
                return {
                    "workspace_id": workspace_id,
                    "action": body.action,
                    "status": "ok",
                    "managed": True,
                    "screenshot_path": relative.as_posix(),
                    "bytes": len(png),
                }

            raise HTTPException(status_code=422, detail="unsupported browser action")
    except HTTPException:
        raise
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="browser operation timed out") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:500]) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:500]) from exc
    finally:
        if body.action == "close" and not lock.locked():
            _BROWSER_CONTROL_LOCKS.pop(session_key, None)


@router.get("/workspaces/{workspace_id}/ssh/hosts")
async def list_ssh_hosts(request: Request, workspace_id: str):
    user_id = await _user(request, "command:execute")
    _require_external_scope(request)
    workspace = await _workspace(user_id, workspace_id)
    _, aliases = await _ssh_runtime(request, user_id=user_id, workspace_path=workspace.path)
    return {"workspace_id": workspace_id, "aliases": aliases}


@router.post("/workspaces/{workspace_id}/ssh/commands")
async def start_ssh_command(request: Request, workspace_id: str, body: SshCommandRequest):
    user_id = await _user(request, "command:execute")
    _require_external_scope(request)
    workspace = await _workspace(user_id, workspace_id)
    if "\x00" in body.command:
        raise HTTPException(status_code=422, detail="SSH command contains an invalid NUL byte")
    if not _SSH_ALIAS_RE.fullmatch(body.alias):
        raise HTTPException(status_code=422, detail="SSH alias is invalid")

    ssh_executable, aliases = await _ssh_runtime(
        request, user_id=user_id, workspace_path=workspace.path
    )
    canonical_alias = next(
        (alias for alias in aliases if alias.casefold() == body.alias.casefold()), None
    )
    if canonical_alias is None:
        raise HTTPException(status_code=422, detail="SSH alias is not configured")

    argv = [
        ssh_executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        canonical_alias,
        body.command,
    ]
    response = await run_command(
        f"ssh {canonical_alias}",
        ".",
        body.wait_seconds,
        __context__={
            "workspace": workspace.path,
            "workspace_id": workspace_id,
            "request": request,
            "user_id": user_id,
        },
        __argv=argv,
    )
    match = re.match(r"^Task ([0-9a-f]{8}):", response)
    if match is None:
        raise HTTPException(status_code=422, detail=response)
    command_id = match.group(1)
    session = get_command_session(request, command_id)
    if session is None or session.get("workspace") != workspace.path:
        raise HTTPException(status_code=500, detail="SSH command session was not created")
    session["transport"] = "ssh"
    session["ssh_alias"] = canonical_alias
    snapshot = await _command_snapshot(
        request,
        workspace_path=workspace.path,
        command_id=command_id,
    )
    return {**snapshot, "workspace_id": workspace_id, "alias": canonical_alias}


@router.get("/workspaces/{workspace_id}/ssh/commands/{command_id}")
async def get_ssh_command(
    request: Request,
    workspace_id: str,
    command_id: str,
    offset: int = 0,
    wait_seconds: int = 0,
):
    user_id = await _user(request, "command:execute")
    _require_external_scope(request)
    workspace = await _workspace(user_id, workspace_id)
    if offset < 0 or wait_seconds < 0 or wait_seconds > 60:
        raise HTTPException(status_code=422, detail="offset and wait_seconds must be within their allowed range")
    session = _ssh_session(request, command_id, workspace.path)
    snapshot = await _command_snapshot(
        request,
        workspace_path=workspace.path,
        command_id=command_id,
        offset=offset,
        wait_seconds=wait_seconds,
    )
    return {
        **snapshot,
        "workspace_id": workspace_id,
        "alias": str(session.get("ssh_alias") or ""),
    }


@router.post("/workspaces/{workspace_id}/ssh/commands/{command_id}/cancel")
async def cancel_ssh_command(request: Request, workspace_id: str, command_id: str):
    user_id = await _user(request, "command:execute")
    _require_external_scope(request)
    workspace = await _workspace(user_id, workspace_id)
    session = _ssh_session(request, command_id, workspace.path)
    error = stop_command_session(request, command_id)
    if error:
        raise HTTPException(status_code=409, detail=error)
    snapshot = await _command_snapshot(
        request,
        workspace_path=workspace.path,
        command_id=command_id,
    )
    return {
        **snapshot,
        "workspace_id": workspace_id,
        "alias": str(session.get("ssh_alias") or ""),
    }
