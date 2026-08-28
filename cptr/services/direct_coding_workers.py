"""Model-free direct-coding workers backed by isolated Git worktrees.

A worker is execution state only. It never invokes an LLM or autonomous agent;
ChatGPT remains the sole planner/reviewer and targets workers through the normal
direct-coding endpoints.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any

from sqlalchemy import select

from cptr.env import DIRECT_WORKER_MAX_PER_WORKSPACE, DIRECT_WORKTREE_ROOT
from cptr.models import DirectCodingWorker, Workspace
from cptr.utils.db import get_db
from cptr.utils.git import (
    change_manifest,
    create_worktree,
    current_revision,
    delete_branch_force,
    is_repo,
    remove_worktree,
    repository_root,
    status,
)
from cptr.utils.identity import ExecutionIdentity, identity_for_user_id
from cptr.utils.tools import command_sessions


ACTIVE_WORKER_STATUSES = {"READY", "WORKING", "RUNNING", "INTEGRATED"}


class DirectCodingWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _now_ms() -> int:
    return int(time.time() * 1000)


def _worker_id() -> str:
    return f"dcw_{uuid.uuid4().hex}"


def _safe_relative_path(value: str) -> str:
    candidate = (value or ".").strip() or "."
    path = Path(candidate)
    windows = PureWindowsPath(candidate)
    if path.is_absolute() or windows.is_absolute() or ".." in path.parts:
        raise DirectCodingWorkerError(
            "DIRECT_WORKER_INVALID_REPO_PATH",
            "repo_path must be a workspace-relative Git repository path",
            status_code=422,
        )
    return path.as_posix()


def _resolve_repo_path(workspace_root: str | Path, repo_path: str) -> Path:
    workspace = Path(workspace_root).expanduser().resolve()
    candidate = (workspace / _safe_relative_path(repo_path)).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise DirectCodingWorkerError(
            "DIRECT_WORKER_INVALID_REPO_PATH",
            "repo_path escapes the authorized workspace",
            status_code=422,
        ) from exc
    return candidate


def _default_worker_root(source_root: Path, worker_id: str) -> Path:
    configured = DIRECT_WORKTREE_ROOT.strip()
    if configured:
        return Path(configured).expanduser().resolve() / source_root.name / worker_id
    return source_root.parent / ".cptr-worktrees" / source_root.name / worker_id


def _manifest_paths(manifest: list[dict[str, str]]) -> set[str]:
    paths: set[str] = set()
    for item in manifest:
        path = item.get("path")
        old_path = item.get("old_path")
        if path:
            paths.add(path)
        if old_path:
            paths.add(old_path)
    return paths


async def create_worker_worktree(
    *,
    source_root: Path,
    worker_root: Path,
    branch: str,
    identity: ExecutionIdentity | None = None,
) -> str:
    """Create one clean branch-backed worktree without copying dirty source state."""
    source_root = source_root.resolve()
    if not await is_repo(str(source_root), identity):
        raise DirectCodingWorkerError(
            "DIRECT_WORKER_NOT_GIT_REPO",
            "direct coding workers require a Git repository",
            status_code=422,
        )
    source_status = await status(str(source_root), identity)
    if source_status.get("files"):
        raise DirectCodingWorkerError(
            "DIRECT_WORKER_DIRTY_BASE",
            "create direct coding workers before modifying the source workspace; the source repository must be clean",
        )
    await asyncio.to_thread(worker_root.parent.mkdir, parents=True, exist_ok=True)
    created = await create_worktree(str(source_root), branch, str(worker_root), identity)
    return str(Path(created["path"]).resolve())


async def worker_changed_paths(
    worker_root: Path, identity: ExecutionIdentity | None = None
) -> set[str]:
    return _manifest_paths(await change_manifest(str(worker_root), identity))


def _copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        destination.symlink_to(os.readlink(source))
        return
    shutil.copy2(source, destination)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


async def apply_worker_changes(
    base_root: Path,
    worker_root: Path,
    *,
    identity: ExecutionIdentity | None = None,
) -> dict[str, Any]:
    """Mechanically copy a worker's non-overlapping working-tree changes into the base.

    The operation deliberately leaves the base uncommitted. Any path already
    changed in the base is reported as a conflict and is never overwritten.
    """
    base_root = base_root.resolve()
    worker_root = worker_root.resolve()
    worker_manifest, base_manifest = await asyncio.gather(
        change_manifest(str(worker_root), identity),
        change_manifest(str(base_root), identity),
    )
    worker_paths = _manifest_paths(worker_manifest)
    base_paths = _manifest_paths(base_manifest)
    conflicts = sorted(worker_paths & base_paths)
    if conflicts:
        return {"applied_paths": [], "conflicts": conflicts}

    applied: list[str] = []
    for item in worker_manifest:
        status_name = item.get("status", "modified")
        path = item.get("path", "")
        old_path = item.get("old_path", "")
        if not path:
            continue
        if status_name == "renamed" and old_path:
            await asyncio.to_thread(_remove_path, base_root / old_path)
        if status_name == "deleted":
            await asyncio.to_thread(_remove_path, base_root / path)
        else:
            source = worker_root / path
            if not source.exists() and not source.is_symlink():
                raise DirectCodingWorkerError(
                    "DIRECT_WORKER_CHANGE_MISSING",
                    f"worker change disappeared before integration: {path}",
                )
            await asyncio.to_thread(_copy_path, source, base_root / path)
        if old_path:
            applied.append(old_path)
        applied.append(path)
    return {"applied_paths": sorted(set(applied)), "conflicts": []}


class DirectCodingWorkerService:
    async def _get(self, *, user_id: str, workspace_id: str, worker_id: str) -> DirectCodingWorker:
        async with await get_db() as db:
            result = await db.execute(
                select(DirectCodingWorker).where(
                    DirectCodingWorker.id == worker_id,
                    DirectCodingWorker.user_id == user_id,
                    DirectCodingWorker.workspace_id == workspace_id,
                )
            )
            worker = result.scalar_one_or_none()
        if worker is None:
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_NOT_FOUND", "direct coding worker not found", status_code=404
            )
        return worker

    async def create(
        self,
        *,
        user_id: str,
        workspace: Workspace,
        name: str,
        responsibility: str = "",
        repo_path: str = ".",
    ) -> dict[str, Any]:
        name = name.strip()
        responsibility = responsibility.strip()
        if not name or len(name) > 80:
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_INVALID_NAME",
                "worker name must contain 1-80 characters",
                status_code=422,
            )
        normalized_repo_path = _safe_relative_path(repo_path)
        requested_root = _resolve_repo_path(workspace.path, normalized_repo_path)
        identity = await identity_for_user_id(user_id)
        if not await is_repo(str(requested_root), identity):
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_NOT_GIT_REPO",
                "repo_path must identify a Git repository",
                status_code=422,
            )
        repo_root = Path(await repository_root(str(requested_root), identity)).resolve()
        workspace_root = Path(workspace.path).expanduser().resolve()
        try:
            relative_repo = repo_root.relative_to(workspace_root).as_posix() or "."
        except ValueError as exc:
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_REPO_OUTSIDE_WORKSPACE",
                "the Git repository must be contained by the authorized workspace",
                status_code=422,
            ) from exc

        async with await get_db() as db:
            result = await db.execute(
                select(DirectCodingWorker).where(
                    DirectCodingWorker.user_id == user_id,
                    DirectCodingWorker.workspace_id == workspace.id,
                    DirectCodingWorker.status.in_(ACTIVE_WORKER_STATUSES),
                )
            )
            if len(result.scalars().all()) >= DIRECT_WORKER_MAX_PER_WORKSPACE:
                raise DirectCodingWorkerError(
                    "DIRECT_WORKER_LIMIT_REACHED",
                    "direct coding worker limit reached for this workspace",
                    status_code=429,
                )

        worker_id = _worker_id()
        branch = f"cptr/direct/{worker_id}"
        worker_root = _default_worker_root(repo_root, worker_id)
        created_root = await create_worker_worktree(
            source_root=repo_root,
            worker_root=worker_root,
            branch=branch,
            identity=identity,
        )
        now = _now_ms()
        worker = DirectCodingWorker(
            id=worker_id,
            user_id=user_id,
            workspace_id=workspace.id,
            name=name,
            responsibility=responsibility,
            repo_path=relative_repo,
            status="READY",
            branch=branch,
            worktree_path=created_root,
            base_revision=await current_revision(str(repo_root), identity),
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
        try:
            async with await get_db() as db:
                db.add(worker)
                await db.commit()
        except Exception:
            await remove_worktree(str(repo_root), created_root, force=True, identity=identity)
            try:
                await delete_branch_force(str(repo_root), branch, identity)
            except Exception:
                pass
            raise
        return await self.summary(worker)

    async def list(self, *, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        async with await get_db() as db:
            result = await db.execute(
                select(DirectCodingWorker)
                .where(
                    DirectCodingWorker.user_id == user_id,
                    DirectCodingWorker.workspace_id == workspace_id,
                    DirectCodingWorker.status != "CLOSED",
                )
                .order_by(DirectCodingWorker.created_at)
            )
            workers = list(result.scalars().all())
        return [await self.summary(worker) for worker in workers]

    async def get(self, *, user_id: str, workspace_id: str, worker_id: str) -> dict[str, Any]:
        return await self.summary(
            await self._get(user_id=user_id, workspace_id=workspace_id, worker_id=worker_id)
        )

    async def resolve_root(self, *, user_id: str, workspace_id: str, worker_id: str) -> Path:
        worker = await self._get(user_id=user_id, workspace_id=workspace_id, worker_id=worker_id)
        if worker.status == "CLOSED" or worker.closed_at is not None:
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_CLOSED", "direct coding worker is closed", status_code=410
            )
        root = Path(worker.worktree_path).resolve()
        if not root.is_dir():
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_WORKTREE_MISSING",
                "direct coding worker worktree is unavailable",
                status_code=410,
            )
        return root

    async def summary(self, worker: DirectCodingWorker) -> dict[str, Any]:
        root = Path(worker.worktree_path)
        changed_paths: list[str] = []
        if root.is_dir() and worker.status != "CLOSED":
            try:
                changed_paths = sorted(await worker_changed_paths(root))
            except Exception:
                changed_paths = []
        sessions = (
            [
                session
                for session in command_sessions.values()
                if Path(str(session.get("workspace") or "")).resolve() == root.resolve()
            ]
            if root.exists()
            else []
        )
        sessions.sort(key=lambda item: float(item.get("created_at") or 0))
        active = [str(item.get("id")) for item in sessions if not item.get("done")]
        recent = [str(item.get("id")) for item in sessions[-5:] if item.get("id")]
        effective_status = "RUNNING" if active else worker.status
        return {
            "worker_id": worker.id,
            "workspace_id": worker.workspace_id,
            "name": worker.name,
            "responsibility": worker.responsibility,
            "repo_path": worker.repo_path,
            "status": effective_status,
            "branch": worker.branch,
            "base_revision": worker.base_revision,
            "changed_file_count": len(changed_paths),
            "changed_paths": changed_paths[:100],
            "active_command_ids": active,
            "recent_command_ids": recent,
            "created_at": worker.created_at,
            "updated_at": worker.updated_at,
            "integrated_at": worker.integrated_at,
            "closed_at": worker.closed_at,
        }

    async def mark_activity(
        self, *, user_id: str, workspace_id: str, worker_id: str, status_value: str | None = None
    ) -> None:
        async with await get_db() as db:
            result = await db.execute(
                select(DirectCodingWorker).where(
                    DirectCodingWorker.id == worker_id,
                    DirectCodingWorker.user_id == user_id,
                    DirectCodingWorker.workspace_id == workspace_id,
                )
            )
            worker = result.scalar_one_or_none()
            if worker is None:
                return
            now = _now_ms()
            worker.updated_at = now
            worker.last_activity_at = now
            if status_value and worker.status != "INTEGRATED":
                worker.status = status_value
            await db.commit()

    async def integrate(
        self, *, user_id: str, workspace: Workspace, worker_ids: list[str]
    ) -> dict[str, Any]:
        if not worker_ids:
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_IDS_REQUIRED", "at least one worker_id is required", status_code=422
            )
        identity = await identity_for_user_id(user_id)
        integrated: list[str] = []
        conflicts: dict[str, list[str]] = {}
        applied_paths: dict[str, list[str]] = {}
        for worker_id in worker_ids:
            worker = await self._get(
                user_id=user_id, workspace_id=workspace.id, worker_id=worker_id
            )
            summary = await self.summary(worker)
            if summary["active_command_ids"]:
                raise DirectCodingWorkerError(
                    "DIRECT_WORKER_COMMAND_ACTIVE",
                    f"worker {worker_id} still has running commands",
                )
            source_root = _resolve_repo_path(workspace.path, worker.repo_path)
            repo_root = Path(await repository_root(str(source_root), identity)).resolve()
            if await current_revision(str(repo_root), identity) != worker.base_revision:
                conflicts[worker_id] = ["<base-revision-changed>"]
                continue
            result = await apply_worker_changes(
                repo_root, Path(worker.worktree_path), identity=identity
            )
            if result["conflicts"]:
                conflicts[worker_id] = list(result["conflicts"])
                continue
            integrated.append(worker_id)
            applied_paths[worker_id] = list(result["applied_paths"])
            async with await get_db() as db:
                persistent = await db.get(DirectCodingWorker, worker_id)
                if persistent and persistent.user_id == user_id:
                    now = _now_ms()
                    persistent.status = "INTEGRATED"
                    persistent.integrated_at = now
                    persistent.updated_at = now
                    await db.commit()
        return {
            "workspace_id": workspace.id,
            "integrated": integrated,
            "conflicts": conflicts,
            "applied_paths": applied_paths,
        }

    async def close(
        self,
        *,
        user_id: str,
        workspace: Workspace,
        worker_id: str,
        discard_changes: bool = False,
    ) -> dict[str, Any]:
        worker = await self._get(user_id=user_id, workspace_id=workspace.id, worker_id=worker_id)
        summary = await self.summary(worker)
        if summary["active_command_ids"]:
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_COMMAND_ACTIVE", "stop worker commands before closing the worker"
            )
        dirty = bool(summary["changed_file_count"])
        if dirty and worker.integrated_at is None and not discard_changes:
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_UNINTEGRATED_CHANGES",
                "worker has unintegrated changes; integrate them first or explicitly discard changes",
            )
        identity = await identity_for_user_id(user_id)
        source_root = _resolve_repo_path(workspace.path, worker.repo_path)
        repo_root = Path(await repository_root(str(source_root), identity)).resolve()
        root = Path(worker.worktree_path)
        if root.exists():
            await remove_worktree(str(repo_root), str(root), force=dirty, identity=identity)
        try:
            await delete_branch_force(str(repo_root), worker.branch, identity)
        except Exception:
            # Worktree cleanup is authoritative. A branch may already have been
            # removed manually; do not turn that into a worker-close failure.
            pass
        async with await get_db() as db:
            persistent = await db.get(DirectCodingWorker, worker_id)
            if persistent and persistent.user_id == user_id:
                now = _now_ms()
                persistent.status = "CLOSED"
                persistent.closed_at = now
                persistent.updated_at = now
                await db.commit()
        return {
            "worker_id": worker_id,
            "workspace_id": workspace.id,
            "status": "CLOSED",
            "discarded": bool(dirty and worker.integrated_at is None and discard_changes),
        }


service = DirectCodingWorkerService()


async def resolve_direct_worker_root(*, user_id: str, workspace_id: str, worker_id: str) -> Path:
    return await service.resolve_root(
        user_id=user_id, workspace_id=workspace_id, worker_id=worker_id
    )
