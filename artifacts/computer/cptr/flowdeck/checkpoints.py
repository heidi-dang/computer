"""Safe, workspace-scoped checkpoint capture and restore for Phase 11."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select

from cptr.flowdeck.git import GitInspectionError, _git
from cptr.models.flowdeck import FlowDeckCheckpoint


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
        value = await _git(root, "rev-parse", "--verify", "HEAD", output_limit=256)
    except GitInspectionError as exc:
        raise CheckpointError("checkpoint revision could not be verified") from exc
    revision = value.strip()
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise CheckpointError("checkpoint revision could not be verified")
    return revision


async def _clean(root: Path) -> bool:
    try:
        value = await _git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            output_limit=128 * 1024,
        )
    except GitInspectionError as exc:
        raise CheckpointError("workspace state could not be verified") from exc
    return not value.strip()


class CheckpointService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

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
            await _git(root, "checkout", "--detach", revision, output_limit=1024)
            observed = await _revision(root)
        except GitInspectionError as exc:
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