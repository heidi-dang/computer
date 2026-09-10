"""Owner-bound task identity adapter for Capability OS.

Capability OS deliberately reuses CPTR's existing durable Workbench, Factory,
and Control task records instead of creating a parallel task namespace.
"""

from __future__ import annotations

from dataclasses import dataclass
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import ControlTask, FactoryRun, WorkbenchSession
from cptr.utils.db import get_session_factory


class CapabilityTaskNotFound(KeyError):
    pass


class CapabilityTaskNotExecutable(RuntimeError):
    pass


_WORKBENCH_TERMINAL = {"COMPLETE", "FAILED", "CANCELLED", "ARCHIVED"}
_WORKBENCH_WAITING = {"WAITING_APPROVAL", "REVIEW_REQUIRED"}
_FACTORY_TERMINAL = {"COMPLETE", "FAILED", "CANCELLED", "BLOCKED"}
_FACTORY_WAITING = {"PAUSED", "APPROVAL_REQUIRED", "RECOVERING"}
_CONTROL_TERMINAL = {
    "COMPLETE",
    "COMPLETE_WITH_TOOL_ERRORS",
    "FAILED",
    "CANCELLED",
    "REJECTED",
}
_CONTROL_WAITING = {"CANCEL_REQUESTED", "REVIEW_REQUIRED"}


@dataclass(frozen=True)
class CapabilityTaskContext:
    task_id: str
    user_id: str
    workspace_id: str | None
    source: str
    status: str
    active: bool
    execution_allowed: bool
    label: str = ""
    updated_at_ms: int = 0


