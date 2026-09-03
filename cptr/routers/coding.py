"""Scoped, workspace-confined direct-coding Control API.

This API is intentionally separate from CPTR's agent loop. It lets a trusted
MCP adapter expose a small set of coding primitives to an LLM while CPTR still
enforces bearer scopes, workspace ownership, identity-aware runtime access, and
bounded command-session management.
"""

from __future__ import annotations

import asyncio
import hashlib
import difflib
import ipaddress
import json
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cptr.env import (
    COMMAND_IDEMPOTENCY_CACHE_MAX_ENTRIES,
    COMMAND_IDEMPOTENCY_CACHE_TTL_SECONDS,
    COMMAND_INLINE_WAIT_MAX_SECONDS,
    DIRECT_CODING_IO_CONCURRENCY,
)
from cptr.models import ControlIdempotency, Workspace
from cptr.services.control_auth import authenticate_control_request
from cptr.services.direct_coding_workers import (
    DirectCodingWorkerError,
    resolve_direct_worker_root,
    service as direct_worker_service,
)
from cptr.services.fdx_intelligence import service as fdx_intelligence_service
from cptr.services.lsp_manager import LspError, lsp_manager
from cptr.services.workspace_availability import is_workspace_available
from cptr.utils.db import get_db
from cptr.utils.identity import (
    IdentityUnavailable,
    env_for,
    expand_user_path,
    identity_for_context,
    preexec_for,
)
from cptr.utils.runtime import FileError, Runtime
from cptr.utils.tools import (
    command_session_bytes_since,
    drain_command_session_input,
    get_command_session,
    resize_command_session,
    run_command,
    search_files,
    send_command_session_input,
    signal_command_session,
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
_COMMAND_IDEMPOTENCY_TTL_SECONDS = float(COMMAND_IDEMPOTENCY_CACHE_TTL_SECONDS)
_COMMAND_IDEMPOTENCY_MAX = COMMAND_IDEMPOTENCY_CACHE_MAX_ENTRIES
_COMMAND_IDEMPOTENCY: dict[tuple[str, str, str], tuple[str, float]] = {}

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


class WorkerTargetRequest(BaseModel):
    worker_id: str | None = Field(default=None, min_length=1, max_length=200)


class ListRequest(WorkerTargetRequest):
    path: str = Field(default=".", min_length=1, max_length=1_000)
    recursive: bool = False
    max_entries: int = Field(default=500, ge=1, le=5000)
    cursor: str | None = None
    include_unavailable: bool = False


class ReadRequest(WorkerTargetRequest):
    path: str = Field(min_length=1, max_length=1_000)
    start_line: int = Field(default=0, ge=0, le=1_000_000)
    end_line: int = Field(default=0, ge=0, le=1_000_000)


class SearchRequest(WorkerTargetRequest):
    query: str = Field(min_length=1, max_length=10_000)
    path: str = Field(default=".", min_length=1, max_length=1_000)
    regex: bool = False
    case_insensitive: bool = False
    include: str = Field(default="", max_length=1_000)
    filenames_only: bool = False
    max_results: int = Field(default=100, ge=1, le=1000)
    context_lines: int = Field(default=0, ge=0, le=10)


class WriteRequest(WorkerTargetRequest):
    path: str = Field(min_length=1, max_length=1_000)
    content: str = Field(max_length=MAX_WRITE_BYTES)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    overwrite: bool = False


class EditRequest(WorkerTargetRequest):
    path: str = Field(min_length=1, max_length=1_000)
    target: str = Field(min_length=1, max_length=MAX_WRITE_BYTES)
    replacement: str = Field(max_length=MAX_WRITE_BYTES)
    start_line: int = Field(default=0, ge=0, le=1_000_000)
    end_line: int = Field(default=0, ge=0, le=1_000_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    replace_all: bool = False


class CommandRequest(WorkerTargetRequest):
    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    cwd: str = Field(default=".", min_length=1, max_length=1_000)
    wait_seconds: int = Field(default=30, ge=0, le=COMMAND_INLINE_WAIT_MAX_SECONDS)
    allow_network: bool = False
    pty: bool = False
    rows: int = Field(default=24, ge=5, le=300)
    cols: int = Field(default=80, ge=20, le=500)
    stdin: str | None = Field(default=None, max_length=65_536)
    idempotency_key: str | None = Field(default=None, max_length=200)


class CommandInputRequest(WorkerTargetRequest):
    data: str = Field(max_length=65_536)


class CommandResizeRequest(WorkerTargetRequest):
    rows: int = Field(ge=5, le=300)
    cols: int = Field(ge=20, le=500)


class CommandSignalRequest(WorkerTargetRequest):
    signal: Literal["interrupt", "terminate", "kill"]


class LspStartRequest(WorkerTargetRequest):
    server_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    root: str = Field(default=".", min_length=1, max_length=1_000)


class LspRequest(WorkerTargetRequest):
    lsp_id: str = Field(min_length=1, max_length=80)
    method: str = Field(min_length=1, max_length=256)
    params: Any = None
    timeout_seconds: float = Field(default=15.0, ge=0.1, le=60.0)


class LspStopRequest(WorkerTargetRequest):
    lsp_id: str = Field(min_length=1, max_length=80)


class WorkspaceInspectRequest(WorkerTargetRequest):
    kind: Literal[
        "project",
        "tree",
        "metadata",
        "read_many",
        "symbols",
        "tests",
        "dependencies",
        "scripts",
        "release",
    ]
    path: str = Field(default=".", min_length=1, max_length=1_000)
    paths: list[str] = Field(default_factory=list, max_length=20)
    query: str | None = Field(default=None, max_length=200)
    depth: int = Field(default=2, ge=1, le=4)


class FdxIntelligenceRequest(WorkerTargetRequest):
    action: Literal[
        "status",
        "capabilities",
        "read",
        "search",
        "grep",
        "batch",
        "outline",
        "tree",
        "ls",
        "impact",
        "impact_v2",
        "why",
        "evidence_graph",
        "semantic_status",
        "semantic_references",
        "build_status",
        "build_graph",
        "diff",
        "index_status",
        "plan",
    ]
    repo_path: str = Field(default=".", min_length=1, max_length=1_000)
    path: str | None = Field(default=None, min_length=1, max_length=1_000)
    paths: list[str] = Field(default_factory=list, max_length=20)
    query: str | None = Field(default=None, max_length=10_000)
    pattern: str | None = Field(default=None, max_length=10_000)
    symbol: str | None = Field(default=None, max_length=2_000)
    target: str | None = Field(default=None, max_length=2_000)
    mode: Literal["auto", "raw", "prototype", "deep"] = "auto"
    kind: str | None = Field(default=None, max_length=120)
    direction: Literal["in", "out", "both"] = "both"
    depth: int | None = Field(default=None, ge=1, le=20)
    base: str | None = Field(default=None, max_length=300)
    head: str | None = Field(default=None, max_length=300)
    limit: int | None = Field(default=None, ge=1, le=20_000)
    offset: int | None = Field(default=None, ge=1, le=10_000_000)
    max_matches: int = Field(default=50, ge=1, le=1_000)
    max_files: int = Field(default=20, ge=1, le=100)
    limit_per_file: int | None = Field(default=None, ge=1, le=20_000)
    min_lines: int = Field(default=1, ge=1, le=100_000)
    context: int = Field(default=2, ge=0, le=20)
    with_deps: bool = True
    fixed_strings: bool = False
    case_sensitive: bool = False
    no_cache: bool = False
    dirs_only: bool = False
    all: bool = False
    staged: bool = False
    policy_overlay: bool = False
    lang: Literal["rust", "typescript", "javascript"] = "rust"
    intent: Literal["localize", "reference_complete", "rename", "impact_seed", "context"] = (
        "reference_complete"
    )


class TestTargetRequest(WorkerTargetRequest):
    target: Literal["python_pytest", "node_test", "node_vitest", "node_build"]
    path: str = Field(default=".", min_length=1, max_length=1_000)
    test_path: str | None = Field(default=None, min_length=1, max_length=1_000)
    wait_seconds: int = Field(default=30, ge=0, le=COMMAND_INLINE_WAIT_MAX_SECONDS)


class SshCommandRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=MAX_SSH_ALIAS_CHARS)
    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    wait_seconds: int = Field(default=0, ge=0, le=COMMAND_INLINE_WAIT_MAX_SECONDS)


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


class CreateDirectoryRequest(WorkerTargetRequest):
    path: str = Field(min_length=1, max_length=1_000)


class MoveRequest(WorkerTargetRequest):
    source: str = Field(min_length=1, max_length=1_000)
    destination: str = Field(min_length=1, max_length=1_000)
    overwrite: bool = False


class DeleteRequest(WorkerTargetRequest):
    path: str = Field(min_length=1, max_length=1_000)


class BatchFileRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)
    start_line: int = Field(default=0, ge=0)
    end_line: int = Field(default=0, ge=0)


