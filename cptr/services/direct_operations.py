"""Durable lifecycle persistence for agent-free direct workspace operations.

This module deliberately does not invoke AgentService or the autonomous supervisor.
It stores the authoritative lifecycle for direct mutations and safe action requests.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError

from cptr.models import (
    AutonomousWorkspaceLease,
    DirectOperation,
    DirectOperationApproval,
    DirectOperationEvent,
    WorkspaceOperationLease,
)
from cptr.utils.db import get_db

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "ORPHANED"}
NONTERMINAL_STATES = {
    "REQUESTED",
    "WAITING_APPROVAL",
    "QUEUED",
    "DISPATCHING",
    "RUNNING",
    "CANCEL_REQUESTED",
    "RECOVERING",
}


class DirectOperationError(RuntimeError):
    """Base error with a stable public-safe code."""

    code = "DIRECT_OPERATION_ERROR"


class IdempotencyConflict(DirectOperationError):
    code = "IDEMPOTENCY_KEY_CONFLICT"


class WorkspaceBusy(DirectOperationError):
    code = "WORKSPACE_BUSY"


class InvalidTransition(DirectOperationError):
    code = "INVALID_OPERATION_STATE"


@dataclass(frozen=True)
class LeaseGrant:
    workspace_id: str
    holder_type: str
    holder_id: str
    fencing_token: int
    expires_at: int


def now_ms() -> int:
    return int(time.time() * 1000)


def canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def event_payload(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep only structured public-safe evidence in the lifecycle event stream."""
    return dict(payload or {})


