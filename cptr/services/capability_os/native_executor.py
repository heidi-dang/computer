"""Bounded host-native action executor for Capability OS.

This adapter intentionally exposes a small registry of existing CPTR primitives
rather than a generic shell.  Every invocation is tied to server-persisted lease
context, revalidates the workspace owner, checks the leased permission for the
exact resource, and enforces lease call/read/write budgets.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Awaitable, Callable

from cptr.models import Workspace
from cptr.services.capability_os.authority import CapabilityLease, permission_covers
from cptr.services.capability_os.contracts import CapabilityRequest
from cptr.services.capability_os.vm import ActionResult
from cptr.services.workspace_availability import is_workspace_available
from cptr.utils.db import get_db
from cptr.utils.git import diff_check as git_diff_check
from cptr.utils.git import status as git_status
from cptr.utils.identity import ExecutionIdentity, IdentityUnavailable, identity_for_user_id
from cptr.utils.runtime import FileError, Runtime

_MAX_TEXT_BYTES = 1_000_000
_MAX_READ_BYTES = 500_000
_MAX_LIST_ENTRIES = 1_000


class NativeActionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class NativeWorkspaceContext:
    user_id: str
    workspace_id: str
    root: Path
    identity: ExecutionIdentity
    root_device: int | None = None
    root_inode: int | None = None


class DatabaseNativeWorkspaceResolver:
    async def resolve(self, *, user_id: str, workspace_id: str) -> NativeWorkspaceContext:
        async with await get_db() as db:
            workspace = await db.get(Workspace, workspace_id)
        if workspace is None or workspace.user_id != user_id:
            raise NativeActionDenied("native action workspace is not owned by the lease user")
        if not is_workspace_available(workspace):
            raise NativeActionDenied("native action workspace is unavailable")
        try:
            identity = await identity_for_user_id(user_id)
        except IdentityUnavailable as exc:
            raise NativeActionDenied(str(exc)) from exc
        root = Path(workspace.path).expanduser().resolve()
        if not root.is_dir():
            raise NativeActionDenied("native action workspace root is unavailable")
        try:
            root_stat = root.stat()
        except OSError as exc:
            raise NativeActionDenied("native action workspace root is unavailable") from exc
        return NativeWorkspaceContext(
            user_id=user_id,
            workspace_id=workspace_id,
            root=root,
            identity=identity,
            root_device=int(root_stat.st_dev),
            root_inode=int(root_stat.st_ino),
        )


@dataclass
class _LeaseUsage:
    calls: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    expires_at_ms: int = 0


class NativeActionExecutor:
    """Execute only explicitly registered host-native CPTR actions."""

    def __init__(
        self,
        *,
        resolver: Any | None = None,
        runtime: Any = Runtime,
        status_fn: Callable[..., Awaitable[dict[str, Any]]] = git_status,
        diff_check_fn: Callable[..., Awaitable[dict[str, Any]]] = git_diff_check,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._resolver = resolver or DatabaseNativeWorkspaceResolver()
        self._runtime = runtime
        self._status_fn = status_fn
        self._diff_check_fn = diff_check_fn
        self._clock_ms = clock_ms
        self._usage: dict[str, _LeaseUsage] = {}
        self._usage_lock = asyncio.Lock()
        self._handlers = {
            "cptr.fs.list": self._list,
            "cptr.fs.read_text": self._read_text,
            "cptr.fs.write_text": self._write_text,
            "cptr.git.status": self._git_status,
            "cptr.git.diff_check": self._git_diff_check,
        }

    @property
    def action_refs(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    @staticmethod
    def _limit(lease: CapabilityLease, name: str) -> int | None:
        value = lease.resource_limits.get(name)
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise NativeActionDenied(f"invalid lease resource limit: {name}") from exc
        if parsed < 0:
            raise NativeActionDenied(f"invalid lease resource limit: {name}")
        return parsed

    async def _usage_for(self, lease: CapabilityLease) -> _LeaseUsage:
        async with self._usage_lock:
            now = int(self._clock_ms())
            for lease_id, usage in list(self._usage.items()):
                if usage.expires_at_ms <= now:
                    self._usage.pop(lease_id, None)
            usage = self._usage.get(lease.lease_id)
            if usage is None:
                usage = _LeaseUsage(expires_at_ms=lease.expires_at_ms)
                self._usage[lease.lease_id] = usage
            return usage

    async def _reserve_call(self, lease: CapabilityLease) -> None:
        async with self._usage_lock:
            now = int(self._clock_ms())
            for lease_id, usage in list(self._usage.items()):
                if usage.expires_at_ms <= now:
                    self._usage.pop(lease_id, None)
            usage = self._usage.setdefault(
                lease.lease_id, _LeaseUsage(expires_at_ms=lease.expires_at_ms)
            )
            maximum = self._limit(lease, "maxCalls")
            if maximum is not None and usage.calls + 1 > maximum:
                raise NativeActionDenied("native action call budget exhausted")
            usage.calls += 1

    async def _reserve_bytes(
        self, lease: CapabilityLease, *, read: int = 0, written: int = 0
    ) -> None:
        if read < 0 or written < 0:
            raise NativeActionDenied("native action byte accounting cannot be negative")
        async with self._usage_lock:
            usage = self._usage.setdefault(
                lease.lease_id, _LeaseUsage(expires_at_ms=lease.expires_at_ms)
            )
            max_read = self._limit(lease, "maxBytesRead")
            max_written = self._limit(lease, "maxBytesWritten")
            if max_read is not None and usage.bytes_read + read > max_read:
                raise NativeActionDenied("native action read budget exhausted")
            if max_written is not None and usage.bytes_written + written > max_written:
                raise NativeActionDenied("native action write budget exhausted")
            usage.bytes_read += read
            usage.bytes_written += written

    async def _reserve_read_window(self, lease: CapabilityLease, requested: int) -> tuple[int, int]:
        maximum = self._limit(lease, "maxBytesRead")
        if maximum is None:
            return requested, 0
        async with self._usage_lock:
            usage = self._usage.setdefault(
                lease.lease_id, _LeaseUsage(expires_at_ms=lease.expires_at_ms)
            )
            remaining = maximum - usage.bytes_read
            if remaining <= 0:
                raise NativeActionDenied("native action read budget exhausted")
            reserved = min(requested, remaining)
            usage.bytes_read += reserved
            return reserved, reserved

    async def _refund_bytes(
        self, lease: CapabilityLease, *, read: int = 0, written: int = 0
    ) -> None:
        async with self._usage_lock:
            usage = self._usage.get(lease.lease_id)
            if usage is None:
                return
            usage.bytes_read = max(0, usage.bytes_read - max(0, read))
            usage.bytes_written = max(0, usage.bytes_written - max(0, written))

    @staticmethod
    def _raise_secure_path_error(exc: FileError, *, operation: str) -> None:
        if exc.status_code in {403, 409}:
            raise NativeActionDenied(
                f"native {operation} rejected unstable workspace path"
            ) from exc
        raise exc

    def _secure_runtime_method(self, name: str):
        method = getattr(self._runtime, name, None)
        if not callable(method):
            raise NativeActionDenied("secure native filesystem runtime is unavailable")
        return method

    @staticmethod
    def _lease_context(lease: CapabilityLease) -> tuple[str, str]:
        context = dict(lease.execution_context or {})
        user_id = str(context.get("userId") or "").strip()
        workspace_id = str(context.get("workspaceId") or "").strip()
        if not user_id or not workspace_id:
            raise NativeActionDenied(
                "native action lease lacks server-bound user/workspace context"
            )
        return user_id, workspace_id

    @staticmethod
    def _relative(root: Path, value: Any) -> tuple[Path, str]:
        text = str(value or ".").strip() or "."
        supplied = Path(text)
        if supplied.is_absolute() or PureWindowsPath(text).is_absolute():
            raise NativeActionDenied("native action path must be workspace-relative")
        resolved = (root / supplied).resolve()
        try:
            relative_path = resolved.relative_to(root)
        except ValueError as exc:
            raise NativeActionDenied("native action path escapes the workspace") from exc
        if any(part.startswith(".env") for part in relative_path.parts):
            raise NativeActionDenied("environment files are not available to native actions")
        relative = relative_path.as_posix() or "."
        return resolved, relative

    @staticmethod
    def _resource(workspace_id: str, relative: str, *, subtree: bool = False) -> str:
        suffix = relative
        if subtree:
            suffix = "**" if relative == "." else f"{relative}/**"
        return f"workspace:{workspace_id}:{suffix}"

    @staticmethod
    def _require_permission(
        lease: CapabilityLease, *, action: str, resource: str
    ) -> CapabilityRequest:
        required = CapabilityRequest(action=action, resource=resource)
        if not any(permission_covers(allowed, required) for allowed in lease.permissions):
            raise NativeActionDenied(
                f"native action lease lacks {action} permission for {resource}"
            )
        return required

    async def invoke(
        self,
        *,
        action_ref: str,
        version: str,
        inputs: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult:
        if lease.status != "active" or lease.expires_at_ms <= int(self._clock_ms()):
            raise NativeActionDenied("native action requires an active lease")
        if lease.runtime_profile != "cptr-vm":
            raise NativeActionDenied("native action lease has the wrong runtime profile")
        if version != "v1":
            raise NativeActionDenied("native action version is not registered")
        handler = self._handlers.get(action_ref)
        if handler is None:
            raise NativeActionDenied("native action is not registered")
        if not isinstance(inputs, dict):
            raise NativeActionDenied("native action inputs must be an object")
        user_id, workspace_id = self._lease_context(lease)
        workspace = await self._resolver.resolve(user_id=user_id, workspace_id=workspace_id)
        if workspace.user_id != user_id or workspace.workspace_id != workspace_id:
            raise NativeActionDenied(
                "native workspace resolver returned mismatched authority context"
            )
        await self._reserve_call(lease)
        return await handler(workspace, dict(inputs), lease, int(timeout_ms))

    async def _list(
        self,
        workspace: NativeWorkspaceContext,
        inputs: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult:
        _, relative = self._relative(workspace.root, inputs.get("path", "."))
        self._require_permission(
            lease,
            action="filesystem.read",
            resource=self._resource(workspace.workspace_id, relative, subtree=True),
        )
        recursive = bool(inputs.get("recursive", False))
        try:
            offset = int(inputs.get("offset", 0))
            limit = int(inputs.get("limit", 500))
        except (TypeError, ValueError) as exc:
            raise NativeActionDenied("native list offset/limit must be integers") from exc
        if offset < 0 or limit <= 0:
            raise NativeActionDenied("native list offset/limit are out of range")
        limit = min(limit, _MAX_LIST_ENTRIES)
        secure_list = self._secure_runtime_method("list_tree_entries_beneath_as")
        try:
            result = await secure_list(
                workspace.identity,
                str(workspace.root),
                relative,
                recursive,
                offset,
                limit,
                workspace.root_device,
                workspace.root_inode,
            )
        except FileError as exc:
            self._raise_secure_path_error(exc, operation="list")
        entries = list(result.get("entries") or [])
        return ActionResult(
            output={
                "path": relative,
                "entries": entries,
                "total": int(result.get("total") or len(entries)),
                "truncated": bool(result.get("truncated")),
                "nextOffset": result.get("next_offset"),
            },
            verification_passed=True,
            metadata={"actionClass": "native", "timeoutMs": timeout_ms},
        )

    async def _read_text(
        self,
        workspace: NativeWorkspaceContext,
        inputs: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult:
        if "path" not in inputs:
            raise NativeActionDenied("native read requires path")
        _, relative = self._relative(workspace.root, inputs["path"])
        self._require_permission(
            lease,
            action="filesystem.read",
            resource=self._resource(workspace.workspace_id, relative),
        )
        requested = inputs.get("maxBytes", _MAX_READ_BYTES)
        try:
            requested_limit = int(requested)
        except (TypeError, ValueError) as exc:
            raise NativeActionDenied("native read maxBytes must be an integer") from exc
        if requested_limit <= 0:
            raise NativeActionDenied("native read maxBytes must be positive")
        read_limit = min(requested_limit, _MAX_READ_BYTES)
        secure_read = self._secure_runtime_method("read_text_file_beneath_as")
        bounded_limit, reserved = await self._reserve_read_window(lease, read_limit)
        try:
            data = await secure_read(
                workspace.identity,
                str(workspace.root),
                relative,
                bounded_limit,
                workspace.root_device,
                workspace.root_inode,
            )
        except FileError as exc:
            if reserved:
                await self._refund_bytes(lease, read=reserved)
            if exc.status_code == 413:
                raise NativeActionDenied(
                    "native read exceeds requested byte or lease budget"
                ) from exc
            self._raise_secure_path_error(exc, operation="read")
        except Exception:
            if reserved:
                await self._refund_bytes(lease, read=reserved)
            raise
        size = int(data.get("size") or 0)
        if reserved:
            if size > reserved:
                await self._refund_bytes(lease, read=reserved)
                raise NativeActionDenied("native action read budget exhausted")
            await self._refund_bytes(lease, read=reserved - size)
        else:
            await self._reserve_bytes(lease, read=size)
        if data.get("binary"):
            raise NativeActionDenied("binary files are not available to native read")
        content = str(data.get("content") or "")
        return ActionResult(
            output={
                "path": relative,
                "content": content,
                "size": size,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
            verification_passed=True,
            metadata={"actionClass": "native", "bytesRead": size, "timeoutMs": timeout_ms},
        )

    async def _write_text(
        self,
        workspace: NativeWorkspaceContext,
        inputs: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult:
        if "path" not in inputs or "content" not in inputs:
            raise NativeActionDenied("native write requires path and content")
        content = inputs["content"]
        if not isinstance(content, str):
            raise NativeActionDenied("native write content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_TEXT_BYTES:
            raise NativeActionDenied("native write exceeds the text size limit")
        _, relative = self._relative(workspace.root, inputs["path"])
        self._require_permission(
            lease,
            action="filesystem.write",
            resource=self._resource(workspace.workspace_id, relative),
        )
        overwrite = bool(inputs.get("overwrite", False))
        expected = inputs.get("expectedSha256")
        if expected is not None:
            expected = str(expected).strip().lower()
            if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
                raise NativeActionDenied("expectedSha256 must be a SHA-256 hex digest")
        secure_write = self._secure_runtime_method("write_text_file_beneath_as")
        await self._reserve_bytes(lease, written=len(encoded))
        try:
            result = await secure_write(
                workspace.identity,
                str(workspace.root),
                relative,
                content,
                overwrite,
                expected,
                _MAX_TEXT_BYTES,
                workspace.root_device,
                workspace.root_inode,
            )
        except FileError as exc:
            await self._refund_bytes(lease, written=len(encoded))
            self._raise_secure_path_error(exc, operation="write")
        except Exception:
            await self._refund_bytes(lease, written=len(encoded))
            raise
        return ActionResult(
            output={
                "path": relative,
                "bytesWritten": len(encoded),
                "sha256": str(result.get("sha256") or hashlib.sha256(encoded).hexdigest()),
                "created": bool(result.get("created")),
            },
            verification_passed=True,
            metadata={
                "actionClass": "native",
                "bytesWritten": len(encoded),
                "timeoutMs": timeout_ms,
            },
        )

    async def _git_status(
        self,
        workspace: NativeWorkspaceContext,
        inputs: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult:
        repo, relative = self._relative(workspace.root, inputs.get("repoPath", "."))
        if not repo.is_dir():
            raise NativeActionDenied("native Git action requires a repository directory")
        self._require_permission(
            lease,
            action="git.read",
            resource=self._resource(workspace.workspace_id, relative, subtree=True),
        )
        output = await self._status_fn(str(repo), workspace.identity)
        return ActionResult(
            output=output,
            verification_passed=True,
            metadata={"actionClass": "native", "timeoutMs": timeout_ms},
        )

    async def _git_diff_check(
        self,
        workspace: NativeWorkspaceContext,
        inputs: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult:
        repo, relative = self._relative(workspace.root, inputs.get("repoPath", "."))
        if not repo.is_dir():
            raise NativeActionDenied("native Git action requires a repository directory")
        self._require_permission(
            lease,
            action="git.read",
            resource=self._resource(workspace.workspace_id, relative, subtree=True),
        )
        output = await self._diff_check_fn(str(repo), workspace.identity)
        passed = bool(output.get("passed"))
        return ActionResult(
            output=output,
            verification_passed=passed,
            metadata={"actionClass": "native", "timeoutMs": timeout_ms},
        )