class ReadManyRequest(WorkerTargetRequest):
    files: list[BatchFileRequest] = Field(min_length=1, max_length=10)
    max_chars: int = Field(default=20_000, ge=1, le=200_000)


class ApplyEdit(BaseModel):
    target: str = Field(min_length=1, max_length=MAX_WRITE_BYTES)
    replacement: str = Field(max_length=MAX_WRITE_BYTES)


class ApplyEditsRequest(WorkerTargetRequest):
    path: str = Field(min_length=1, max_length=1_000)
    edits: list[ApplyEdit] = Field(min_length=1, max_length=20)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class DirectWorkerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    responsibility: str = Field(default="", max_length=500)
    repo_path: str = Field(default=".", min_length=1, max_length=1_000)


class DirectWorkersIntegrateRequest(BaseModel):
    worker_ids: list[str] = Field(min_length=1, max_length=16)


class DirectWorkerCloseRequest(BaseModel):
    discard_changes: bool = False


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


def _raise_worker_error(exc: DirectCodingWorkerError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc), "retriable": exc.status_code >= 409},
    ) from exc


async def _coding_root(
    user_id: str, workspace_id: str, workspace: Workspace, worker_id: str | None
) -> Path:
    if not worker_id:
        return Path(workspace.path).resolve()
    try:
        return await resolve_direct_worker_root(
            user_id=user_id, workspace_id=workspace_id, worker_id=worker_id
        )
    except DirectCodingWorkerError as exc:
        _raise_worker_error(exc)
        raise AssertionError("unreachable")


async def _touch_worker(user_id: str, workspace_id: str, worker_id: str | None) -> None:
    if not worker_id:
        return
    await direct_worker_service.mark_activity(
        user_id=user_id, workspace_id=workspace_id, worker_id=worker_id, status_value="WORKING"
    )


def _command_context(
    *, request: Request, user_id: str, workspace_id: str, workspace_path: str, worker_id: str | None
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "workspace": workspace_path,
        "workspace_id": workspace_id,
        "request": request,
        "user_id": user_id,
    }
    if worker_id:
        context["direct_worker_id"] = worker_id
    return context


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
        raise HTTPException(
            status_code=403, detail="environment files are not available through direct coding"
        )
    return resolved, relative


def _truncate(text: str, max_chars: int = MAX_COMMAND_OUTPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return f"{text[:half]}\n\n... [output truncated] ...\n\n{text[-half:]}"


def _sha256(value: str | bytes) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _precondition(actual: str, expected: str | None) -> None:
    if expected and actual != expected:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_HASH",
                "message": "file changed since it was read; reread it and retry",
                "retriable": True,
                "field": "expected_sha256",
            },
        )


def _cursor(value: str | None) -> int:
    if value is None:
        return 0
    if not value.isdigit():
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_CURSOR",
                "message": "cursor must be a non-negative integer",
                "retriable": False,
                "field": "cursor",
            },
        )
    return int(value)


def _command_idempotency_memory_get(user_id: str, workspace_id: str, key: str) -> str | None:
    now = time.monotonic()
    expired = [item for item, (_, expires_at) in _COMMAND_IDEMPOTENCY.items() if expires_at <= now]
    for item in expired:
        _COMMAND_IDEMPOTENCY.pop(item, None)
    value = _COMMAND_IDEMPOTENCY.get((user_id, workspace_id, key))
    return value[0] if value and value[1] > now else None


def _command_idempotency_memory_put(
    user_id: str, workspace_id: str, key: str, command_id: str
) -> None:
    if len(_COMMAND_IDEMPOTENCY) >= _COMMAND_IDEMPOTENCY_MAX:
        oldest = min(_COMMAND_IDEMPOTENCY.items(), key=lambda item: item[1][1])[0]
        _COMMAND_IDEMPOTENCY.pop(oldest, None)
    _COMMAND_IDEMPOTENCY[(user_id, workspace_id, key)] = (
        command_id,
        time.monotonic() + _COMMAND_IDEMPOTENCY_TTL_SECONDS,
    )


def _command_idempotency_db_key(workspace_id: str, key: str) -> str:
    return f"direct-command:{workspace_id}:{key}"


async def _command_idempotency_get(user_id: str, workspace_id: str, key: str) -> str | None:
    cached = _command_idempotency_memory_get(user_id, workspace_id, key)
    if cached is not None:
        return cached

    durable_key = _command_idempotency_db_key(workspace_id, key)
    async with await get_db() as db:
        record = (
            await db.execute(
                select(ControlIdempotency).where(
                    ControlIdempotency.user_id == user_id,
                    ControlIdempotency.key == durable_key,
                )
            )
        ).scalar_one_or_none()
    if record is None:
        return None
    if record.resource_type != "direct_command":
        raise HTTPException(status_code=409, detail="command idempotency key collision")
    command_id = str(record.resource_id)
    _command_idempotency_memory_put(user_id, workspace_id, key, command_id)
    return command_id