class CapabilityTaskCoordinator:
    def __init__(self, *, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def bootstrap(self, *, user_id: str) -> CapabilityTaskContext:
        owner_id = user_id.strip()
        if not owner_id:
            raise CapabilityTaskNotFound("Capability OS task owner is required")
        now = int(time.time() * 1000)
        async with self._session_factory() as db:
            session = WorkbenchSession(
                user_id=owner_id,
                name="Capability OS MCP Session",
                workspace_id=None,
                status="OPEN",
                event_count=0,
                created_at=now,
                updated_at=now,
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)
            return CapabilityTaskContext(
                task_id=session.id,
                user_id=session.user_id,
                workspace_id=None,
                source="workbench",
                status="OPEN",
                active=True,
                execution_allowed=True,
                label=str(session.name or "Capability OS MCP Session"),
                updated_at_ms=int(session.updated_at or now),
            )

    async def fork_many(
        self, *, user_id: str, parent_task_id: str, count: int
    ) -> tuple[CapabilityTaskContext, ...]:
        """Atomically create isolated Capability OS child contexts for host-native fan-out."""
        parent = await self.require_executable(user_id=user_id, task_id=parent_task_id)
        if parent.source != "workbench":
            raise CapabilityTaskNotExecutable(
                "parallel subagent fan-out requires a workbench parent task"
            )
        bounded_count = int(count)
        if bounded_count < 2 or bounded_count > 10:
            raise ValueError("parallel subagent count must be between 2 and 10")
        now = int(time.time() * 1000)
        sessions = [
            WorkbenchSession(
                user_id=parent.user_id,
                name=f"Capability OS Subagent {index + 1:02d}",
                workspace_id=parent.workspace_id,
                status="OPEN",
                event_count=0,
                created_at=now,
                updated_at=now,
            )
            for index in range(bounded_count)
        ]
        async with self._session_factory() as db:
            db.add_all(sessions)
            await db.commit()
            for session in sessions:
                await db.refresh(session)
        return tuple(
            CapabilityTaskContext(
                task_id=session.id,
                user_id=session.user_id,
                workspace_id=session.workspace_id,
                source="workbench",
                status="OPEN",
                active=True,
                execution_allowed=True,
                label=str(session.name),
                updated_at_ms=int(session.updated_at or now),
            )
            for session in sessions
        )

    async def resolve(self, *, user_id: str, task_id: str) -> CapabilityTaskContext | None:
        if not user_id.strip() or not task_id.strip():
            return None
        async with self._session_factory() as db:
            workbench = await db.scalar(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == task_id,
                    WorkbenchSession.user_id == user_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            if workbench is not None:
                status = str(workbench.status or "").upper()
                active = status not in _WORKBENCH_TERMINAL
                return CapabilityTaskContext(
                    task_id=workbench.id,
                    user_id=workbench.user_id,
                    workspace_id=workbench.active_workspace_id or workbench.workspace_id,
                    source="workbench",
                    status=status,
                    active=active,
                    execution_allowed=active and status not in _WORKBENCH_WAITING,
                    label=str(workbench.name or "Workbench session"),
                    updated_at_ms=int(workbench.updated_at or 0),
                )

            factory = await db.scalar(
                select(FactoryRun).where(
                    FactoryRun.id == task_id,
                    FactoryRun.user_id == user_id,
                )
            )
            if factory is not None:
                status = str(factory.state or "").upper()
                active = status not in _FACTORY_TERMINAL
                return CapabilityTaskContext(
                    task_id=factory.id,
                    user_id=factory.user_id,
                    workspace_id=factory.workspace_id,
                    source="factory",
                    status=status,
                    active=active,
                    execution_allowed=active and status not in _FACTORY_WAITING,
                    label=str(factory.mission or "Factory run"),
                    updated_at_ms=int(factory.updated_at or 0),
                )

            control = await db.scalar(
                select(ControlTask).where(
                    ControlTask.id == task_id,
                    ControlTask.user_id == user_id,
                )
            )
            if control is not None:
                status = str(control.status or "").upper()
                active = status not in _CONTROL_TERMINAL
                return CapabilityTaskContext(
                    task_id=control.id,
                    user_id=control.user_id,
                    workspace_id=control.workspace_id,
                    source="control",
                    status=status,
                    active=active,
                    execution_allowed=active and status not in _CONTROL_WAITING,
                    label=str(control.prompt or "Control task"),
                    updated_at_ms=int(control.updated_at or 0),
                )
        return None

    async def list_recent(
        self, *, user_id: str, limit: int = 20
    ) -> tuple[CapabilityTaskContext, ...]:
        normalized_user = str(user_id or "").strip()
        bounded_limit = max(1, min(int(limit), 50))
        if not normalized_user:
            return ()
        query_limit = min(100, bounded_limit * 3)
        async with self._session_factory() as db:
            workbenches = (
                await db.scalars(
                    select(WorkbenchSession)
                    .where(
                        WorkbenchSession.user_id == normalized_user,
                        WorkbenchSession.deleted_at.is_(None),
                        WorkbenchSession.status.not_in(_WORKBENCH_TERMINAL),
                    )
                    .order_by(WorkbenchSession.updated_at.desc())
                    .limit(query_limit)
                )
            ).all()
            factories = (
                await db.scalars(
                    select(FactoryRun)
                    .where(
                        FactoryRun.user_id == normalized_user,
                        FactoryRun.state.not_in(_FACTORY_TERMINAL),
                    )
                    .order_by(FactoryRun.updated_at.desc())
                    .limit(query_limit)
                )
            ).all()
            controls = (
                await db.scalars(
                    select(ControlTask)
                    .where(
                        ControlTask.user_id == normalized_user,
                        ControlTask.status.not_in(_CONTROL_TERMINAL),
                    )
                    .order_by(ControlTask.updated_at.desc())
                    .limit(query_limit)
                )
            ).all()

        contexts = [
            CapabilityTaskContext(
                task_id=row.id,
                user_id=row.user_id,
                workspace_id=row.active_workspace_id or row.workspace_id,
                source="workbench",
                status=str(row.status or "").upper(),
                active=True,
                execution_allowed=str(row.status or "").upper() not in _WORKBENCH_WAITING,
                label=str(row.name or "Workbench session"),
                updated_at_ms=int(row.updated_at or 0),
            )
            for row in workbenches
        ]
        contexts.extend(
            CapabilityTaskContext(
                task_id=row.id,
                user_id=row.user_id,
                workspace_id=row.workspace_id,
                source="factory",
                status=str(row.state or "").upper(),
                active=True,
                execution_allowed=str(row.state or "").upper() not in _FACTORY_WAITING,
                label=str(row.mission or "Factory run"),
                updated_at_ms=int(row.updated_at or 0),
            )
            for row in factories
        )
        contexts.extend(
            CapabilityTaskContext(
                task_id=row.id,
                user_id=row.user_id,
                workspace_id=row.workspace_id,
                source="control",
                status=str(row.status or "").upper(),
                active=True,
                execution_allowed=str(row.status or "").upper() not in _CONTROL_WAITING,
                label=str(row.prompt or "Control task"),
                updated_at_ms=int(row.updated_at or 0),
            )
            for row in controls
        )
        contexts.sort(key=lambda item: (item.updated_at_ms, item.task_id), reverse=True)
        return tuple(contexts[:bounded_limit])

    async def require_active(self, *, user_id: str, task_id: str) -> CapabilityTaskContext:
        task = await self.resolve(user_id=user_id, task_id=task_id)
        if task is None or not task.active:
            raise CapabilityTaskNotFound("active Capability OS task not found")
        return task

    async def require_executable(self, *, user_id: str, task_id: str) -> CapabilityTaskContext:
        task = await self.require_active(user_id=user_id, task_id=task_id)
        if not task.execution_allowed:
            raise CapabilityTaskNotExecutable(
                f"task {task.task_id} is {task.status} and cannot start new side effects"
            )
        return task