class DirectOperationStore:
    """SQL-backed source of truth for direct-operation state and ownership."""

    async def create_or_replay(
        self,
        *,
        user_id: str,
        workspace_id: str,
        kind: str,
        request: dict[str, Any],
        idempotency_key: str,
        expected_revision: str | None,
    ) -> tuple[DirectOperation, bool]:
        digest = canonical_digest(request)
        now = now_ms()
        async with await get_db() as db:
            existing = await self._find_idempotency(
                db, user_id, workspace_id, kind, idempotency_key
            )
            if existing is not None:
                if existing.request_digest != digest:
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different direct operation"
                    )
                return existing, True

            operation = DirectOperation(
                user_id=user_id,
                workspace_id=workspace_id,
                kind=kind,
                state="REQUESTED",
                request=dict(request),
                request_digest=digest,
                idempotency_key=idempotency_key,
                expected_revision=expected_revision,
                created_at=now,
                updated_at=now,
                version=1,
            )
            db.add(operation)
            try:
                await db.flush()
                db.add(
                    DirectOperationEvent(
                        operation_id=operation.id,
                        event_type="CREATED",
                        state="REQUESTED",
                        payload=event_payload({"kind": kind}),
                        created_at=now,
                    )
                )
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = await self._find_idempotency(
                    db, user_id, workspace_id, kind, idempotency_key
                )
                if existing is None:
                    raise
                if existing.request_digest != digest:
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different direct operation"
                    )
                return existing, True
            await db.refresh(operation)
            return operation, False

    async def get(self, operation_id: str, user_id: str) -> DirectOperation | None:
        async with await get_db() as db:
            row = await db.get(DirectOperation, operation_id)
            if row is None or row.user_id != user_id:
                return None
            return row

    async def get_internal(self, operation_id: str) -> DirectOperation | None:
        """Executor-only lookup; public API access always uses ``get`` above."""
        async with await get_db() as db:
            return await db.get(DirectOperation, operation_id)

    async def list_events(
        self, operation_id: str, *, cursor: int = 0, limit: int = 100
    ) -> list[DirectOperationEvent]:
        async with await get_db() as db:
            result = await db.execute(
                select(DirectOperationEvent)
                .where(
                    DirectOperationEvent.operation_id == operation_id,
                    DirectOperationEvent.created_at >= cursor,
                )
                .order_by(DirectOperationEvent.created_at, DirectOperationEvent.id)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def transition(
        self,
        operation_id: str,
        *,
        expected_states: set[str],
        state: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        public_result: dict[str, Any] | None = None,
        public_error_code: str | None = None,
        cancel_reason: str | None = None,
        lease_fencing_token: int | None = None,
    ) -> DirectOperation | None:
        now = now_ms()
        values: dict[str, Any] = {
            "state": state,
            "updated_at": now,
            "version": DirectOperation.version + 1,
        }
        if state == "RUNNING":
            values["started_at"] = now
        if state in TERMINAL_STATES:
            values["finished_at"] = now
        if state == "CANCEL_REQUESTED":
            values["cancel_requested_at"] = now
        if public_result is not None:
            values["public_result"] = dict(public_result)
        if public_error_code is not None:
            values["public_error_code"] = public_error_code
        if cancel_reason is not None:
            values["cancel_reason"] = cancel_reason
        if lease_fencing_token is not None:
            values["lease_fencing_token"] = lease_fencing_token

        async with await get_db() as db:
            result = await db.execute(
                update(DirectOperation)
                .where(
                    DirectOperation.id == operation_id,
                    DirectOperation.state.in_(expected_states),
                )
                .values(**values)
            )
            if result.rowcount != 1:
                await db.rollback()
                return None
            db.add(
                DirectOperationEvent(
                    operation_id=operation_id,
                    event_type=event_type,
                    state=state,
                    payload=event_payload(payload),
                    created_at=now,
                )
            )
            await db.commit()
            return await db.get(DirectOperation, operation_id)

    async def request_cancel(
        self, operation_id: str, *, reason: str | None = None
    ) -> DirectOperation | None:
        operation = await self.transition(
            operation_id,
            expected_states={"REQUESTED", "WAITING_APPROVAL", "QUEUED", "DISPATCHING", "RUNNING"},
            state="CANCEL_REQUESTED",
            event_type="CANCEL_REQUESTED",
            payload={"reason": reason or "cancel requested"},
            cancel_reason=reason or "cancel requested",
        )
        if operation is not None:
            return operation
        async with await get_db() as db:
            return await db.get(DirectOperation, operation_id)

    async def complete_cancel(self, operation_id: str, *, detail: str = "cancelled") -> DirectOperation | None:
        return await self.transition(
            operation_id,
            expected_states={"CANCEL_REQUESTED"},
            state="CANCELLED",
            event_type="CANCELLED",
            payload={"detail": detail},
            public_result={"detail": detail},
        )

    async def create_approval(
        self,
        operation_id: str,
        *,
        request_digest: str,
        reason: str,
        expires_at: int | None = None,
    ) -> DirectOperationApproval:
        now = now_ms()
        async with await get_db() as db:
            existing = await db.execute(
                select(DirectOperationApproval).where(
                    DirectOperationApproval.operation_id == operation_id
                )
            )
            approval = existing.scalar_one_or_none()
            if approval is not None:
                return approval
            approval = DirectOperationApproval(
                operation_id=operation_id,
                request_digest=request_digest,
                reason=reason,
                status="PENDING",
                requested_at=now,
                expires_at=expires_at,
            )
            db.add(approval)
            await db.flush()
            await db.execute(
                update(DirectOperation)
                .where(
                    DirectOperation.id == operation_id,
                    DirectOperation.state == "REQUESTED",
                )
                .values(
                    state="WAITING_APPROVAL",
                    approval_id=approval.id,
                    updated_at=now,
                    version=DirectOperation.version + 1,
                )
            )
            db.add(
                DirectOperationEvent(
                    operation_id=operation_id,
                    event_type="APPROVAL_REQUIRED",
                    state="WAITING_APPROVAL",
                    payload={"approval_id": approval.id, "reason": reason},
                    created_at=now,
                )
            )
            await db.commit()
            await db.refresh(approval)
            return approval

    async def decide_approval(
        self, operation_id: str, *, approved: bool, decided_by: str
    ) -> DirectOperation | None:
        now = now_ms()
        async with await get_db() as db:
            result = await db.execute(
                select(DirectOperationApproval)
                .join(DirectOperation)
                .where(
                    DirectOperationApproval.operation_id == operation_id,
                    DirectOperation.state == "WAITING_APPROVAL",
                    DirectOperationApproval.status == "PENDING",
                )
            )
            approval = result.scalar_one_or_none()
            if approval is None:
                return None
            approval.status = "APPROVED" if approved else "REJECTED"
            approval.decided_at = now
            approval.decided_by = decided_by
            state = "QUEUED" if approved else "REJECTED"
            await db.execute(
                update(DirectOperation)
                .where(DirectOperation.id == operation_id)
                .values(
                    state=state,
                    updated_at=now,
                    finished_at=None if approved else now,
                    public_error_code=None if approved else "APPROVAL_REJECTED",
                    version=DirectOperation.version + 1,
                )
            )
            db.add(
                DirectOperationEvent(
                    operation_id=operation_id,
                    event_type="APPROVAL_DECIDED",
                    state=state,
                    payload={"approved": approved, "decided_by": decided_by},
                    created_at=now,
                )
            )
            await db.commit()
            return await db.get(DirectOperation, operation_id)

    async def acquire_workspace_lease(
        self,
        *,
        workspace_id: str,
        holder_type: str,
        holder_id: str,
        lease_ms: int = 60_000,
    ) -> LeaseGrant:
        now = now_ms()
        expires_at = now + lease_ms
        async with await get_db() as db:
            legacy_monitor_lease = await db.get(AutonomousWorkspaceLease, workspace_id)
            if (
                legacy_monitor_lease is not None
                and legacy_monitor_lease.expires_at >= now
                and not (
                    holder_type == "AUTONOMOUS_MONITOR"
                    and legacy_monitor_lease.monitor_id == holder_id
                )
            ):
                raise WorkspaceBusy("workspace mutation lease is held by an autonomous monitor")

            lease = await db.get(WorkspaceOperationLease, workspace_id)
            if lease is not None:
                held_by_other = lease.holder_id != holder_id or lease.holder_type != holder_type
                if held_by_other and lease.expires_at >= now:
                    raise WorkspaceBusy("workspace mutation lease is held by another operation")
                next_token = int(lease.fencing_token or 0) + 1
                lease.holder_type = holder_type
                lease.holder_id = holder_id
                lease.fencing_token = next_token
                lease.acquired_at = now
                lease.expires_at = expires_at
            else:
                lease = WorkspaceOperationLease(
                    workspace_id=workspace_id,
                    holder_type=holder_type,
                    holder_id=holder_id,
                    fencing_token=1,
                    acquired_at=now,
                    expires_at=expires_at,
                )
                db.add(lease)
                next_token = 1
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise WorkspaceBusy("workspace mutation lease could not be acquired") from None
            return LeaseGrant(workspace_id, holder_type, holder_id, next_token, expires_at)

    async def release_workspace_lease(
        self, *, workspace_id: str, holder_type: str, holder_id: str, fencing_token: int
    ) -> None:
        async with await get_db() as db:
            await db.execute(
                update(WorkspaceOperationLease)
                .where(
                    and_(
                        WorkspaceOperationLease.workspace_id == workspace_id,
                        WorkspaceOperationLease.holder_type == holder_type,
                        WorkspaceOperationLease.holder_id == holder_id,
                        WorkspaceOperationLease.fencing_token == fencing_token,
                    )
                )
                .values(expires_at=0)
            )
            await db.commit()

    async def reconcile_after_restart(self) -> int:
        """Never infer success after losing in-memory executor state.

        File mutations are executed inline. Any nonterminal operation left by a process
        restart is therefore marked ORPHANED until a future executor can reconcile it.
        """
        now = now_ms()
        async with await get_db() as db:
            result = await db.execute(
                update(DirectOperation)
                .where(DirectOperation.state.in_(NONTERMINAL_STATES))
                .values(
                    state="ORPHANED",
                    public_error_code="RECOVERY_REQUIRED",
                    updated_at=now,
                    finished_at=now,
                    version=DirectOperation.version + 1,
                )
            )
            if result.rowcount:
                rows = await db.execute(
                    select(DirectOperation.id).where(
                        DirectOperation.state == "ORPHANED",
                        DirectOperation.updated_at == now,
                    )
                )
                for (operation_id,) in rows.all():
                    db.add(
                        DirectOperationEvent(
                            operation_id=operation_id,
                            event_type="RECOVERY_ORPHANED",
                            state="ORPHANED",
                            payload={"reason": "service restart requires explicit recovery"},
                            created_at=now,
                        )
                    )
            await db.commit()
            return result.rowcount or 0

    @staticmethod
    async def _find_idempotency(db, user_id: str, workspace_id: str, kind: str, key: str):
        result = await db.execute(
            select(DirectOperation).where(
                DirectOperation.user_id == user_id,
                DirectOperation.workspace_id == workspace_id,
                DirectOperation.kind == kind,
                DirectOperation.idempotency_key == key,
            )
        )
        return result.scalar_one_or_none()