async def _command_idempotency_put(
    user_id: str, workspace_id: str, key: str, command_id: str
) -> str:
    durable_key = _command_idempotency_db_key(workspace_id, key)
    now_ms = int(time.time() * 1000)
    async with await get_db() as db:
        existing = (
            await db.execute(
                select(ControlIdempotency).where(
                    ControlIdempotency.user_id == user_id,
                    ControlIdempotency.key == durable_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.resource_type != "direct_command":
                raise HTTPException(status_code=409, detail="command idempotency key collision")
            authoritative = str(existing.resource_id)
            _command_idempotency_memory_put(user_id, workspace_id, key, authoritative)
            return authoritative

        db.add(
            ControlIdempotency(
                user_id=user_id,
                key=durable_key,
                resource_type="direct_command",
                resource_id=command_id,
                response={"workspace_id": workspace_id, "command_id": command_id},
                created_at=now_ms,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = (
                await db.execute(
                    select(ControlIdempotency).where(
                        ControlIdempotency.user_id == user_id,
                        ControlIdempotency.key == durable_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is None or existing.resource_type != "direct_command":
                raise
            command_id = str(existing.resource_id)

    _command_idempotency_memory_put(user_id, workspace_id, key, command_id)
    return command_id


def _bounded_diff(old: str, new: str, path: str) -> tuple[str, bool]:
    raw = "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=path, tofile=path)
    )
    return _truncate(raw, 20_000), len(raw) > 20_000


def _validate_command(command: str, allow_network: bool) -> None:
    if "\x00" in command:
        raise HTTPException(status_code=422, detail="command contains an invalid NUL byte")
    if _DESTRUCTIVE_COMMAND.search(command):
        raise HTTPException(
            status_code=403,
            detail="destructive commands are not available through direct coding",
        )
    if _SSH_TRANSPORT_COMMAND.search(command):
        raise HTTPException(
            status_code=403,
            detail="SSH transport commands are available only through the dedicated SSH control tools",
        )
    if not allow_network and _EXTERNAL_COMMAND.search(command):
        raise HTTPException(
            status_code=403,
            detail="command may contact an external service; obtain explicit user approval and set allow_network",
        )


def _require_external_scope(request: Request) -> None:
    scopes = set(getattr(getattr(request, "state", None), "control_scopes", set()))
    if "command:external" not in scopes:
        raise HTTPException(
            status_code=403, detail="SSH commands require the command:external scope"
        )


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
        raise HTTPException(
            status_code=422, detail="browser URL must be an absolute http or https URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(
            status_code=422, detail="browser URL must not contain embedded credentials"
        )
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


async def _settled_browser_snapshot(client: Any, *, settle_seconds: float = 1.5) -> str:
    """Avoid returning a transient empty AX tree immediately after navigation."""
    deadline = asyncio.get_running_loop().time() + max(0.1, min(settle_seconds, 2.0))
    latest = "[empty page]"
    while True:
        latest = await asyncio.wait_for(
            client.snapshot(), timeout=_BROWSER_OPERATION_TIMEOUT_SECONDS
        )
        if latest.strip() and latest.strip() != "[empty page]":
            return latest
        if asyncio.get_running_loop().time() >= deadline:
            return latest
        await asyncio.sleep(0.1)


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


async def _ssh_runtime(
    request: Request, *, user_id: str, workspace_path: str
) -> tuple[str, list[str]]:
    try:
        identity = await identity_for_context(
            {"request": request, "user_id": user_id, "workspace": workspace_path}
        )
    except IdentityUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="SSH execution identity is unavailable"
        ) from exc

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
        raise HTTPException(
            status_code=exc.status_code, detail="SSH config is not available"
        ) from exc
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


def _parse_recovered_command_log(
    log_path: Path,
    *,
    offset: int,
    tail_bytes: int | None,
) -> dict[str, Any] | None:
    """Stream one durable command JSONL into a bounded recovery projection."""

    command = ""
    created_at = 0.0
    completed_at: float | None = None
    exit_code: int | None = None
    pty_mode = False
    rows = 24
    cols = 80
    total_bytes = 0
    legacy_offset = 0
    preview_bytes = MAX_COMMAND_OUTPUT_CHARS * 4
    head = bytearray()
    tail = bytearray()
    requested_tail = max(0, int(tail_bytes or 0)) if tail_bytes is not None else None
    selected_char_count = 0

    def append_preview(payload: bytes) -> None:
        nonlocal selected_char_count
        if not payload:
            return
        selected_char_count += len(payload.decode("utf-8", errors="replace"))
        if len(head) < preview_bytes:
            head.extend(payload[: preview_bytes - len(head)])
        tail.extend(payload)
        if len(tail) > preview_bytes:
            del tail[:-preview_bytes]

    try:
        source = log_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None

    with source:
        for line in source:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("type") or "")
            if entry_type == "start":
                command = str(entry.get("command") or command)
                created_at = float(entry.get("ts") or created_at or 0.0)
                pty_mode = bool(entry.get("pty", pty_mode))
                rows = int(entry.get("rows") or rows)
                cols = int(entry.get("cols") or cols)
                continue
            if entry_type == "end":
                value = entry.get("exit_code")
                exit_code = int(value) if isinstance(value, int) else None
                completed_at = float(entry.get("ts") or time.time())
                total_value = entry.get("total_bytes")
                if isinstance(total_value, int) and total_value >= 0:
                    total_bytes = max(total_bytes, total_value)
                    legacy_offset = max(legacy_offset, total_value)
                started_value = entry.get("started_at")
                if not created_at and isinstance(started_value, (int, float)):
                    created_at = float(started_value)
                command = str(entry.get("command") or command)
                pty_mode = bool(entry.get("pty", pty_mode))
                rows = int(entry.get("rows") or rows)
                cols = int(entry.get("cols") or cols)
                continue
            if entry_type != "output":
                continue

            payload = str(entry.get("data") or "").encode("utf-8", errors="replace")
            explicit_end = entry.get("offset_end")
            if isinstance(explicit_end, int) and explicit_end >= 0:
                chunk_end = explicit_end
                chunk_start = max(0, chunk_end - len(payload))
            else:
                chunk_start = legacy_offset
                chunk_end = chunk_start + len(payload)
            legacy_offset = max(legacy_offset, chunk_end)
            total_bytes = max(total_bytes, chunk_end)

            if requested_tail is not None:
                if requested_tail == 0:
                    continue
                tail.extend(payload)
                if len(tail) > requested_tail:
                    del tail[:-requested_tail]
                continue

            if chunk_end <= offset:
                continue
            local_start = max(0, offset - chunk_start)
            append_preview(payload[local_start:])

    if requested_tail is not None:
        selected = bytes(tail)
        decoded = selected.decode("utf-8", errors="replace")
        output_truncated = len(decoded) > MAX_COMMAND_OUTPUT_CHARS
        output = _truncate(decoded)
    else:
        output_truncated = selected_char_count > MAX_COMMAND_OUTPUT_CHARS
        if not output_truncated:
            output = bytes(head).decode("utf-8", errors="replace")
        else:
            head_text = bytes(head).decode("utf-8", errors="replace")
            tail_text = bytes(tail).decode("utf-8", errors="replace")
            half = MAX_COMMAND_OUTPUT_CHARS // 2
            output = f"{head_text[:half]}\n\n... [output truncated] ...\n\n{tail_text[-half:]}"

    return {
        "command": command,
        "created_at": created_at,
        "completed_at": completed_at,
        "exit_code": exit_code,
        "pty": pty_mode,
        "rows": rows,
        "cols": cols,
        "next_offset": total_bytes,
        "output": output,
        "output_truncated": output_truncated,
        "finished": completed_at is not None,
    }


async def _recovered_command_snapshot(
    *,
    workspace_path: str,
    command_id: str,
    offset: int = 0,
    tail_bytes: int | None = None,
) -> dict[str, Any] | None:
    if re.fullmatch(r"[0-9a-f]{8}", command_id) is None:
        return None
    log_path = Path(workspace_path).resolve() / ".cptr" / "task_logs" / f"{command_id}.jsonl"
    parsed = await asyncio.to_thread(
        _parse_recovered_command_log,
        log_path,
        offset=max(0, int(offset)),
        tail_bytes=tail_bytes,
    )
    if parsed is None:
        return None
    completed_at = parsed["completed_at"]
    created_at = float(parsed["created_at"] or completed_at or time.time())
    return {
        "command_id": command_id,
        "status": "COMPLETE" if parsed["finished"] else "INTERRUPTED",
        "exit_code": parsed["exit_code"],
        "output": parsed["output"],
        "next_offset": parsed["next_offset"],
        "duration_ms": max(0, int(((completed_at or time.time()) - created_at) * 1000)),
        "output_truncated": parsed["output_truncated"],
        "timed_out": False,
        "pty": parsed["pty"],
        "rows": parsed["rows"],
        "cols": parsed["cols"],
        "recovered": True,
        "command": parsed["command"],
    }


async def _command_snapshot(
    request: Request,
    *,
    workspace_path: str,
    command_id: str,
    offset: int = 0,
    wait_seconds: int = 0,
    tail_bytes: int | None = None,
) -> dict[str, Any]:
    session = get_command_session(request, command_id)
    if session is None or session.get("workspace") != workspace_path:
        recovered = await _recovered_command_snapshot(
            workspace_path=workspace_path,
            command_id=command_id,
            offset=offset,
            tail_bytes=tail_bytes,
        )
        if recovered is not None:
            return recovered
        raise HTTPException(status_code=404, detail="command not found")
    waited_out = False
    if wait_seconds > 0 and not session.get("done"):
        task = session.get("log_task")
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
            except asyncio.TimeoutError:
                waited_out = True
    if tail_bytes is not None:
        retained = bytes(session.get("output") or b"")
        raw = retained[-tail_bytes:] if tail_bytes else b""
        next_offset = int(session.get("total_bytes") or 0)
    else:
        raw, next_offset = command_session_bytes_since(session, max(0, offset))
    decoded = raw.decode(errors="replace")
    output_truncated = len(decoded) > MAX_COMMAND_OUTPUT_CHARS
    output = _truncate(decoded)
    created_at = float(session.get("created_at") or time.time())
    return {
        "command_id": command_id,
        "status": "COMPLETE" if session.get("done") else "RUNNING",
        "exit_code": session.get("exit_code"),
        "output": output,
        "next_offset": next_offset,
        "duration_ms": max(0, int((time.time() - created_at) * 1000)),
        "output_truncated": output_truncated,
        "timed_out": waited_out,
        "pty": bool(session.get("pty")),
        "rows": int(session.get("rows") or 24),
        "cols": int(session.get("cols") or 80),
        "recovered": False,
    }


async def _bounded_tree(
    request: Request,
    *,
    root: Path,
    start: Path,
    max_depth: int,
    max_entries: int = 240,
) -> list[dict[str, Any]]:
    """Return a deterministic, bounded project tree without heavy/generated folders."""
    results: list[dict[str, Any]] = []
    queue: list[tuple[Path, int]] = [(start, 0)]
    excluded = {".git", ".cptr", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    while queue and len(results) < max_entries:
        current, depth = queue.pop(0)
        try:
            listing = await Runtime.list_directory(request, str(current))
        except FileError:
            continue
        entries = listing.get("entries", [])
        if not isinstance(entries, list):
            continue
        for entry in sorted(
            (item for item in entries if isinstance(item, dict)),
            key=lambda item: str(item.get("name") or "").casefold(),
        ):
            if len(results) >= max_entries:
                break
            name = str(entry.get("name") or "")
            if not name or name in excluded or name.startswith(".env"):
                continue
            child = current / name
            try:
                relative = child.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            record = {
                "path": relative,
                "type": str(entry.get("type") or "file"),
                "size": entry.get("size"),
                "modified": entry.get("modified"),
            }
            results.append(record)
            if record["type"] == "directory" and depth < max_depth - 1:
                queue.append((child, depth + 1))
    return results


async def _try_read_text(
    request: Request,
    full: Path,
    relative: str,
    *,
    limit: int = 80_000,
) -> dict[str, Any] | None:
    try:
        stat = await Runtime.stat(request, str(full))
        if stat.get("type") != "file" or int(stat.get("size") or 0) > limit:
            return None
        data = await Runtime.read_file(request, str(full))
    except FileError:
        return None
    if data.get("binary"):
        return None
    return {
        "path": relative,
        "size": int(stat.get("size") or 0),
        "content": str(data.get("content") or ""),
    }


async def _known_project_files(request: Request, root: Path) -> list[str]:
    candidates = [
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "Pipfile",
        "poetry.lock",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "Makefile",
        "Dockerfile",
        "docker-compose.yml",
        "compose.yml",
        "vite.config.ts",
        "svelte.config.js",
    ]
    found: list[str] = []
    for relative in candidates:
        try:
            stat = await Runtime.stat(request, str(root / relative))
        except FileError:
            continue
        if stat.get("type") == "file":
            found.append(relative)
    return found


async def _workspace_insight(
    request: Request,
    *,
    root: Path,
    body: WorkspaceInspectRequest,
    user_id: str,
) -> dict[str, Any]:
    start, relative = _relative_path(body.path, root)
    if body.kind == "tree":
        return {
            "path": relative,
            "entries": await _bounded_tree(request, root=root, start=start, max_depth=body.depth),
        }
    if body.kind == "metadata":
        stat = await Runtime.stat(request, str(start))
        return {
            "path": relative,
            "metadata": {
                key: stat.get(key) for key in ("name", "type", "size", "modified", "media_type")
            },
        }
    if body.kind == "read_many":
        if not body.paths:
            raise HTTPException(
                status_code=422, detail="paths is required for read_many inspection"
            )
        files: list[dict[str, Any]] = []
        for supplied in body.paths:
            full, item_relative = _relative_path(supplied, root)
            item = await _try_read_text(request, full, item_relative, limit=MAX_READ_BYTES)
            if item is not None:
                raw = str(item["content"])
                item["content"] = _truncate(raw, 20_000)
                item["truncated"] = len(raw) > 20_000
                files.append(item)
        return {
            "files": files,
            "omitted_count": max(0, len(body.paths) - len(files)),
        }
    if body.kind == "symbols":
        if not body.query:
            raise HTTPException(status_code=422, detail="query is required for symbols inspection")
        matches = await search_files(
            body.query,
            relative,
            False,
            False,
            "",
            False,
            __context__={
                "workspace": str(root),
                "request": request,
                "user_id": user_id,
            },
        )
        raw_matches = matches if isinstance(matches, list) else str(matches or "").splitlines()
        return {
            "path": relative,
            "query": body.query,
            "matches": raw_matches[:200],
            "truncated": len(raw_matches) > 200,
        }

    project_files = await _known_project_files(request, root)
    if body.kind == "project":
        runtimes: list[str] = []
        if "package.json" in project_files:
            runtimes.append("node")
        if any(name in project_files for name in {"pyproject.toml", "requirements.txt", "Pipfile"}):
            runtimes.append("python")
        if "Cargo.toml" in project_files:
            runtimes.append("rust")
        if "go.mod" in project_files:
            runtimes.append("go")
        return {
            "project_files": project_files,
            "detected_runtimes": runtimes,
            "root": ".",
        }
    if body.kind == "tests":
        entries = await _bounded_tree(
            request, root=root, start=start, max_depth=body.depth, max_entries=480
        )
        tests = [
            entry["path"]
            for entry in entries
            if entry["type"] == "file"
            and (
                entry["path"].endswith(
                    ("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
                )
                or "/test_" in entry["path"]
                or "/tests/" in f"/{entry['path']}"
            )
        ][:160]
        return {
            "path": relative,
            "tests": tests,
            "truncated": len(entries) >= 480,
        }
    if body.kind == "dependencies":
        manifests: list[dict[str, Any]] = []
        package = await _try_read_text(request, root / "package.json", "package.json")
        if package:
            try:
                parsed = json.loads(str(package["content"]))
                dependencies = {
                    **(parsed.get("dependencies") or {}),
                    **(parsed.get("devDependencies") or {}),
                }
                manifests.append(
                    {
                        "path": "package.json",
                        "packages": sorted(str(key) for key in dependencies)[:160],
                    }
                )
            except (json.JSONDecodeError, AttributeError, TypeError):
                manifests.append(
                    {"path": "package.json", "packages": [], "parse_error": "invalid JSON"}
                )
        for name in ("requirements.txt", "pyproject.toml"):
            item = await _try_read_text(request, root / name, name)
            if item:
                packages = [
                    line.split("[", 1)[0].split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip()
                    for line in str(item["content"]).splitlines()
                    if line.strip() and not line.lstrip().startswith(("#", "["))
                ]
                manifests.append(
                    {
                        "path": name,
                        "packages": [value for value in packages if value][:160],
                    }
                )
        return {
            "manifests": manifests,
            "detected_project_files": project_files,
        }
    if body.kind == "scripts":
        package = await _try_read_text(request, root / "package.json", "package.json")
        if package is None:
            return {"scripts": {}, "manifest_present": False}
        try:
            parsed = json.loads(str(package["content"]))
            scripts = parsed.get("scripts") if isinstance(parsed, dict) else {}
            if not isinstance(scripts, dict):
                scripts = {}
            return {
                "scripts": {
                    str(name)[:120]: str(command)[:500]
                    for name, command in list(scripts.items())[:80]
                    if isinstance(name, str) and isinstance(command, str)
                },
                "manifest_present": True,
            }
        except json.JSONDecodeError:
            return {
                "scripts": {},
                "manifest_present": True,
                "parse_error": "invalid JSON",
            }
    if body.kind == "release":
        entries = await _bounded_tree(request, root=root, start=root, max_depth=3, max_entries=300)
        test_count = sum(
            1
            for entry in entries
            if entry["type"] == "file"
            and entry["path"].endswith(
                ("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
            )
        )
        return {
            "checks": [
                {
                    "name": "project_manifest",
                    "status": "present" if project_files else "missing",
                },
                {
                    "name": "test_files_discovered",
                    "status": "present" if test_count else "missing",
                    "count": test_count,
                },
                {"name": "git_metadata", "status": "use_git_status_tool"},
            ],
            "note": "Static readiness inventory only; run an approved test target for execution evidence.",
        }
    raise HTTPException(status_code=422, detail="unsupported workspace inspection kind")


@router.post("/workspaces/{workspace_id}/coding/workers")
async def create_direct_worker(
    request: Request, workspace_id: str, body: DirectWorkerCreateRequest
):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    try:
        return await direct_worker_service.create(
            user_id=user_id,
            workspace=workspace,
            name=body.name,
            responsibility=body.responsibility,
            repo_path=body.repo_path,
        )
    except DirectCodingWorkerError as exc:
        _raise_worker_error(exc)


@router.get("/workspaces/{workspace_id}/coding/workers")
async def list_direct_workers(request: Request, workspace_id: str):
    user_id = await _user(request, "coding:read")
    await _workspace(user_id, workspace_id)
    return {
        "workspace_id": workspace_id,
        "workers": await direct_worker_service.list(user_id=user_id, workspace_id=workspace_id),
    }


@router.get("/workspaces/{workspace_id}/coding/workers/{worker_id}")
async def get_direct_worker(request: Request, workspace_id: str, worker_id: str):
    user_id = await _user(request, "coding:read")
    await _workspace(user_id, workspace_id)
    try:
        return await direct_worker_service.get(
            user_id=user_id, workspace_id=workspace_id, worker_id=worker_id
        )
    except DirectCodingWorkerError as exc:
        _raise_worker_error(exc)


@router.get("/workspaces/{workspace_id}/coding/workers-overview")
async def direct_workers_overview(request: Request, workspace_id: str):
    user_id = await _user(request, "coding:read")
    await _workspace(user_id, workspace_id)
    workers = await direct_worker_service.list(user_id=user_id, workspace_id=workspace_id)
    return {
        "workspace_id": workspace_id,
        "workers": workers,
        "total": len(workers),
        "active": sum(1 for worker in workers if worker["status"] in {"RUNNING", "WORKING"}),
        "ready": sum(1 for worker in workers if worker["status"] == "READY"),
        "integrated": sum(1 for worker in workers if worker["status"] == "INTEGRATED"),
    }


@router.post("/workspaces/{workspace_id}/coding/workers-integrate")
async def integrate_direct_workers(
    request: Request, workspace_id: str, body: DirectWorkersIntegrateRequest
):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    try:
        return await direct_worker_service.integrate(
            user_id=user_id, workspace=workspace, worker_ids=body.worker_ids
        )
    except DirectCodingWorkerError as exc:
        _raise_worker_error(exc)


@router.post("/workspaces/{workspace_id}/coding/workers/{worker_id}/close")
async def close_direct_worker(
    request: Request,
    workspace_id: str,
    worker_id: str,
    body: DirectWorkerCloseRequest,
):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    try:
        return await direct_worker_service.close(
            user_id=user_id,
            workspace=workspace,
            worker_id=worker_id,
            discard_changes=body.discard_changes,
        )
    except DirectCodingWorkerError as exc:
        _raise_worker_error(exc)


@router.post("/workspaces/{workspace_id}/coding/inspect")
async def inspect_workspace(request: Request, workspace_id: str, body: WorkspaceInspectRequest):
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    try:
        result = await _workspace_insight(request, root=root, body=body, user_id=user_id)
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "kind": body.kind, **result}


@router.post("/workspaces/{workspace_id}/coding/fdx")
async def run_fdx_intelligence(request: Request, workspace_id: str, body: FdxIntelligenceRequest):
    """Run one structured, read-only FDX repository-intelligence operation."""
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    coding_root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    repo_root, repo_relative = _relative_path(body.repo_path, coding_root)
    if not repo_root.is_dir():
        raise HTTPException(status_code=422, detail="repo_path must resolve to a directory")

    options = body.model_dump(exclude={"action", "worker_id", "repo_path"}, exclude_none=True)
    if body.path:
        _, options["path"] = _relative_path(body.path, repo_root)
    if body.paths:
        options["paths"] = [_relative_path(path, repo_root)[1] for path in body.paths]
    if body.target and ("/" in body.target or "\\" in body.target or body.target.startswith(".")):
        _, options["target"] = _relative_path(body.target, repo_root)

    try:
        identity = await identity_for_context(
            {"request": request, "user_id": user_id, "workspace": str(repo_root)}
        )
    except IdentityUnavailable as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    result = await fdx_intelligence_service.execute(
        user_id=user_id,
        workspace_id=workspace_id,
        root=repo_root,
        identity=identity,
        action=body.action,
        options=options,
    )
    result["repo_path"] = repo_relative
    if body.worker_id:
        result["worker_id"] = body.worker_id
        await _touch_worker(user_id, workspace_id, body.worker_id)
    return result


@router.post("/workspaces/{workspace_id}/coding/test-targets")
async def run_workspace_test_target(request: Request, workspace_id: str, body: TestTargetRequest):
    """Run one fixed local validation profile; callers cannot provide arbitrary commands."""
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    _, relative_cwd = _relative_path(body.path, root)
    test_relative = None
    if body.test_path:
        _, test_relative = _relative_path(body.test_path, root)
    profiles: dict[str, list[str]] = {
        "python_pytest": ["python", "-m", "pytest", *([test_relative] if test_relative else [])],
        "node_test": ["npm", "test", "--", *([test_relative] if test_relative else [])],
        "node_vitest": [
            "./node_modules/.bin/vitest",
            "run",
            *([test_relative] if test_relative else []),
        ],
        "node_build": ["npm", "run", "build"],
    }
    argv = profiles[body.target]
    command = shlex.join(argv)
    _validate_command(command, False)
    response = await run_command(
        command,
        relative_cwd,
        body.wait_seconds,
        __context__=_command_context(
            request=request,
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_path=str(root),
            worker_id=body.worker_id,
        ),
        __argv=argv,
        __use_pty=False,
    )
    match = re.match(r"^Task ([0-9a-f]{8}):", response)
    if match is None:
        raise HTTPException(status_code=422, detail=response)
    await _touch_worker(user_id, workspace_id, body.worker_id)
    snapshot = await _command_snapshot(
        request,
        workspace_path=str(root),
        command_id=match.group(1),
        wait_seconds=0,
    )
    if body.wait_seconds > 0 and response.startswith(f"Task {match.group(1)}: running"):
        snapshot["timed_out"] = True
    return {"target": body.target, **snapshot}


@router.post("/workspaces/{workspace_id}/coding/list")
async def list_workspace_files(request: Request, workspace_id: str, body: ListRequest):
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    full, relative = _relative_path(body.path, root)
    start = _cursor(body.cursor)
    try:
        result = await Runtime.list_tree_entries(
            request,
            str(full),
            body.recursive,
            start,
            body.max_entries,
        )
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    entries = []
    for entry in result.get("entries") or []:
        tree_path = str(entry.get("path") or "")
        if not tree_path:
            continue
        output_path = (
            tree_path if relative in {"", "."} else (Path(relative) / tree_path).as_posix()
        )
        entries.append(
            {
                "path": output_path,
                "type": str(entry.get("type") or "file"),
                "size": int(entry.get("size") or 0),
                "modified": entry.get("modified"),
            }
        )
    next_offset = result.get("next_offset")
    return {
        "workspace_id": workspace_id,
        "path": relative,
        "entries": entries,
        "total": int(result.get("total") or len(entries)),
        "total_exact": bool(result.get("total_exact", True)),
        "truncated": bool(result.get("truncated")),
        "max_entries": body.max_entries,
        "cursor": str(next_offset) if next_offset is not None else None,
    }


@router.post("/workspaces/{workspace_id}/coding/read")
async def read_workspace_file(request: Request, workspace_id: str, body: ReadRequest):
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    full, relative = _relative_path(body.path, root)
    try:
        data = await Runtime.read_text_file(request, str(full), MAX_READ_BYTES)
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    size = int(data.get("size") or 0)
    if data.get("binary"):
        raise HTTPException(
            status_code=415, detail="binary files are not available through direct coding"
        )
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
        "content_sha256": _sha256(str(data.get("content") or "")),
    }


@router.post("/workspaces/{workspace_id}/coding/search")
async def search_workspace_files(request: Request, workspace_id: str, body: SearchRequest):
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    _, relative = _relative_path(body.path, root)
    result = await search_files(
        body.query,
        relative,
        body.regex,
        body.case_insensitive,
        body.include,
        body.filenames_only,
        __context__={"workspace": str(root), "request": request, "user_id": user_id},
    )
    raw_matches = result if isinstance(result, list) else str(result or "").splitlines()
    parsed_matches: list[dict[str, Any]] = []
    for item in raw_matches:
        if isinstance(item, dict):
            path = str(item.get("path") or "")
            line = int(item.get("line") or 0)
            text = str(item.get("text") or "")
        else:
            raw = str(item)
            m = re.match(r"(.+?):(\d+):(.*)", raw)
            if m:
                path, line, text = m.group(1), int(m.group(2)), m.group(3)
            elif raw:
                path, line, text = raw, 0, ""
            else:
                continue
        parsed_matches.append({"path": path, "line": line, "text": text})

    matches = parsed_matches[: body.max_results]
    if body.context_lines > 0:
        semaphore = asyncio.Semaphore(DIRECT_CODING_IO_CONCURRENCY)
        paths = list(dict.fromkeys(match["path"] for match in matches if match["line"] > 0))

        async def read_source(path: str) -> tuple[str, list[str] | None]:
            candidates = []
            if relative not in {"", "."}:
                candidates.append(root / relative / path)
            candidates.append(root / path)
            async with semaphore:
                for source in candidates:
                    try:
                        context_data = await Runtime.read_file(request, str(source))
                    except FileError:
                        continue
                    if not context_data.get("binary"):
                        return path, str(context_data.get("content") or "").splitlines()
            return path, None

        source_results = await asyncio.gather(*(read_source(path) for path in paths))
        source_lines_by_path = {path: lines for path, lines in source_results if lines is not None}
        for match in matches:
            line = int(match["line"])
            source_lines = source_lines_by_path.get(str(match["path"]))
            if line <= 0 or source_lines is None:
                continue
            start = max(0, line - body.context_lines - 1)
            end = min(len(source_lines), line + body.context_lines)
            match["context"] = source_lines[start:end]

    return {
        "workspace_id": workspace_id,
        "path": relative,
        "matches": matches,
        "max_results": body.max_results,
        "truncated": len(parsed_matches) > body.max_results,
    }


@router.post("/workspaces/{workspace_id}/coding/write")
async def write_workspace_file(request: Request, workspace_id: str, body: WriteRequest):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    full, relative = _relative_path(body.path, root)
    try:
        try:
            current = await Runtime.read_file(request, str(full))
            current_text = str(current.get("content") or "")
            if not body.overwrite:
                raise HTTPException(status_code=409, detail="existing file requires overwrite=true")
            _precondition(_sha256(current_text), body.expected_sha256)
        except FileError as exc:
            if exc.status_code != 404:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            if body.expected_sha256:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "STALE_HASH",
                        "message": "expected_sha256 cannot be used for a missing file",
                        "retriable": False,
                        "field": "expected_sha256",
                    },
                )
        await Runtime.write_file(request, str(full), body.content)
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "workspace_id": workspace_id,
        "path": relative,
        "bytes_written": len(body.content.encode("utf-8")),
        "sha256": _sha256(body.content),
    }


@router.post("/workspaces/{workspace_id}/coding/edit")
async def edit_workspace_file(request: Request, workspace_id: str, body: EditRequest):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    full, relative = _relative_path(body.path, root)
    try:
        data = await Runtime.read_file(request, str(full))
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if data.get("binary"):
        raise HTTPException(
            status_code=415, detail="binary files are not available through direct coding"
        )
    content = str(data.get("content") or "")
    _precondition(_sha256(content), body.expected_sha256)
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise HTTPException(
            status_code=413, detail=f"file is too large (max {MAX_WRITE_BYTES} bytes)"
        )

    if body.start_line or body.end_line:
        lines = content.splitlines(keepends=True)
        start = max(1, body.start_line) - 1
        end = min(len(lines), body.end_line) if body.end_line else len(lines)
        region = "".join(lines[start:end])
        count = region.count(body.target)
        if count != 1 and (not body.replace_all or count == 0):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "AMBIGUOUS_EDIT",
                    "message": "target must occur exactly once in the requested line range",
                    "match_count": count,
                    "context_hint": region[:200],
                },
            )
        updated = (
            "".join(lines[:start])
            + region.replace(body.target, body.replacement, -1 if body.replace_all else 1)
            + "".join(lines[end:])
        )
    else:
        count = content.count(body.target)
        if count != 1 and (not body.replace_all or count == 0):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "AMBIGUOUS_EDIT",
                    "message": "target must occur exactly once in the file unless replace_all=true",
                    "match_count": count,
                    "context_hint": content[:200],
                    "retriable": True,
                    "field": "target",
                },
            )
        updated = content.replace(body.target, body.replacement, -1 if body.replace_all else 1)
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
        "sha256": _sha256(updated),
        "diff": _bounded_diff(content, updated, relative)[0],
    }


