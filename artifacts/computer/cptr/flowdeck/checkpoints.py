"""Safe, workspace-scoped checkpoint capture and restore for Phase 11."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select

from cptr.models.flowdeck import FlowDeckCheckpoint
from cptr.utils.git import GitError, _run, status as git_status


class CheckpointError(RuntimeError):
    def __init__(self, message: str, *, code: str = "checkpoint_error"):
        super().__init__(message)
        self.code = code


def _root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise CheckpointError("workspace is not a Git repository", code="invalid_workspace")
    return root


async def _revision(root: Path) -> str:
    try:
        _, value, _ = await _run("rev-parse", "--verify", "HEAD", cwd=str(root))
    except GitError as exc:
        raise CheckpointError("checkpoint revision could not be verified") from exc
    revision = value.strip()
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise CheckpointError("checkpoint revision could not be verified")
    return revision


async def _clean(root: Path) -> bool:
    try:
        value = await git_status(str(root))
    except GitError as exc:
        raise CheckpointError("workspace state could not be verified") from exc
    return not value["files"]


class CheckpointService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def list(self, *, workspace: str, owner: str) -> list[dict[str, Any]]:
        root = _root(workspace)
        async with self.session_factory() as db:
            rows = await db.scalars(
                select(FlowDeckCheckpoint)
                .where(
                    FlowDeckCheckpoint.workspace == str(root),
                    FlowDeckCheckpoint.owner == owner,
                )
                .order_by(FlowDeckCheckpoint.created_at.desc())
            )
            return [
                {
                    "checkpoint_id": row.id,
                    "revision": row.revision,
                    "status": row.status,
                    "created_at": row.created_at,
                    "restored_at": row.restored_at,
                }
                for row in rows
            ]

    async def capture(
        self, *, workspace: str, owner: str, run_id: str | None = None
    ) -> dict[str, Any]:
        root = _root(workspace)
        if not await _clean(root):
            raise CheckpointError(
                "checkpoint capture requires a clean worktree", code="dirty_workspace"
            )
        revision = await _revision(root)
        now = int(time.time() * 1000)
        checkpoint = FlowDeckCheckpoint(
            workspace=str(root),
            owner=owner,
            run_id=run_id,
            revision=revision,
            status="AVAILABLE",
            evidence={
                "authoritative": True,
                "source": "verifier",
                "observation": "verifier_check",
                "observed_outcome": "succeeded",
                "revision_sha256": hashlib.sha256(revision.encode()).hexdigest(),
                "clean_worktree": True,
            },
            created_at=now,
        )
        async with self.session_factory() as db:
            db.add(checkpoint)
            await db.commit()
        return {
            "checkpoint_id": checkpoint.id,
            "workspace": str(root),
            "revision": revision,
            "status": checkpoint.status,
        }

    async def restore(
        self, *, checkpoint_id: str, workspace: str, owner: str
    ) -> dict[str, Any]:
        root = _root(workspace)
        async with self.session_factory() as db:
            checkpoint = await db.scalar(
                select(FlowDeckCheckpoint).where(
                    FlowDeckCheckpoint.id == checkpoint_id,
                    FlowDeckCheckpoint.workspace == str(root),
                    FlowDeckCheckpoint.owner == owner,
                    FlowDeckCheckpoint.status == "AVAILABLE",
                )
            )
            if not checkpoint:
                raise CheckpointError(
                    "checkpoint is unavailable for this workspace", code="checkpoint_denied"
                )
            revision = checkpoint.revision
        if not await _clean(root):
            raise CheckpointError(
                "restore requires a clean worktree", code="dirty_workspace"
            )
        try:
            await _run("checkout", "--detach", revision, cwd=str(root))
            observed = await _revision(root)
        except GitError as exc:
            raise CheckpointError("checkpoint restore could not be verified") from exc
        if observed != revision:
            raise CheckpointError(
                "checkpoint restore outcome is unknown", code="restore_unknown"
            )
        async with self.session_factory() as db:
            checkpoint = await db.scalar(
                select(FlowDeckCheckpoint).where(
                    FlowDeckCheckpoint.id == checkpoint_id,
                    FlowDeckCheckpoint.workspace == str(root),
                    FlowDeckCheckpoint.owner == owner,
                    FlowDeckCheckpoint.status == "AVAILABLE",
                )
            )
            if not checkpoint:
                raise CheckpointError(
                    "checkpoint restore became stale", code="restore_stale"
                )
            checkpoint.status = "RESTORED"
            checkpoint.restored_at = int(time.time() * 1000)
            await db.commit()
        return {"checkpoint_id": checkpoint_id, "revision": revision, "status": "RESTORED"}