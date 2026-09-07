"""Durable zero-authority cleanup for Capability OS task lifecycles.

CPTR task systems remain the source of truth for task state. This module owns
only Capability OS authority attached to those task identities and is designed
to be safe to call repeatedly from terminal/waiting transitions and recovery.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import (
    CapabilityOsLease,
    CapabilityOsMcpMount,
    ControlTask,
    FactoryRun,
    WorkbenchSession,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.utils.db import get_session_factory

_WORKBENCH_ZERO_AUTHORITY = {
    "COMPLETE",
    "FAILED",
    "CANCELLED",
    "ARCHIVED",
    "WAITING_APPROVAL",
    "REVIEW_REQUIRED",
}
_FACTORY_ZERO_AUTHORITY = {
    "COMPLETE",
    "FAILED",
    "CANCELLED",
    "BLOCKED",
    "PAUSED",
    "APPROVAL_REQUIRED",
    "RECOVERING",
}
_CONTROL_ZERO_AUTHORITY = {
    "COMPLETE",
    "COMPLETE_WITH_TOOL_ERRORS",
    "FAILED",
    "CANCELLED",
    "REJECTED",
    "CANCEL_REQUESTED",
    "REVIEW_REQUIRED",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


async def revoke_task_authority(
    task_id: str,
    *,
    store: SqlCapabilityOsStore | None = None,
    now_ms: int | None = None,
) -> dict[str, int]:
    """Release every durable Capability OS authority object for one task.

    Evidence and artifact history are intentionally retained. MCP mounts are
    released before leases are revoked so both projections converge to an
    explicit zero-authority state. The operation is idempotent.
    """
    normalized = str(task_id).strip()
    if not normalized:
        raise ValueError("Capability OS lifecycle task id must not be blank")
    owned_store = store or SqlCapabilityOsStore()
    current = int(_now_ms() if now_ms is None else now_ms)
    released_mounts = await owned_store.release_task_mounts(normalized, now_ms=current)
    revoked_leases = await owned_store.revoke_task_leases(normalized, now_ms=current)
    return {
        "releasedMounts": int(released_mounts),
        "revokedLeases": int(revoked_leases),
    }


async def _task_requires_zero_authority(db, task_id: str) -> tuple[bool, str]:
    workbench = await db.scalar(
        select(WorkbenchSession).where(WorkbenchSession.id == task_id).limit(1)
    )
    if workbench is not None:
        status = str(workbench.status or "").upper()
        if workbench.deleted_at is not None:
            return True, "workbench-deleted"
        return status in _WORKBENCH_ZERO_AUTHORITY, f"workbench:{status or 'UNKNOWN'}"

    factory = await db.scalar(select(FactoryRun).where(FactoryRun.id == task_id).limit(1))
    if factory is not None:
        status = str(factory.state or "").upper()
        return status in _FACTORY_ZERO_AUTHORITY, f"factory:{status or 'UNKNOWN'}"

    control = await db.scalar(select(ControlTask).where(ControlTask.id == task_id).limit(1))
    if control is not None:
        status = str(control.status or "").upper()
        return status in _CONTROL_ZERO_AUTHORITY, f"control:{status or 'UNKNOWN'}"

    # Authority must never outlive the CPTR task identity that granted its
    # ownership boundary. Unknown task IDs are therefore treated as orphans.
    return True, "orphaned-task"


async def reconcile_inactive_task_authority(
    *,
    store: SqlCapabilityOsStore | None = None,
    session_factory: async_sessionmaker | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Repair stale authority after crashes/restarts and task-state races.

    Expired leases are retired individually. All authority for terminal,
    waiting, deleted, or orphaned tasks is revoked as an idempotent task-level
    cleanup. Active tasks with non-expired leases remain untouched.
    """
    current = int(_now_ms() if now_ms is None else now_ms)
    sessions = session_factory or (
        store._session_factory if store is not None else get_session_factory()
    )
    owned_store = store or SqlCapabilityOsStore(session_factory=sessions)

    async with sessions() as db:
        expired_leases = list(
            (
                await db.scalars(
                    select(CapabilityOsLease).where(
                        CapabilityOsLease.status == "active",
                        CapabilityOsLease.expires_at_ms <= current,
                    )
                )
            ).all()
        )
        candidate_task_ids = set(
            (
                await db.scalars(
                    select(CapabilityOsLease.task_id)
                    .where(CapabilityOsLease.status == "active")
                    .distinct()
                )
            ).all()
        )
        candidate_task_ids.update(
            (
                await db.scalars(
                    select(CapabilityOsMcpMount.task_id)
                    .where(CapabilityOsMcpMount.state == "mounted")
                    .distinct()
                )
            ).all()
        )
        inactive: list[tuple[str, str]] = []
        for task_id in sorted(str(item) for item in candidate_task_ids if str(item)):
            zero, reason = await _task_requires_zero_authority(db, task_id)
            if zero:
                inactive.append((task_id, reason))

    released_expired_mounts = 0
    revoked_expired_leases = 0
    for lease in expired_leases:
        async with sessions() as db:
            mount_ids = list(
                (
                    await db.scalars(
                        select(CapabilityOsMcpMount.mount_id).where(
                            CapabilityOsMcpMount.lease_id == lease.lease_id,
                            CapabilityOsMcpMount.state == "mounted",
                        )
                    )
                ).all()
            )
        for mount_id in mount_ids:
            if await owned_store.release_mcp_mount(str(mount_id), now_ms=current):
                released_expired_mounts += 1
        if await owned_store.revoke_lease(lease.lease_id, now_ms=current):
            revoked_expired_leases += 1

    released_task_mounts = 0
    revoked_task_leases = 0
    for task_id, _reason in inactive:
        result = await revoke_task_authority(task_id, store=owned_store, now_ms=current)
        released_task_mounts += result["releasedMounts"]
        revoked_task_leases += result["revokedLeases"]

    return {
        "inactiveTasks": len(inactive),
        "inactiveTaskReasons": {task_id: reason for task_id, reason in inactive},
        "releasedExpiredMounts": released_expired_mounts,
        "revokedExpiredLeases": revoked_expired_leases,
        "releasedTaskMounts": released_task_mounts,
        "revokedTaskLeases": revoked_task_leases,
    }