@router.post("/workspaces/{workspace_id}/coding/read-many")
async def read_many_workspace_files(request: Request, workspace_id: str, body: ReadManyRequest):
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    resolved = [_relative_path(item.path, root) for item in body.files]
    try:
        batch = await Runtime.read_text_files(
            request,
            [str(full) for full, _ in resolved],
            MAX_READ_BYTES,
        )
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    data_files = list(batch.get("files") or [])
    if len(data_files) != len(body.files):
        raise HTTPException(
            status_code=500, detail="bounded runtime read returned an incomplete batch"
        )

    loaded = []
    for item, (_, relative), data in zip(body.files, resolved, data_files, strict=True):
        if data.get("binary"):
            raise HTTPException(
                status_code=415,
                detail=f"binary file is not available through direct coding: {relative}",
            )
        raw = str(data.get("content") or "")
        sliced, start, end, lines = _line_slice(raw, item.start_line, item.end_line)
        loaded.append(
            {
                "path": relative,
                "raw": raw,
                "sliced": sliced,
                "start_line": start,
                "end_line": end,
                "total_lines": lines,
            }
        )
    total = 0
    files = []
    any_truncated = False
    for item in loaded:
        remaining = max(0, body.max_chars - total)
        sliced = str(item["sliced"])
        text = sliced[:remaining]
        file_truncated = len(text) < len(sliced)
        any_truncated = any_truncated or file_truncated
        total += len(text)
        files.append(
            {
                "path": item["path"],
                "content": text,
                "content_sha256": _sha256(str(item["raw"])),
                "truncated": file_truncated,
                "start_line": item["start_line"],
                "end_line": item["end_line"],
                "total_lines": item["total_lines"],
            }
        )
    return {
        "workspace_id": workspace_id,
        "files": files,
        "total_chars": total,
        "truncated": any_truncated,
    }


