"""Aggregation service for WorkspaceTasks across Git repositories, DirectCodingWorkers, and verification evidence."""

from __future__ import annotations

import time
from pathlib import Path, PureWindowsPath
from typing import Any

from sqlalchemy import select

from cptr.models import (
    DirectCodingWorker,
    Workspace,
    WorkspaceTask,
    WorkspaceTaskEvidence,
    WorkspaceTaskRepositoryPin,
    WorkspaceTaskWorkerLink,
)
from cptr.services.direct_coding_workers import service as direct_worker_service
from cptr.utils.db import get_db
from cptr.utils.git import (
    current_branch,
    current_revision,
    is_repo,
    repository_root,
    revision_divergence,
)
from cptr.utils.identity import identity_for_user_id

TASK_STATUSES = frozenset({"OPEN", "IN_PROGRESS", "VERIFIED", "INTEGRATED", "CLOSED", "FAILED"})
TERMINAL_TASK_STATUSES = frozenset({"VERIFIED", "INTEGRATED", "CLOSED", "FAILED"})
WORKER_LINK_STATUSES = frozenset({"ACTIVE", "DETACHED", "INTEGRATED"})
EVIDENCE_KINDS = frozenset({"test", "lint", "typecheck", "diff", "review", "command", "manual"})
EVIDENCE_STATUSES = frozenset({"PASSED", "FAILED", "PENDING", "OBSERVED"})


class WorkspaceTaskError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_relative_path(value: str) -> str:
    candidate = (value or ".").strip() or "."
    path = Path(candidate)
    windows = PureWindowsPath(candidate)
    if path.is_absolute() or windows.is_absolute() or ".." in path.parts:
        raise WorkspaceTaskError(
            "WORKSPACE_TASK_INVALID_REPO_PATH",
            "repo_path must be a workspace-relative Git repository path",
            status_code=422,
        )
    return path.as_posix()


def _resolve_repo_path(workspace_root: str | Path, repo_path: str) -> Path:
    workspace = Path(workspace_root).expanduser().resolve()
    candidate = (workspace / _safe_relative_path(repo_path)).resolve()
    return candidate


def _get_workspace_path(workspace: Workspace | Path | str | dict[str, Any]) -> str:
    if hasattr(workspace, "path"):
        return str(workspace.path)
    if isinstance(workspace, dict) and "path" in workspace:
        return str(workspace["path"])
    return str(workspace)


def _task_to_dict(task: WorkspaceTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "user_id": task.user_id,
        "workspace_id": task.workspace_id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "metadata": task.task_metadata or {},
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "closed_at": task.closed_at,
    }


def _pin_to_dict(pin: WorkspaceTaskRepositoryPin) -> dict[str, Any]:
    return {
        "id": pin.id,
        "task_id": pin.task_id,
        "repo_path": pin.repo_path,
        "pinned_revision": pin.pinned_revision,
        "head_revision": pin.head_revision,
        "branch": pin.branch,
        "pinned_at": pin.pinned_at,
        "updated_at": pin.updated_at,
    }


def _link_to_dict(link: WorkspaceTaskWorkerLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "task_id": link.task_id,
        "worker_id": link.worker_id,
        "role": link.role,
        "status": link.status,
        "linked_at": link.linked_at,
    }


def _evidence_to_dict(evidence: WorkspaceTaskEvidence) -> dict[str, Any]:
    return {
        "id": evidence.id,
        "task_id": evidence.task_id,
        "worker_id": evidence.worker_id,
        "repo_path": evidence.repo_path,
        "kind": evidence.kind,
        "status": evidence.status,
        "summary": evidence.summary,
        "command": evidence.command,
        "details": evidence.details or {},
        "fingerprint": evidence.fingerprint,
        "created_at": evidence.created_at,
    }


