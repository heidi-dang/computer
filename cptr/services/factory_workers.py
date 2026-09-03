"""Durable Dark Factory worker ownership and quiescence control.

The factory composes ``DirectCodingWorkerService`` rather than creating Git
worktrees itself. Mutation scopes are persisted and checked before assignment;
read-only investigations may overlap up to a bounded parallelism limit. Worker
processes are quiesced through CPTR command-session controls, never unmanaged
shell process discovery.
"""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Awaitable, Callable, Iterable, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import FactoryCycle, FactoryRun, FactoryWorkerAssignment, Workspace
from cptr.services.direct_coding_workers import (
    DirectCodingWorkerError,
    DirectCodingWorkerService,
    service as direct_worker_service,
)
from cptr.utils.db import get_db, get_session_factory
from cptr.utils.tools import get_command_session, signal_command_session


class FactoryWorkerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FactoryWorkerAssignmentMode(str, Enum):
    READ_ONLY = "READ_ONLY"
    MUTATION = "MUTATION"


class FactoryWorkerAssignmentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CANCELLING = "CANCELLING"
    QUIESCENT = "QUIESCENT"
    INTEGRATED = "INTEGRATED"
    CLOSED = "CLOSED"
    MISSING = "MISSING"


_BLOCKING_MUTATION_STATUSES = {
    FactoryWorkerAssignmentStatus.ACTIVE.value,
    FactoryWorkerAssignmentStatus.CANCELLING.value,
    FactoryWorkerAssignmentStatus.QUIESCENT.value,
    FactoryWorkerAssignmentStatus.MISSING.value,
}
_ACTIVE_READ_ONLY_STATUSES = {FactoryWorkerAssignmentStatus.ACTIVE.value}
_LOOP_WORKSPACE_LOCKS: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()