@router.post("/workspaces/{workspace_id}/coding/apply-edits")
async def apply_workspace_edits(request: Request, workspace_id: str, body: ApplyEditsRequest):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    full, relative = _relative_path(body.path, root)
    try:
        data = await Runtime.read_file(request, str(full))
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if data.get("binary"):
        raise HTTPException(
            status_code=415, detail="binary files are not available through direct coding"
        )

    original = str(data.get("content") or "")
    _precondition(_sha256(original), body.expected_sha256)

    # Validate every edit against the same immutable source before computing any
    # replacement. This prevents an earlier replacement from creating or
    # destroying the target of a later edit and keeps the write all-or-nothing.
    spans: list[tuple[int, int, str, int]] = []
    for index, edit in enumerate(body.edits):
        count = original.count(edit.target)
        if count != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "AMBIGUOUS_EDIT",
                    "message": "each apply_edits target must occur exactly once in the original file",
                    "match_count": count,
                    "retriable": True,
                    "field": f"edits[{index}].target",
                },
            )
        start = original.index(edit.target)
        spans.append((start, start + len(edit.target), edit.replacement, index))

    spans.sort(key=lambda item: item[0])
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "OVERLAPPING_EDITS",
                    "message": "apply_edits targets overlap in the original file",
                    "retriable": False,
                    "field": f"edits[{current[3]}].target",
                },
            )

    chunks: list[str] = []
    cursor = 0
    for start, end, replacement, _ in spans:
        chunks.append(original[cursor:start])
        chunks.append(replacement)
        cursor = end
    chunks.append(original[cursor:])
    updated = "".join(chunks)
    if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail="edited file exceeds size limit")
    try:
        await Runtime.write_file(request, str(full), updated)
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    diff, _ = _bounded_diff(original, updated, relative)
    return {
        "workspace_id": workspace_id,
        "path": relative,
        "diff": diff,
        "sha256": _sha256(updated),
    }


