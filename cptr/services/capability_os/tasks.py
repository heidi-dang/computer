"""Owner-bound task identity adapter for Capability OS.

Capability OS deliberately reuses CPTR's existing durable Workbench, Factory,
and Control task records instead of creating a parallel task namespace.
"""

from __future__ import annotations

from dataclasses import dataclass

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


class CapabilityTaskCoordinator:
    def __init__(self, *, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

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
                )
        return None

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