def _workspace_assignment_lock(workspace_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    per_workspace = _LOOP_WORKSPACE_LOCKS.setdefault(loop, {})
    lock = per_workspace.get(workspace_id)
    if lock is None:
        lock = asyncio.Lock()
        per_workspace[workspace_id] = lock
    return lock


def _now_ms() -> int:
    return int(time.time() * 1000)


def _assignment_id() -> str:
    return f"fworker_{uuid.uuid4().hex}"


def _normalize_repo_path(value: str) -> str:
    raw = (value or ".").strip() or "."
    path = Path(raw)
    windows = PureWindowsPath(raw)
    if path.is_absolute() or windows.is_absolute() or ".." in path.parts:
        raise FactoryWorkerError(
            "FACTORY_WORKER_INVALID_REPO_PATH",
            "repo_path must remain relative to the authorized workspace",
        )
    return path.as_posix()


def _normalize_scope(scope: Iterable[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw_value in scope:
        raw = str(raw_value).strip().replace("\\", "/")
        if not raw:
            raise FactoryWorkerError("FACTORY_WORKER_INVALID_SCOPE", "scope path must not be blank")
        path = PurePosixPath(raw)
        windows = PureWindowsPath(raw)
        if path.is_absolute() or windows.is_absolute() or ".." in path.parts:
            raise FactoryWorkerError(
                "FACTORY_WORKER_INVALID_SCOPE",
                "scope paths must be workspace-relative and must not contain parent traversal",
            )
        value = path.as_posix().strip("/")
        normalized.add("." if value in {"", "."} else value)
    if not normalized:
        raise FactoryWorkerError(
            "FACTORY_WORKER_INVALID_SCOPE",
            "at least one owned path scope is required",
        )
    return tuple(sorted(normalized))


def _path_prefix(left: str, right: str) -> bool:
    if left == ".":
        return True
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    return len(left_parts) <= len(right_parts) and right_parts[: len(left_parts)] == left_parts


def scopes_overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    left_scope = _normalize_scope(left)
    right_scope = _normalize_scope(right)
    return any(_path_prefix(a, b) or _path_prefix(b, a) for a in left_scope for b in right_scope)


class WorkerCommandController(Protocol):
    async def terminate_and_wait(
        self,
        *,
        user_id: str,
        command_id: str,
        timeout_ms: int,
    ) -> bool: ...


class DefaultWorkerCommandController:
    """Quiesce one CPTR-owned command session by opaque command ID."""

    @staticmethod
    def _quiescent(user_id: str, command_id: str) -> bool:
        session = get_command_session(None, command_id, context={"user_id": user_id})
        if session is None or session.get("done"):
            return True
        proc = session.get("proc")
        if proc is None or getattr(proc, "returncode", None) is not None:
            return True
        poll = getattr(proc, "poll", None)
        return bool(callable(poll) and poll() is not None)

    async def _wait(self, *, user_id: str, command_id: str, timeout_ms: int) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while not self._quiescent(user_id, command_id):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.02, remaining))
        return True

    async def terminate_and_wait(
        self,
        *,
        user_id: str,
        command_id: str,
        timeout_ms: int,
    ) -> bool:
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        error = signal_command_session(
            None,
            command_id,
            "terminate",
            context={"user_id": user_id},
        )
        if error == "command session not found":
            return True
        if error is not None:
            return False
        if await self._wait(user_id=user_id, command_id=command_id, timeout_ms=timeout_ms):
            return True
        signal_command_session(None, command_id, "kill", context={"user_id": user_id})
        return await self._wait(
            user_id=user_id,
            command_id=command_id,
            timeout_ms=min(timeout_ms, 1000),
        )


class SqlFactoryWorkerStore:
    """SQLite-backed durable assignment projection with serialized mutation ownership."""

    def __init__(self, *, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def get(self, assignment_id: str) -> FactoryWorkerAssignment | None:
        async with self._session_factory() as db:
            return await db.get(FactoryWorkerAssignment, assignment_id)

    async def list_for_run(self, run_id: str) -> list[FactoryWorkerAssignment]:
        async with self._session_factory() as db:
            rows = await db.execute(
                select(FactoryWorkerAssignment)
                .where(FactoryWorkerAssignment.run_id == run_id)
                .order_by(FactoryWorkerAssignment.created_at, FactoryWorkerAssignment.id)
            )
            return list(rows.scalars().all())

    async def count_active_read_only(self, *, run_id: str, cycle_id: str) -> int:
        async with self._session_factory() as db:
            rows = await db.execute(
                select(FactoryWorkerAssignment.id).where(
                    FactoryWorkerAssignment.run_id == run_id,
                    FactoryWorkerAssignment.cycle_id == cycle_id,
                    FactoryWorkerAssignment.mode == FactoryWorkerAssignmentMode.READ_ONLY.value,
                    FactoryWorkerAssignment.status.in_(_ACTIVE_READ_ONLY_STATUSES),
                )
            )
            return len(rows.all())

    async def ensure_mutation_scope_available(
        self,
        *,
        workspace_id: str,
        scope: Iterable[str],
    ) -> None:
        normalized = _normalize_scope(scope)
        async with self._session_factory() as db:
            rows = await db.execute(
                select(FactoryWorkerAssignment).where(
                    FactoryWorkerAssignment.workspace_id == workspace_id,
                    FactoryWorkerAssignment.mode == FactoryWorkerAssignmentMode.MUTATION.value,
                    FactoryWorkerAssignment.status.in_(_BLOCKING_MUTATION_STATUSES),
                )
            )
            for existing in rows.scalars().all():
                if scopes_overlap(existing.scope or (), normalized):
                    raise FactoryWorkerError(
                        "FACTORY_WORKER_SCOPE_CONFLICT",
                        f"mutation scope overlaps active assignment {existing.id}",
                    )

    async def create_assignment(
        self,
        *,
        run_id: str,
        cycle_id: str,
        workspace_id: str,
        worker_id: str | None,
        owner_key: str,
        mode: FactoryWorkerAssignmentMode,
        repo_path: str,
        scope: Iterable[str],
        branch: str | None = None,
        base_revision: str | None = None,
        max_read_only_assignments: int | None = None,
    ) -> FactoryWorkerAssignment:
        normalized_scope = _normalize_scope(scope)
        normalized_repo = _normalize_repo_path(repo_path)
        owner_key = owner_key.strip()
        if not owner_key:
            raise FactoryWorkerError("FACTORY_WORKER_INVALID_OWNER", "owner_key must not be blank")
        if mode is FactoryWorkerAssignmentMode.MUTATION and not worker_id:
            raise FactoryWorkerError(
                "FACTORY_WORKER_ID_REQUIRED",
                "mutation assignments require a direct worker ID",
            )

        if max_read_only_assignments is not None and max_read_only_assignments <= 0:
            raise ValueError("max_read_only_assignments must be positive")

        lock = _workspace_assignment_lock(workspace_id)
        async with lock:
            async with self._session_factory() as db:
                try:
                    async with db.begin():
                        run = await db.get(FactoryRun, run_id)
                        cycle = await db.get(FactoryCycle, cycle_id)
                        if run is None or cycle is None or cycle.run_id != run_id:
                            raise FactoryWorkerError(
                                "FACTORY_WORKER_RUN_CYCLE_MISMATCH",
                                "factory run/cycle association is invalid",
                            )
                        if run.workspace_id != workspace_id:
                            raise FactoryWorkerError(
                                "FACTORY_WORKER_CROSS_WORKSPACE",
                                "assignment workspace differs from the factory run workspace",
                            )
                        # Acquire SQLite's cross-process write lock before the
                        # ownership read. The loop-local lock above prevents
                        # same-connection async races in StaticPool tests.
                        await db.execute(
                            update(FactoryRun)
                            .where(FactoryRun.id == run_id)
                            .values(updated_at=FactoryRun.updated_at)
                        )
                        if mode is FactoryWorkerAssignmentMode.MUTATION:
                            rows = await db.execute(
                                select(FactoryWorkerAssignment).where(
                                    FactoryWorkerAssignment.workspace_id == workspace_id,
                                    FactoryWorkerAssignment.mode
                                    == FactoryWorkerAssignmentMode.MUTATION.value,
                                    FactoryWorkerAssignment.status.in_(_BLOCKING_MUTATION_STATUSES),
                                )
                            )
                            for existing in rows.scalars().all():
                                if scopes_overlap(existing.scope or (), normalized_scope):
                                    raise FactoryWorkerError(
                                        "FACTORY_WORKER_SCOPE_CONFLICT",
                                        f"mutation scope overlaps active assignment {existing.id}",
                                    )
                        elif max_read_only_assignments is not None:
                            rows = await db.execute(
                                select(FactoryWorkerAssignment.id).where(
                                    FactoryWorkerAssignment.run_id == run_id,
                                    FactoryWorkerAssignment.cycle_id == cycle_id,
                                    FactoryWorkerAssignment.mode
                                    == FactoryWorkerAssignmentMode.READ_ONLY.value,
                                    FactoryWorkerAssignment.status.in_(_ACTIVE_READ_ONLY_STATUSES),
                                )
                            )
                            if len(rows.all()) >= max_read_only_assignments:
                                raise FactoryWorkerError(
                                    "FACTORY_WORKER_READ_ONLY_LIMIT",
                                    "bounded read-only investigation limit reached",
                                )
                        now = _now_ms()
                        assignment = FactoryWorkerAssignment(
                            id=_assignment_id(),
                            run_id=run_id,
                            cycle_id=cycle_id,
                            workspace_id=workspace_id,
                            worker_id=worker_id,
                            owner_key=owner_key,
                            mode=mode.value,
                            repo_path=normalized_repo,
                            scope=list(normalized_scope),
                            branch=branch,
                            base_revision=base_revision,
                            status=FactoryWorkerAssignmentStatus.ACTIVE.value,
                            created_at=now,
                            updated_at=now,
                        )
                        db.add(assignment)
                    return assignment
                except IntegrityError as exc:
                    raise FactoryWorkerError(
                        "FACTORY_WORKER_DUPLICATE_ASSIGNMENT",
                        "worker is already owned by a factory assignment",
                    ) from exc

    async def set_status(
        self,
        assignment_id: str,
        status: FactoryWorkerAssignmentStatus,
        *,
        branch: str | None = None,
        base_revision: str | None = None,
    ) -> FactoryWorkerAssignment:
        async with self._session_factory() as db:
            async with db.begin():
                assignment = await db.get(FactoryWorkerAssignment, assignment_id)
                if assignment is None:
                    raise FactoryWorkerError(
                        "FACTORY_WORKER_ASSIGNMENT_NOT_FOUND",
                        "factory worker assignment not found",
                    )
                assignment.status = status.value
                assignment.updated_at = _now_ms()
                if branch is not None:
                    assignment.branch = branch
                if base_revision is not None:
                    assignment.base_revision = base_revision
                if status is FactoryWorkerAssignmentStatus.CLOSED:
                    assignment.closed_at = assignment.updated_at
            return assignment


@dataclass(frozen=True)
class WorkerQuiescenceResult:
    quiescent: bool
    failed_command_ids: tuple[str, ...]
    unresolved_assignment_ids: tuple[str, ...]


WorkspaceLoader = Callable[..., Awaitable[Workspace]]


async def _default_workspace_loader(*, user_id: str, workspace_id: str) -> Workspace:
    async with await get_db() as db:
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None or workspace.user_id != user_id:
            raise KeyError("workspace not found")
        return workspace


class FactoryWorkerController:
    def __init__(
        self,
        *,
        store: SqlFactoryWorkerStore | None = None,
        worker_service: DirectCodingWorkerService | Any = direct_worker_service,
        workspace_loader: WorkspaceLoader = _default_workspace_loader,
        command_controller: WorkerCommandController | None = None,
        max_read_only_assignments: int = 8,
    ) -> None:
        if max_read_only_assignments <= 0:
            raise ValueError("max_read_only_assignments must be positive")
        self._store = store or SqlFactoryWorkerStore()
        self._worker_service = worker_service
        self._workspace_loader = workspace_loader
        self._command_controller = command_controller or DefaultWorkerCommandController()
        self._max_read_only_assignments = int(max_read_only_assignments)

    @staticmethod
    def _validate_run_cycle(run: FactoryRun, cycle: FactoryCycle) -> None:
        if cycle.run_id != run.id:
            raise FactoryWorkerError(
                "FACTORY_WORKER_RUN_CYCLE_MISMATCH",
                "cycle does not belong to the supplied run",
            )
        if run.current_cycle_id and run.current_cycle_id != cycle.id:
            raise FactoryWorkerError(
                "FACTORY_WORKER_STALE_CYCLE",
                "worker assignment requires the run's current cycle",
            )

    async def _workspace(self, run: FactoryRun) -> Workspace:
        try:
            workspace = await self._workspace_loader(
                user_id=run.user_id,
                workspace_id=run.workspace_id,
            )
        except KeyError as exc:
            raise FactoryWorkerError(
                "FACTORY_WORKER_WORKSPACE_NOT_FOUND",
                "factory run workspace is unavailable",
            ) from exc
        if workspace.id != run.workspace_id:
            raise FactoryWorkerError(
                "FACTORY_WORKER_CROSS_WORKSPACE",
                "workspace loader returned a different workspace",
            )
        return workspace

    async def _persist_mutation_summary(
        self,
        run: FactoryRun,
        cycle: FactoryCycle,
        *,
        repo_path: str,
        scope: Iterable[str],
        summary: dict[str, Any],
    ) -> FactoryWorkerAssignment:
        if summary.get("workspace_id") != run.workspace_id:
            raise FactoryWorkerError(
                "FACTORY_WORKER_CROSS_WORKSPACE",
                "direct worker belongs to a different workspace",
            )
        worker_id = str(summary.get("worker_id") or "").strip()
        branch = str(summary.get("branch") or "").strip()
        base_revision = str(summary.get("base_revision") or "").strip()
        if not worker_id or not branch or not base_revision:
            raise FactoryWorkerError(
                "FACTORY_WORKER_INVALID_WORKER_SUMMARY",
                "direct worker summary lacks immutable ownership metadata",
            )
        if str(summary.get("status") or "").upper() in {"CLOSED", "INTEGRATED"}:
            raise FactoryWorkerError(
                "FACTORY_WORKER_NOT_MUTABLE",
                "closed or integrated workers cannot receive a mutation assignment",
            )
        if cycle.base_revision and base_revision != cycle.base_revision:
            raise FactoryWorkerError(
                "FACTORY_WORKER_BASE_REVISION_MISMATCH",
                "direct worker base revision differs from the factory cycle base revision",
            )
        return await self._store.create_assignment(
            run_id=run.id,
            cycle_id=cycle.id,
            workspace_id=run.workspace_id,
            worker_id=worker_id,
            owner_key=worker_id,
            mode=FactoryWorkerAssignmentMode.MUTATION,
            repo_path=repo_path,
            scope=scope,
            branch=branch,
            base_revision=base_revision,
        )

    async def create_mutation_worker(
        self,
        run: FactoryRun,
        cycle: FactoryCycle,
        repo_path: str,
        *,
        scope: Iterable[str],
        name: str = "factory-mutation",
    ) -> FactoryWorkerAssignment:
        self._validate_run_cycle(run, cycle)
        normalized_scope = _normalize_scope(scope)
        normalized_repo = _normalize_repo_path(repo_path)
        await self._store.ensure_mutation_scope_available(
            workspace_id=run.workspace_id,
            scope=normalized_scope,
        )
        workspace = await self._workspace(run)
        summary = await self._worker_service.create(
            user_id=run.user_id,
            workspace=workspace,
            name=name,
            responsibility=f"dark-factory:{run.id}:{cycle.id}",
            repo_path=normalized_repo,
        )
        try:
            return await self._persist_mutation_summary(
                run,
                cycle,
                repo_path=normalized_repo,
                scope=normalized_scope,
                summary=summary,
            )
        except Exception:
            worker_id = str(summary.get("worker_id") or "")
            if worker_id:
                try:
                    await self._worker_service.close(
                        user_id=run.user_id,
                        workspace=workspace,
                        worker_id=worker_id,
                        discard_changes=False,
                    )
                except Exception:
                    pass
            raise

    async def assign_mutation(
        self,
        run: FactoryRun,
        cycle: FactoryCycle,
        *,
        worker_id: str,
        repo_path: str,
        scope: Iterable[str],
    ) -> FactoryWorkerAssignment:
        self._validate_run_cycle(run, cycle)
        summary = await self._worker_service.get(
            user_id=run.user_id,
            workspace_id=run.workspace_id,
            worker_id=worker_id,
        )
        return await self._persist_mutation_summary(
            run,
            cycle,
            repo_path=_normalize_repo_path(repo_path),
            scope=_normalize_scope(scope),
            summary=summary,
        )

    async def assign_read_only(
        self,
        run: FactoryRun,
        cycle: FactoryCycle,
        *,
        owner_key: str,
        scope: Iterable[str],
        repo_path: str = ".",
    ) -> FactoryWorkerAssignment:
        self._validate_run_cycle(run, cycle)
        await self._workspace(run)
        count = await self._store.count_active_read_only(run_id=run.id, cycle_id=cycle.id)
        if count >= self._max_read_only_assignments:
            raise FactoryWorkerError(
                "FACTORY_WORKER_READ_ONLY_LIMIT",
                "bounded read-only investigation limit reached",
            )
        return await self._store.create_assignment(
            run_id=run.id,
            cycle_id=cycle.id,
            workspace_id=run.workspace_id,
            worker_id=None,
            owner_key=owner_key,
            mode=FactoryWorkerAssignmentMode.READ_ONLY,
            repo_path=repo_path,
            scope=scope,
            max_read_only_assignments=self._max_read_only_assignments,
        )

    async def reconcile(self, run: FactoryRun) -> list[FactoryWorkerAssignment]:
        records = await self._store.list_for_run(run.id)
        reconciled: list[FactoryWorkerAssignment] = []
        for record in records:
            if record.mode != FactoryWorkerAssignmentMode.MUTATION.value or record.status == FactoryWorkerAssignmentStatus.CLOSED.value:
                reconciled.append(record)
                continue
            if not record.worker_id:
                reconciled.append(
                    await self._store.set_status(record.id, FactoryWorkerAssignmentStatus.MISSING)
                )
                continue
            try:
                summary = await self._worker_service.get(
                    user_id=run.user_id,
                    workspace_id=run.workspace_id,
                    worker_id=record.worker_id,
                )
            except DirectCodingWorkerError as exc:
                if exc.code not in {
                    "DIRECT_WORKER_NOT_FOUND",
                    "DIRECT_WORKER_CLOSED",
                    "DIRECT_WORKER_WORKTREE_MISSING",
                }:
                    raise
                reconciled.append(
                    await self._store.set_status(record.id, FactoryWorkerAssignmentStatus.MISSING)
                )
                continue
            if summary.get("workspace_id") != run.workspace_id:
                reconciled.append(
                    await self._store.set_status(record.id, FactoryWorkerAssignmentStatus.MISSING)
                )
                continue
            branch = str(summary.get("branch") or "")
            base_revision = str(summary.get("base_revision") or "")
            if (record.branch and branch != record.branch) or (
                record.base_revision and base_revision != record.base_revision
            ):
                reconciled.append(
                    await self._store.set_status(record.id, FactoryWorkerAssignmentStatus.MISSING)
                )
                continue
            worker_status = str(summary.get("status") or "").upper()
            mapped = {
                "READY": FactoryWorkerAssignmentStatus.ACTIVE,
                "WORKING": FactoryWorkerAssignmentStatus.ACTIVE,
                "RUNNING": FactoryWorkerAssignmentStatus.ACTIVE,
                "INTEGRATED": FactoryWorkerAssignmentStatus.INTEGRATED,
                "CLOSED": FactoryWorkerAssignmentStatus.CLOSED,
            }.get(worker_status, FactoryWorkerAssignmentStatus.MISSING)
            reconciled.append(
                await self._store.set_status(
                    record.id,
                    mapped,
                    branch=branch or None,
                    base_revision=base_revision or None,
                )
            )
        return reconciled

    async def cancel_run(self, run: FactoryRun, *, timeout_ms: int) -> WorkerQuiescenceResult:
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        failed_commands: set[str] = set()
        unresolved: set[str] = set()
        records = await self._store.list_for_run(run.id)
        for record in records:
            if record.mode != FactoryWorkerAssignmentMode.MUTATION.value:
                continue
            if record.status in {
                FactoryWorkerAssignmentStatus.CLOSED.value,
                FactoryWorkerAssignmentStatus.INTEGRATED.value,
                FactoryWorkerAssignmentStatus.QUIESCENT.value,
            }:
                continue
            if record.status == FactoryWorkerAssignmentStatus.MISSING.value or not record.worker_id:
                unresolved.add(record.id)
                continue
            try:
                summary = await self._worker_service.get(
                    user_id=run.user_id,
                    workspace_id=run.workspace_id,
                    worker_id=record.worker_id,
                )
            except DirectCodingWorkerError as exc:
                if exc.code not in {
                    "DIRECT_WORKER_NOT_FOUND",
                    "DIRECT_WORKER_CLOSED",
                    "DIRECT_WORKER_WORKTREE_MISSING",
                }:
                    raise
                await self._store.set_status(record.id, FactoryWorkerAssignmentStatus.MISSING)
                unresolved.add(record.id)
                continue
            if summary.get("workspace_id") != run.workspace_id:
                await self._store.set_status(record.id, FactoryWorkerAssignmentStatus.MISSING)
                unresolved.add(record.id)
                continue
            active_ids = tuple(sorted({str(item) for item in summary.get("active_command_ids") or [] if str(item)}))
            if not active_ids:
                await self._store.set_status(record.id, FactoryWorkerAssignmentStatus.QUIESCENT)
                continue
            await self._store.set_status(record.id, FactoryWorkerAssignmentStatus.CANCELLING)
            assignment_failed = False
            for command_id in active_ids:
                if not await self._command_controller.terminate_and_wait(
                    user_id=run.user_id,
                    command_id=command_id,
                    timeout_ms=timeout_ms,
                ):
                    failed_commands.add(command_id)
                    assignment_failed = True
            if not assignment_failed:
                await self._store.set_status(record.id, FactoryWorkerAssignmentStatus.QUIESCENT)
        return WorkerQuiescenceResult(
            quiescent=not failed_commands and not unresolved,
            failed_command_ids=tuple(sorted(failed_commands)),
            unresolved_assignment_ids=tuple(sorted(unresolved)),
        )

    async def cleanup(
        self,
        run: FactoryRun,
        assignment_id: str,
        *,
        discard_changes: bool = False,
    ) -> FactoryWorkerAssignment:
        assignment = await self._store.get(assignment_id)
        if assignment is None or assignment.run_id != run.id:
            raise FactoryWorkerError(
                "FACTORY_WORKER_ASSIGNMENT_NOT_FOUND",
                "factory worker assignment not found for this run",
            )
        if assignment.workspace_id != run.workspace_id:
            raise FactoryWorkerError(
                "FACTORY_WORKER_CROSS_WORKSPACE",
                "assignment workspace differs from factory run",
            )
        if assignment.status == FactoryWorkerAssignmentStatus.CLOSED.value:
            return assignment
        if assignment.mode == FactoryWorkerAssignmentMode.READ_ONLY.value:
            return await self._store.set_status(assignment.id, FactoryWorkerAssignmentStatus.CLOSED)
        if assignment.status not in {
            FactoryWorkerAssignmentStatus.QUIESCENT.value,
            FactoryWorkerAssignmentStatus.INTEGRATED.value,
        }:
            raise FactoryWorkerError(
                "FACTORY_WORKER_NOT_QUIESCENT",
                "mutation worker must be quiescent or integrated before cleanup",
            )
        if not assignment.worker_id:
            raise FactoryWorkerError(
                "FACTORY_WORKER_ID_REQUIRED",
                "mutation assignment has no worker ID",
            )
        workspace = await self._workspace(run)
        await self._worker_service.close(
            user_id=run.user_id,
            workspace=workspace,
            worker_id=assignment.worker_id,
            discard_changes=discard_changes,
        )
        return await self._store.set_status(assignment.id, FactoryWorkerAssignmentStatus.CLOSED)