@router.post("/workspaces/{workspace_id}/coding/directories")
async def create_workspace_directory(
    request: Request, workspace_id: str, body: CreateDirectoryRequest
):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    full, relative = _relative_path(body.path, root)
    try:
        existing = await Runtime.stat(request, str(full))
        if existing.get("type") == "directory":
            return {
                "workspace_id": workspace_id,
                "path": relative,
                "type": "directory",
                "created": False,
            }
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PATH_CONFLICT",
                "message": "path exists and is not a directory",
                "retriable": False,
            },
        )
    except HTTPException:
        raise
    except FileError as exc:
        if exc.status_code != 404:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    try:
        await Runtime.create_item(request, str(full), type="directory")
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "path": relative, "type": "directory", "created": True}


@router.post("/workspaces/{workspace_id}/coding/move")
async def move_workspace_file(request: Request, workspace_id: str, body: MoveRequest):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    source, source_relative = _relative_path(body.source, root)
    destination, destination_relative = _relative_path(body.destination, root)
    try:
        source_stat = await Runtime.stat(request, str(source))
        if source_stat.get("type") != "file":
            raise HTTPException(
                status_code=422, detail="only files may be moved through direct coding"
            )
        destination_exists = False
        try:
            await Runtime.stat(request, str(destination))
            destination_exists = True
        except FileError as exc:
            if exc.status_code != 404:
                raise
        if destination_exists and not body.overwrite:
            raise HTTPException(status_code=409, detail="destination already exists")
        if destination_exists and body.overwrite:
            destination_data = await Runtime.read_file(request, str(destination))
            if destination_data.get("binary"):
                raise HTTPException(status_code=415, detail="binary files cannot be overwritten")
        await Runtime.move_item(request, str(source), str(destination))
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    destination_data = await Runtime.read_file(request, str(destination))
    return {
        "workspace_id": workspace_id,
        "source": source_relative,
        "destination": destination_relative,
        "sha256": _sha256(str(destination_data.get("content") or "")),
    }