class WorkspaceTaskService:
    """Aggregates Git repositories, DirectCodingWorkers, and verification evidence around a WorkspaceTask."""

    async def _get_task(self, *, user_id: str, workspace_id: str, task_id: str) -> WorkspaceTask:
        async with await get_db() as db:
            result = await db.execute(
                select(WorkspaceTask).where(
                    WorkspaceTask.id == task_id,
                    WorkspaceTask.user_id == user_id,
                    WorkspaceTask.workspace_id == workspace_id,
                )
            )
            task = result.scalar_one_or_none()
        if task is None:
            raise WorkspaceTaskError(
                "WORKSPACE_TASK_NOT_FOUND",
                f"workspace task '{task_id}' not found",
                status_code=404,
            )
        return task

    async def create_task(
        self,
        *,
        user_id: str,
        workspace_id: str,
        title: str,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise WorkspaceTaskError(
                "WORKSPACE_TASK_INVALID_TITLE",
                "task title cannot be empty",
                status_code=422,
            )
        now = _now_ms()
        task = WorkspaceTask(
            user_id=user_id,
            workspace_id=workspace_id,
            title=title,
            description=description.strip(),
            status="OPEN",
            task_metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        async with await get_db() as db:
            db.add(task)
            await db.commit()
            await db.refresh(task)
        return _task_to_dict(task)

    async def get_task(self, *, user_id: str, workspace_id: str, task_id: str) -> dict[str, Any]:
        task = await self._get_task(user_id=user_id, workspace_id=workspace_id, task_id=task_id)
        return _task_to_dict(task)

    async def list_tasks(
        self, *, user_id: str, workspace_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        async with await get_db() as db:
            query = select(WorkspaceTask).where(
                WorkspaceTask.user_id == user_id,
                WorkspaceTask.workspace_id == workspace_id,
            )
            if status:
                query = query.where(WorkspaceTask.status == status.upper())
            query = query.order_by(WorkspaceTask.updated_at.desc())
            result = await db.execute(query)
            tasks = list(result.scalars().all())
        return [_task_to_dict(t) for t in tasks]

    async def update_task_status(
        self,
        *,
        user_id: str,
        workspace_id: str,
        task_id: str,
        status: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status_upper = status.strip().upper()
        if status_upper not in TASK_STATUSES:
            raise WorkspaceTaskError(
                "WORKSPACE_TASK_INVALID_STATUS",
                f"invalid task status '{status}'. Must be one of {sorted(TASK_STATUSES)}",
                status_code=422,
            )
        now = _now_ms()
        async with await get_db() as db:
            result = await db.execute(
                select(WorkspaceTask).where(
                    WorkspaceTask.id == task_id,
                    WorkspaceTask.user_id == user_id,
                    WorkspaceTask.workspace_id == workspace_id,
                )
            )
            task = result.scalar_one_or_none()
            if task is None:
                raise WorkspaceTaskError(
                    "WORKSPACE_TASK_NOT_FOUND",
                    f"workspace task '{task_id}' not found",
                    status_code=404,
                )
            # State-machine gate: INTEGRATED requires prior VERIFIED state
            # and the task must have at least one PASSED evidence with no FAILED evidence.
            if status_upper == "INTEGRATED":
                if task.status != "VERIFIED":
                    raise WorkspaceTaskError(
                        "WORKSPACE_TASK_INVALID_TRANSITION",
                        f"cannot transition from '{task.status}' to 'INTEGRATED'; "
                        "task must be in VERIFIED state first",
                        status_code=422,
                    )
                evidence_result = await db.execute(
                    select(WorkspaceTaskEvidence.status).where(
                        WorkspaceTaskEvidence.task_id == task_id
                    )
                )
                evidence_statuses = [
                    str(value).upper() for value in evidence_result.scalars().all()
                ]
                if (
                    not evidence_statuses
                    or "PASSED" not in evidence_statuses
                    or "FAILED" in evidence_statuses
                    or "PENDING" in evidence_statuses
                ):
                    raise WorkspaceTaskError(
                        "WORKSPACE_TASK_VERIFICATION_REQUIRED",
                        "integration requires at least one PASSED verification evidence item "
                        "and no FAILED or PENDING evidence",
                        status_code=422,
                    )
            task.status = status_upper
            task.updated_at = now
            if description is not None:
                task.description = description.strip()
            if metadata is not None:
                task.task_metadata = metadata
            if status_upper in {"CLOSED", "INTEGRATED", "FAILED"}:
                if task.closed_at is None:
                    task.closed_at = now
            else:
                task.closed_at = None
            await db.commit()
            await db.refresh(task)
            return _task_to_dict(task)

    async def pin_repository(
        self,
        *,
        user_id: str,
        workspace: Workspace | Path | str | dict[str, Any],
        task_id: str,
        repo_path: str = ".",
        pinned_revision: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        workspace_path = _get_workspace_path(workspace)
        workspace_id = (
            workspace.id
            if hasattr(workspace, "id")
            else (workspace.get("id") if isinstance(workspace, dict) else "")
        )
        if not workspace_id:
            raise WorkspaceTaskError(
                "WORKSPACE_TASK_MISSING_WORKSPACE_ID",
                "workspace_id is required; refusing to pin without ownership verification",
                status_code=422,
            )
        await self._get_task(user_id=user_id, workspace_id=workspace_id, task_id=task_id)

        normalized_repo = _safe_relative_path(repo_path)
        resolved_repo = _resolve_repo_path(workspace_path, normalized_repo)
        identity = await identity_for_user_id(user_id)

        if not await is_repo(str(resolved_repo), identity):
            raise WorkspaceTaskError(
                "WORKSPACE_TASK_NOT_GIT_REPO",
                f"path '{normalized_repo}' is not a Git repository",
                status_code=422,
            )

        repo_root = Path(await repository_root(str(resolved_repo), identity)).resolve()
        rev = pinned_revision or await current_revision(str(repo_root), identity)
        br = branch if branch is not None else await current_branch(str(repo_root), identity)
        now = _now_ms()

        async with await get_db() as db:
            result = await db.execute(
                select(WorkspaceTaskRepositoryPin).where(
                    WorkspaceTaskRepositoryPin.task_id == task_id,
                    WorkspaceTaskRepositoryPin.repo_path == normalized_repo,
                )
            )
            pin = result.scalar_one_or_none()
            # Capture current HEAD regardless of which revision is being pinned
            head_rev = await current_revision(str(repo_root), identity)
            if pin is None:
                pin = WorkspaceTaskRepositoryPin(
                    task_id=task_id,
                    repo_path=normalized_repo,
                    pinned_revision=rev,
                    head_revision=head_rev,
                    branch=br,
                    pinned_at=now,
                    updated_at=now,
                )
                db.add(pin)
            else:
                pin.pinned_revision = rev
                pin.head_revision = head_rev
                pin.branch = br
                pin.updated_at = now
            await db.commit()
            await db.refresh(pin)
            return _pin_to_dict(pin)

    async def list_repository_pins(
        self, *, user_id: str, workspace_id: str, task_id: str
    ) -> list[dict[str, Any]]:
        await self._get_task(user_id=user_id, workspace_id=workspace_id, task_id=task_id)
        async with await get_db() as db:
            result = await db.execute(
                select(WorkspaceTaskRepositoryPin)
                .where(WorkspaceTaskRepositoryPin.task_id == task_id)
                .order_by(WorkspaceTaskRepositoryPin.repo_path)
            )
            pins = list(result.scalars().all())
        return [_pin_to_dict(p) for p in pins]

    async def check_divergence(
        self,
        *,
        user_id: str,
        workspace: Workspace | Path | str | dict[str, Any],
        task_id: str,
        repo_path: str | None = None,
        target_revision: str = "HEAD",
    ) -> list[dict[str, Any]]:
        workspace_path = _get_workspace_path(workspace)
        identity = await identity_for_user_id(user_id)

        async with await get_db() as db:
            query = select(WorkspaceTaskRepositoryPin).where(
                WorkspaceTaskRepositoryPin.task_id == task_id
            )
            if repo_path is not None:
                query = query.where(
                    WorkspaceTaskRepositoryPin.repo_path == _safe_relative_path(repo_path)
                )
            result = await db.execute(query)
            pins = list(result.scalars().all())

        divergences: list[dict[str, Any]] = []
        for pin in pins:
            resolved_repo = _resolve_repo_path(workspace_path, pin.repo_path)
            if not await is_repo(str(resolved_repo), identity):
                divergences.append(
                    {
                        "pin_id": pin.id,
                        "repo_path": pin.repo_path,
                        "pinned_revision": pin.pinned_revision,
                        "pinned_branch": pin.branch,
                        "head_revision": pin.head_revision,
                        "error": "repository path not found or invalid",
                        "behind": 0,
                        "ahead": 0,
                        "is_diverged": False,
                    }
                )
                continue

            repo_root = str(await repository_root(str(resolved_repo), identity))
            cur_rev = await current_revision(repo_root, identity)
            cur_branch = await current_branch(repo_root, identity)
            div = await revision_divergence(
                repo_root,
                pin.pinned_revision,
                target_revision=target_revision,
                identity=identity,
            )
            is_div = bool(div["behind"] > 0 or div["ahead"] > 0)
            divergences.append(
                {
                    "pin_id": pin.id,
                    "repo_path": pin.repo_path,
                    "pinned_revision": pin.pinned_revision,
                    "pinned_branch": pin.branch,
                    "head_revision": pin.head_revision,
                    "current_branch": cur_branch,
                    "current_revision": cur_rev,
                    "target_revision": target_revision,
                    "behind": div["behind"],
                    "ahead": div["ahead"],
                    "is_diverged": is_div,
                    "pinned_at": pin.pinned_at,
                    "updated_at": pin.updated_at,
                }
            )
        return divergences

    async def link_worker(
        self,
        *,
        user_id: str,
        workspace_id: str,
        task_id: str,
        worker_id: str,
        role: str = "contributor",
    ) -> dict[str, Any]:
        await self._get_task(user_id=user_id, workspace_id=workspace_id, task_id=task_id)
        # Verify worker exists and belongs to user & workspace
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
                raise WorkspaceTaskError(
                    "WORKSPACE_TASK_WORKER_NOT_FOUND",
                    f"worker '{worker_id}' not found in workspace",
                    status_code=404,
                )

            link_result = await db.execute(
                select(WorkspaceTaskWorkerLink).where(
                    WorkspaceTaskWorkerLink.task_id == task_id,
                    WorkspaceTaskWorkerLink.worker_id == worker_id,
                )
            )
            link = link_result.scalar_one_or_none()
            now = _now_ms()
            if link is None:
                link = WorkspaceTaskWorkerLink(
                    task_id=task_id,
                    worker_id=worker_id,
                    role=role.strip() or "contributor",
                    status="ACTIVE",
                    linked_at=now,
                )
                db.add(link)
            else:
                link.role = role.strip() or link.role
                link.status = "ACTIVE"
            await db.commit()
            await db.refresh(link)
            return _link_to_dict(link)

    async def unlink_worker(
        self,
        *,
        user_id: str,
        workspace_id: str,
        task_id: str,
        worker_id: str,
        status: str = "DETACHED",
    ) -> None:
        await self._get_task(user_id=user_id, workspace_id=workspace_id, task_id=task_id)
        if status not in WORKER_LINK_STATUSES:
            raise WorkspaceTaskError(
                "WORKSPACE_TASK_INVALID_WORKER_STATUS",
                f"invalid worker link status '{status}'. Must be one of {sorted(WORKER_LINK_STATUSES)}",
                status_code=422,
            )
        async with await get_db() as db:
            result = await db.execute(
                select(WorkspaceTaskWorkerLink).where(
                    WorkspaceTaskWorkerLink.task_id == task_id,
                    WorkspaceTaskWorkerLink.worker_id == worker_id,
                )
            )
            link = result.scalar_one_or_none()
            if link is not None:
                link.status = status
                await db.commit()

    async def list_workers(
        self, *, user_id: str, workspace_id: str, task_id: str
    ) -> list[dict[str, Any]]:
        await self._get_task(user_id=user_id, workspace_id=workspace_id, task_id=task_id)
        identity = await identity_for_user_id(user_id)
        async with await get_db() as db:
            result = await db.execute(
                select(WorkspaceTaskWorkerLink)
                .where(WorkspaceTaskWorkerLink.task_id == task_id)
                .order_by(WorkspaceTaskWorkerLink.linked_at)
            )
            links = list(result.scalars().all())

            worker_items: list[dict[str, Any]] = []
            for link in links:
                try:
                    w_res = await db.execute(
                        select(DirectCodingWorker).where(
                            DirectCodingWorker.id == link.worker_id,
                            DirectCodingWorker.user_id == user_id,
                            DirectCodingWorker.workspace_id == workspace_id,
                        )
                    )
                    worker = w_res.scalar_one_or_none()
                    worker_summary: dict[str, Any] = {}
                    base_divergence: dict[str, int] = {"behind": 0, "ahead": 0}
                    if worker is not None:
                        worker_summary = await direct_worker_service.summary(worker)
                        wt = Path(worker.worktree_path)
                        if wt.is_dir():
                            base_divergence = await revision_divergence(
                                str(wt),
                                worker.base_revision,
                                target_revision="HEAD",
                                identity=identity,
                            )

                    item = _link_to_dict(link)
                    item["worker"] = worker_summary
                    item["base_divergence"] = base_divergence
                    worker_items.append(item)
                except Exception as _worker_err:  # noqa: BLE001
                    # One corrupt/inaccessible worker must not abort the aggregation
                    item = _link_to_dict(link)
                    item["worker"] = {}
                    item["base_divergence"] = {"behind": 0, "ahead": 0}
                    item["error"] = str(_worker_err)
                    worker_items.append(item)
        return worker_items

    async def add_evidence(
        self,
        *,
        user_id: str,
        workspace_id: str,
        task_id: str,
        kind: str,
        status: str,
        summary: str,
        worker_id: str | None = None,
        repo_path: str | None = None,
        command: str | None = None,
        details: dict[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        await self._get_task(user_id=user_id, workspace_id=workspace_id, task_id=task_id)
        kind_lower = kind.strip().lower()
        if kind_lower not in EVIDENCE_KINDS:
            raise WorkspaceTaskError(
                "WORKSPACE_TASK_INVALID_EVIDENCE_KIND",
                f"invalid evidence kind '{kind}'. Must be one of {sorted(EVIDENCE_KINDS)}",
                status_code=422,
            )
        status_upper = status.strip().upper()
        if status_upper not in EVIDENCE_STATUSES:
            raise WorkspaceTaskError(
                "WORKSPACE_TASK_INVALID_EVIDENCE_STATUS",
                f"invalid evidence status '{status}'. Must be one of {sorted(EVIDENCE_STATUSES)}",
                status_code=422,
            )
        summary = summary.strip()
        if not summary:
            raise WorkspaceTaskError(
                "WORKSPACE_TASK_INVALID_EVIDENCE_SUMMARY",
                "evidence summary cannot be empty",
                status_code=422,
            )

        now = _now_ms()
        norm_repo = _safe_relative_path(repo_path) if repo_path else None
        evidence = WorkspaceTaskEvidence(
            task_id=task_id,
            worker_id=worker_id,
            repo_path=norm_repo,
            kind=kind_lower,
            status=status_upper,
            summary=summary,
            command=command,
            details=details or {},
            fingerprint=fingerprint,
            created_at=now,
        )
        async with await get_db() as db:
            db.add(evidence)
            # Update task updated_at
            db_task = await db.get(WorkspaceTask, task_id)
            if db_task:
                db_task.updated_at = now
            await db.commit()
            await db.refresh(evidence)
            return _evidence_to_dict(evidence)

    async def list_evidence(
        self,
        *,
        user_id: str,
        workspace_id: str,
        task_id: str,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        await self._get_task(user_id=user_id, workspace_id=workspace_id, task_id=task_id)
        async with await get_db() as db:
            query = select(WorkspaceTaskEvidence).where(WorkspaceTaskEvidence.task_id == task_id)
            if kind:
                query = query.where(WorkspaceTaskEvidence.kind == kind.lower())
            if status:
                query = query.where(WorkspaceTaskEvidence.status == status.upper())
            query = query.order_by(WorkspaceTaskEvidence.created_at.desc())
            result = await db.execute(query)
            items = list(result.scalars().all())
        return [_evidence_to_dict(e) for e in items]

    async def verification_summary(
        self, *, user_id: str, workspace_id: str, task_id: str
    ) -> dict[str, Any]:
        evidence_list = await self.list_evidence(
            user_id=user_id, workspace_id=workspace_id, task_id=task_id
        )
        passed = sum(1 for e in evidence_list if e["status"] == "PASSED")
        failed = sum(1 for e in evidence_list if e["status"] == "FAILED")
        pending = sum(1 for e in evidence_list if e["status"] == "PENDING")
        observed = sum(1 for e in evidence_list if e["status"] == "OBSERVED")

        by_kind: dict[str, dict[str, int]] = {}
        failures: list[str] = []
        for e in evidence_list:
            k = e["kind"]
            if k not in by_kind:
                by_kind[k] = {"total": 0, "passed": 0, "failed": 0, "pending": 0, "observed": 0}
            by_kind[k]["total"] += 1
            stat_key = e["status"].lower()
            if stat_key in by_kind[k]:
                by_kind[k][stat_key] += 1
            if e["status"] == "FAILED":
                failures.append(f"[{e['kind']}] {e['summary']}")

        verified = bool(len(evidence_list) > 0 and failed == 0 and pending == 0 and passed > 0)
        return {
            "task_id": task_id,
            "total": len(evidence_list),
            "passed": passed,
            "failed": failed,
            "pending": pending,
            "observed": observed,
            "verified": verified,
            "by_kind": by_kind,
            "failures": failures,
        }

    async def summary(
        self,
        *,
        user_id: str,
        workspace: Workspace | Path | str | dict[str, Any],
        task_id: str,
    ) -> dict[str, Any]:
        workspace_id = (
            workspace.id
            if hasattr(workspace, "id")
            else (workspace.get("id") if isinstance(workspace, dict) else "")
        )
        task = await self.get_task(user_id=user_id, workspace_id=workspace_id, task_id=task_id)
        repos = await self.check_divergence(user_id=user_id, workspace=workspace, task_id=task_id)
        workers = await self.list_workers(
            user_id=user_id, workspace_id=workspace_id, task_id=task_id
        )
        verification = await self.verification_summary(
            user_id=user_id, workspace_id=workspace_id, task_id=task_id
        )
        has_divergence = any(r.get("is_diverged") for r in repos)
        all_workers_clean = all(
            not w.get("error")
            and (
                w.get("worker", {}).get("changed_file_count", 0) == 0
                or w.get("worker", {}).get("status") == "INTEGRATED"
            )
            for w in workers
        )
        return {
            "task": task,
            "repositories": repos,
            "workers": workers,
            "verification": verification,
            "has_divergence": has_divergence,
            "all_workers_clean": all_workers_clean,
            "all_passed": verification["verified"],
        }


service = WorkspaceTaskService()
workspace_task_service = service