@router.post("/workspaces/{workspace_id}/coding/delete")
async def delete_workspace_file(request: Request, workspace_id: str, body: DeleteRequest):
    user_id = await _user(request, "coding:write")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    full, relative = _relative_path(body.path, root)
    try:
        try:
            file_stat = await Runtime.stat(request, str(full))
        except FileError as exc:
            if exc.status_code == 404:
                return {
                    "workspace_id": workspace_id,
                    "path": relative,
                    "deleted": False,
                    "existed": False,
                }
            raise
        if file_stat.get("type") != "file":
            raise HTTPException(
                status_code=422, detail="only files may be deleted through direct coding"
            )
        await Runtime.delete_item(request, str(full))
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "path": relative, "deleted": True, "existed": True}


@router.post("/workspaces/{workspace_id}/coding/commands")
async def start_workspace_command(request: Request, workspace_id: str, body: CommandRequest):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    _, relative_cwd = _relative_path(body.cwd, root)
    _validate_command(body.command, body.allow_network)
    idempotency_workspace = f"{workspace_id}:{body.worker_id or '-'}"
    if body.idempotency_key:
        existing_id = await _command_idempotency_get(
            user_id, idempotency_workspace, body.idempotency_key
        )
        if existing_id:
            try:
                return await _command_snapshot(
                    request,
                    workspace_path=str(root),
                    command_id=existing_id,
                )
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                raise HTTPException(
                    status_code=409,
                    detail="durable command checkpoint exists but its transcript is unavailable",
                ) from exc
    scopes = set(getattr(getattr(request, "state", None), "control_scopes", set()))
    if body.allow_network and "command:external" not in scopes:
        raise HTTPException(
            status_code=403,
            detail="external commands require the command:external scope",
        )
    run_options: dict[str, Any] = {}
    if body.pty:
        run_options.update({"__rows": body.rows, "__cols": body.cols})
    if body.stdin is not None:
        run_options["__stdin"] = body.stdin
    response = await run_command(
        body.command,
        relative_cwd,
        body.wait_seconds,
        __context__=_command_context(
            request=request,
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_path=str(root),
            worker_id=body.worker_id,
        ),
        __use_pty=body.pty,
        **run_options,
    )
    match = re.match(r"^Task ([0-9a-f]{8}):", response)
    if match is None:
        raise HTTPException(status_code=422, detail=response)
    command_id = match.group(1)
    await _touch_worker(user_id, workspace_id, body.worker_id)
    if body.idempotency_key:
        authoritative_id = await _command_idempotency_put(
            user_id, idempotency_workspace, body.idempotency_key, command_id
        )
        if authoritative_id != command_id:
            stop_command_session(request, command_id, force=True)
            command_id = authoritative_id
    snapshot = await _command_snapshot(
        request,
        workspace_path=str(root),
        command_id=command_id,
        wait_seconds=0,
    )
    if body.wait_seconds > 0 and response.startswith(f"Task {command_id}: running"):
        snapshot["timed_out"] = True
    return snapshot


@router.get("/workspaces/{workspace_id}/coding/commands/{command_id}")
async def get_workspace_command(
    request: Request,
    workspace_id: str,
    command_id: str,
    offset: int = 0,
    wait_seconds: int = 0,
    tail_bytes: int | None = None,
    worker_id: str | None = None,
):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, worker_id)
    if (
        offset < 0
        or wait_seconds < 0
        or wait_seconds > 60
        or (tail_bytes is not None and (tail_bytes < 0 or tail_bytes > 10_000_000))
    ):
        raise HTTPException(
            status_code=422,
            detail="offset, wait_seconds, and tail_bytes must be within their allowed range",
        )
    return await _command_snapshot(
        request,
        workspace_path=str(root),
        command_id=command_id,
        offset=offset,
        wait_seconds=wait_seconds,
        tail_bytes=tail_bytes,
    )


@router.post("/workspaces/{workspace_id}/coding/commands/{command_id}/cancel")
async def cancel_workspace_command(
    request: Request, workspace_id: str, command_id: str, worker_id: str | None = None
):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, worker_id)
    session = get_command_session(request, command_id)
    if session is None or session.get("workspace") != str(root):
        raise HTTPException(status_code=404, detail="command not found")
    error = stop_command_session(request, command_id)
    if error:
        raise HTTPException(status_code=409, detail=error)
    return await _command_snapshot(
        request,
        workspace_path=str(root),
        command_id=command_id,
        wait_seconds=2,
    )


@router.post("/workspaces/{workspace_id}/coding/commands/{command_id}/input")
async def send_workspace_command_input(
    request: Request,
    workspace_id: str,
    command_id: str,
    body: CommandInputRequest,
):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    session = get_command_session(request, command_id)
    if session is None or session.get("workspace") != str(root):
        raise HTTPException(status_code=404, detail="command not found")
    error = send_command_session_input(request, command_id, body.data.encode("utf-8"))
    if error:
        raise HTTPException(status_code=409, detail=error)
    await drain_command_session_input(request, command_id)
    return await _command_snapshot(request, workspace_path=str(root), command_id=command_id)


@router.post("/workspaces/{workspace_id}/coding/commands/{command_id}/resize")
async def resize_workspace_command(
    request: Request,
    workspace_id: str,
    command_id: str,
    body: CommandResizeRequest,
):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    session = get_command_session(request, command_id)
    if session is None or session.get("workspace") != str(root):
        raise HTTPException(status_code=404, detail="command not found")
    if session.get("master_fd") is None:
        raise HTTPException(status_code=409, detail="command is not running in PTY mode")
    resize_command_session(request, command_id, body.rows, body.cols)
    return await _command_snapshot(request, workspace_path=str(root), command_id=command_id)


@router.post("/workspaces/{workspace_id}/coding/commands/{command_id}/signal")
async def signal_workspace_command(
    request: Request,
    workspace_id: str,
    command_id: str,
    body: CommandSignalRequest,
):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    session = get_command_session(request, command_id)
    if session is None or session.get("workspace") != str(root):
        raise HTTPException(status_code=404, detail="command not found")
    error = signal_command_session(request, command_id, body.signal)
    if error:
        raise HTTPException(status_code=409, detail=error)
    return await _command_snapshot(
        request,
        workspace_path=str(root),
        command_id=command_id,
        wait_seconds=2 if body.signal in {"terminate", "kill"} else 0,
    )


@router.post("/workspaces/{workspace_id}/coding/lsp/discover")
async def discover_workspace_lsp(
    request: Request,
    workspace_id: str,
    body: WorkerTargetRequest,
):
    user_id = await _user(request, "coding:read")
    workspace = await _workspace(user_id, workspace_id)
    await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    return {"workspace_id": workspace_id, **lsp_manager.discover()}


@router.post("/workspaces/{workspace_id}/coding/lsp/start")
async def start_workspace_lsp(
    request: Request,
    workspace_id: str,
    body: LspStartRequest,
):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    lsp_root, relative_root = _relative_path(body.root, root)
    context = _command_context(
        request=request,
        user_id=user_id,
        workspace_id=workspace_id,
        workspace_path=str(root),
        worker_id=body.worker_id,
    )
    try:
        identity = await identity_for_context(context)
        if identity.is_pam:
            process_env = env_for(identity, lsp_root, {"PAGER": "cat", "GIT_PAGER": "cat"})
            process_preexec = preexec_for(identity)
        else:
            process_env = {**os.environ, "PAGER": "cat", "GIT_PAGER": "cat"}
            process_preexec = None
        result = await lsp_manager.start(
            server_id=body.server_id,
            root=lsp_root,
            user_id=user_id,
            env=process_env,
            preexec_fn=process_preexec,
        )
    except (IdentityUnavailable, LspError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result.update({"workspace_id": workspace_id, "root": relative_root})
    return result


@router.post("/workspaces/{workspace_id}/coding/lsp/request")
async def request_workspace_lsp(
    request: Request,
    workspace_id: str,
    body: LspRequest,
):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    try:
        lsp_manager.validate_scope(lsp_id=body.lsp_id, user_id=user_id, workspace_root=root)
        response = await lsp_manager.request(
            lsp_id=body.lsp_id,
            user_id=user_id,
            method=body.method,
            params=body.params,
            timeout_seconds=body.timeout_seconds,
        )
    except LspError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "lsp_id": body.lsp_id, "response": response}


@router.post("/workspaces/{workspace_id}/coding/lsp/stop")
async def stop_workspace_lsp(
    request: Request,
    workspace_id: str,
    body: LspStopRequest,
):
    user_id = await _user(request, "command:execute")
    workspace = await _workspace(user_id, workspace_id)
    root = await _coding_root(user_id, workspace_id, workspace, body.worker_id)
    try:
        lsp_manager.validate_scope(lsp_id=body.lsp_id, user_id=user_id, workspace_root=root)
        result = await lsp_manager.stop(lsp_id=body.lsp_id, user_id=user_id)
    except LspError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, **result}


@router.post("/workspaces/{workspace_id}/browser")
async def control_managed_browser(request: Request, workspace_id: str, body: BrowserControlRequest):
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
                    await _settled_browser_snapshot(client)
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
        raise HTTPException(
            status_code=422, detail="offset and wait_seconds must be within their allowed range"
        )
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
