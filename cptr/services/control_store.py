"""SQLAlchemy persistence adapters for control tasks and monitors."""

from __future__ import annotations

import secrets
import time
from typing import Any

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from cptr.models import (
    AutonomousApproval,
    AutonomousEvidence,
    AutonomousMonitor,
    AutonomousScope,
    AutonomousWorkspaceLease,
    ChatMessage,
    ControlIdempotency,
    ControlMessage,
    ControlTask,
    WorkspaceOperationLease,
)
from cptr.services.supervisor import (
    ApprovalRecord,
    EvidenceRecord,
    MonitorState,
    MonitorStatus,
    ScopeRecord,
    ScopeStatus,
)
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_sensitive


def _now_ms() -> int:
    return int(time.time() * 1000)


class SqlSupervisorStore:
    """Persist monitor state and claim leases with atomic SQLite updates."""

    async def create_monitor(
        self, monitor: MonitorState, idempotency_key: str | None
    ) -> MonitorState:
        async with await get_db() as db:
            if idempotency_key:
                existing_key = await db.execute(
                    select(ControlIdempotency).where(
                        ControlIdempotency.user_id == monitor.user_id,
                        ControlIdempotency.key == idempotency_key,
                    )
                )
                record = existing_key.scalar_one_or_none()
                if record:
                    existing = await db.get(AutonomousMonitor, record.resource_id)
                    if existing:
                        return await self._load_from_session(db, existing.id)

            now = _now_ms()
            db.add(
                AutonomousMonitor(
                    id=monitor.monitor_id,
                    goal_id=monitor.goal_id,
                    user_id=monitor.user_id,
                    workspace_id=monitor.workspace_id,
                    original_goal=monitor.original_goal,
                    original_acceptance_criteria=monitor.original_acceptance_criteria,
                    model_id=monitor.model_id,
                    status=monitor.status.value,
                    current_scope_id=monitor.current_scope_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            for ordinal, scope in enumerate(monitor.scopes):
                db.add(self._scope_model(monitor.monitor_id, ordinal, scope, now))
            if idempotency_key:
                db.add(
                    ControlIdempotency(
                        user_id=monitor.user_id,
                        key=idempotency_key,
                        resource_type="autonomous_monitor",
                        resource_id=monitor.monitor_id,
                        response={"monitor_id": monitor.monitor_id},
                        created_at=now,
                    )
                )
            await db.commit()
        return monitor

    async def get_monitor(self, monitor_id: str) -> MonitorState | None:
        async with await get_db() as db:
            return await self._load_from_session(db, monitor_id)

    async def save_monitor(self, monitor: MonitorState) -> None:
        now = _now_ms()
        async with await get_db() as db:
            row = await db.get(AutonomousMonitor, monitor.monitor_id)
            if row is None:
                raise KeyError(f"monitor not found: {monitor.monitor_id}")
            terminal = {
                MonitorStatus.COMPLETE.value,
                MonitorStatus.CANCELLED.value,
                MonitorStatus.BLOCKED.value,
                MonitorStatus.FAILED.value,
                MonitorStatus.CANCEL_REQUESTED.value,
            }
            releasable_terminal = terminal - {MonitorStatus.CANCEL_REQUESTED.value}
            if row.status in terminal and row.status != monitor.status.value:
                await db.rollback()
                return
            row.status = monitor.status.value
            row.current_scope_id = monitor.current_scope_id
            row.approval_id = monitor.approval_id
            row.approved_operations = list(monitor.approved_operations)
            row.director_state = dict(monitor.director_state)
            row.updated_at = now
            result = await db.execute(
                select(AutonomousScope).where(AutonomousScope.monitor_id == monitor.monitor_id)
            )
            stored_scopes = {scope.id: scope for scope in result.scalars().all()}
            for scope in monitor.scopes:
                target = stored_scopes.get(scope.scope_id)
                if target is None:
                    continue
                target.status = scope.status.value
                target.attempt_count = scope.attempt_count
                target.worker_task_ids = list(scope.worker_task_ids)
                target.steering_requests = list(scope.steering_requests)
                target.verification_evidence = list(scope.verification_evidence)
                target.failure_evidence = list(scope.failure_evidence)
                target.failure_signature_counts = dict(scope.failure_signature_counts)
                target.last_decision = dict(scope.last_decision)
                target.next_action = scope.next_action
                target.history = [item.value for item in scope.history]
                target.updated_at = scope.updated_at
            if monitor.status in releasable_terminal:
                await db.execute(
                    delete(AutonomousWorkspaceLease).where(
                        AutonomousWorkspaceLease.monitor_id == monitor.monitor_id
                    )
                )
            await db.commit()

    async def request_cancel_monitor(self, monitor_id: str) -> bool:
        async with await get_db() as db:
            result = await db.execute(
                update(AutonomousMonitor)
                .where(
                    AutonomousMonitor.id == monitor_id,
                    AutonomousMonitor.status.not_in(
                        [
                            MonitorStatus.COMPLETE.value,
                            MonitorStatus.CANCELLED.value,
                            MonitorStatus.BLOCKED.value,
                            MonitorStatus.FAILED.value,
                            MonitorStatus.CANCEL_REQUESTED.value,
                        ]
                    ),
                )
                .values(status=MonitorStatus.CANCEL_REQUESTED.value, updated_at=_now_ms())
            )
            await db.commit()
            return result.rowcount == 1

    async def finalize_cancel_monitor(self, monitor_id: str) -> bool:
        async with await get_db() as db:
            result = await db.execute(
                update(AutonomousMonitor)
                .where(
                    AutonomousMonitor.id == monitor_id,
                    AutonomousMonitor.status == MonitorStatus.CANCEL_REQUESTED.value,
                )
                .values(status=MonitorStatus.CANCELLED.value, updated_at=_now_ms())
            )
            if result.rowcount == 1:
                scope_result = await db.execute(
                    select(AutonomousScope).where(AutonomousScope.monitor_id == monitor_id)
                )
                for scope in scope_result.scalars().all():
                    if scope.status in {
                        ScopeStatus.VERIFIED.value,
                        ScopeStatus.CANCELLED.value,
                    }:
                        continue
                    history = list(scope.history or [])
                    if not history or history[-1] != ScopeStatus.CANCELLED.value:
                        history.append(ScopeStatus.CANCELLED.value)
                    scope.status = ScopeStatus.CANCELLED.value
                    scope.history = history
                    scope.next_action = None
                    scope.updated_at = _now_ms()
                await db.execute(
                    delete(AutonomousWorkspaceLease).where(
                        AutonomousWorkspaceLease.monitor_id == monitor_id
                    )
                )
            await db.commit()
            return result.rowcount == 1

    async def block_cancel_monitor(self, monitor_id: str) -> bool:
        async with await get_db() as db:
            result = await db.execute(
                update(AutonomousMonitor)
                .where(
                    AutonomousMonitor.id == monitor_id,
                    AutonomousMonitor.status == MonitorStatus.CANCEL_REQUESTED.value,
                )
                .values(status=MonitorStatus.BLOCKED.value, updated_at=_now_ms())
            )
            if result.rowcount == 1:
                scope_result = await db.execute(
                    select(AutonomousScope).where(AutonomousScope.monitor_id == monitor_id)
                )
                for scope in scope_result.scalars().all():
                    if scope.status in {
                        ScopeStatus.VERIFIED.value,
                        ScopeStatus.CANCELLED.value,
                    }:
                        continue
                    history = list(scope.history or [])
                    if not history or history[-1] != ScopeStatus.BLOCKED.value:
                        history.append(ScopeStatus.BLOCKED.value)
                    scope.status = ScopeStatus.BLOCKED.value
                    scope.history = history
                    scope.next_action = (
                        "Owned execution did not quiesce within the cancellation bound."
                    )
                    scope.updated_at = _now_ms()
            await db.commit()
            return result.rowcount == 1

    async def cancel_monitor(self, monitor_id: str) -> bool:
        if not await self.request_cancel_monitor(monitor_id):
            return False
        return await self.finalize_cancel_monitor(monitor_id)

    async def claim_monitor(self, monitor_id: str) -> bool:
        now = _now_ms()
        token = secrets.token_urlsafe(18)
        async with await get_db() as db:
            result = await db.execute(
                update(AutonomousMonitor)
                .where(
                    AutonomousMonitor.id == monitor_id,
                    or_(
                        AutonomousMonitor.lock_expires_at.is_(None),
                        AutonomousMonitor.lock_expires_at < now,
                    ),
                )
                .values(lock_token=token, lock_expires_at=now + 300_000)
            )
            await db.commit()
            return result.rowcount == 1

    async def release_monitor(self, monitor_id: str) -> None:
        async with await get_db() as db:
            await db.execute(
                update(AutonomousMonitor)
                .where(AutonomousMonitor.id == monitor_id)
                .values(lock_token=None, lock_expires_at=None)
            )
            await db.commit()

    async def append_evidence(
        self, monitor_id: str, scope_id: str | None, kind: str, payload: dict[str, Any]
    ) -> EvidenceRecord:
        row = AutonomousEvidence(
            monitor_id=monitor_id,
            scope_id=scope_id,
            kind=kind,
            payload=redact_sensitive(payload),
            created_at=_now_ms(),
        )
        async with await get_db() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return EvidenceRecord(
            evidence_id=row.id,
            monitor_id=row.monitor_id,
            scope_id=row.scope_id,
            kind=row.kind,
            payload=redact_sensitive(row.payload or {}),
            created_at=row.created_at,
        )

    async def list_evidence(self, monitor_id: str) -> list[EvidenceRecord]:
        async with await get_db() as db:
            result = await db.execute(
                select(AutonomousEvidence)
                .where(AutonomousEvidence.monitor_id == monitor_id)
                .order_by(AutonomousEvidence.created_at, AutonomousEvidence.id)
            )
            return [
                EvidenceRecord(
                    evidence_id=item.id,
                    monitor_id=item.monitor_id,
                    scope_id=item.scope_id,
                    kind=item.kind,
                    payload=redact_sensitive(item.payload or {}),
                    created_at=item.created_at,
                )
                for item in result.scalars().all()
            ]

    async def get_message(self, message_id: str) -> ControlMessage | None:
        """Read a durable control message for autonomous provenance checks."""
        async with await get_db() as db:
            return await db.get(ControlMessage, message_id)

    async def create_approval(self, monitor_id: str, operation: str, reason: str) -> ApprovalRecord:
        now = _now_ms()
        row = AutonomousApproval(
            monitor_id=monitor_id,
            operation=operation,
            reason=reason,
            status="PENDING",
            requested_at=now,
        )
        async with await get_db() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return ApprovalRecord(
            approval_id=row.id,
            monitor_id=row.monitor_id,
            operation=row.operation,
            reason=row.reason,
            status=row.status,
            requested_at=row.requested_at,
        )

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        async with await get_db() as db:
            row = await db.get(AutonomousApproval, approval_id)
            if row is None:
                return None
            return ApprovalRecord(
                approval_id=row.id,
                monitor_id=row.monitor_id,
                operation=row.operation,
                reason=row.reason,
                status=row.status,
                requested_at=row.requested_at,
                decided_at=row.decided_at,
                decided_by=row.decided_by,
            )

    async def decide_approval(
        self, approval_id: str, *, status: str, decided_by: str
    ) -> ApprovalRecord:
        async with await get_db() as db:
            row = await db.get(AutonomousApproval, approval_id)
            if row is None or row.status != "PENDING":
                raise KeyError("approval is no longer pending")
            row.status = status
            row.decided_at = _now_ms()
            row.decided_by = decided_by
            await db.commit()
            return ApprovalRecord(
                approval_id=row.id,
                monitor_id=row.monitor_id,
                operation=row.operation,
                reason=row.reason,
                status=row.status,
                requested_at=row.requested_at,
                decided_at=row.decided_at,
                decided_by=row.decided_by,
            )

    async def claim_workspace(self, workspace_id: str, monitor_id: str) -> bool:
        """Claim the generalized workspace lease for an autonomous monitor.

        New monitors coordinate with direct operations through
        ``workspace_operation_leases``. The legacy table is cleared only after
        the generalized lease is committed, which keeps existing deployments
        safe while active legacy monitor leases age out.
        """
        now = _now_ms()
        expires_at = now + 300_000
        async with await get_db() as db:
            legacy = await db.get(AutonomousWorkspaceLease, workspace_id)
            if legacy is not None and legacy.monitor_id != monitor_id:
                owner = await db.get(AutonomousMonitor, legacy.monitor_id)
                terminal_owner = owner is None or owner.status in {
                    MonitorStatus.COMPLETE.value,
                    MonitorStatus.CANCELLED.value,
                    MonitorStatus.BLOCKED.value,
                    MonitorStatus.FAILED.value,
                }
                if not terminal_owner:
                    return False
                await db.delete(legacy)
                await db.flush()

            lease = await db.get(WorkspaceOperationLease, workspace_id)
            if lease is not None:
                held_by_other = (
                    lease.holder_type != "AUTONOMOUS_MONITOR" or lease.holder_id != monitor_id
                )
                if held_by_other and lease.expires_at >= now:
                    return False
                lease.holder_type = "AUTONOMOUS_MONITOR"
                lease.holder_id = monitor_id
                lease.fencing_token = int(lease.fencing_token or 0) + 1
                lease.acquired_at = now
                lease.expires_at = expires_at
            else:
                db.add(
                    WorkspaceOperationLease(
                        workspace_id=workspace_id,
                        holder_type="AUTONOMOUS_MONITOR",
                        holder_id=monitor_id,
                        fencing_token=1,
                        acquired_at=now,
                        expires_at=expires_at,
                    )
                )
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                return False
            if legacy is not None and legacy.monitor_id == monitor_id:
                legacy.expires_at = 0
            await db.commit()
            return True

    async def release_workspace(self, workspace_id: str, monitor_id: str) -> None:
        async with await get_db() as db:
            await db.execute(
                update(WorkspaceOperationLease)
                .where(
                    and_(
                        WorkspaceOperationLease.workspace_id == workspace_id,
                        WorkspaceOperationLease.holder_type == "AUTONOMOUS_MONITOR",
                        WorkspaceOperationLease.holder_id == monitor_id,
                    )
                )
                .values(expires_at=0)
            )
            await db.execute(
                update(AutonomousWorkspaceLease)
                .where(
                    and_(
                        AutonomousWorkspaceLease.workspace_id == workspace_id,
                        AutonomousWorkspaceLease.monitor_id == monitor_id,
                    )
                )
                .values(expires_at=0)
            )
            await db.commit()

    async def list_active(self) -> list[MonitorState]:
        async with await get_db() as db:
            result = await db.execute(
                select(AutonomousMonitor).where(
                    AutonomousMonitor.status.in_(
                        [MonitorStatus.RUNNING.value, MonitorStatus.APPROVAL_REQUIRED.value]
                    )
                )
            )
            rows = list(result.scalars().all())
            output = []
            for row in rows:
                output.append(await self._load_from_session(db, row.id))
            return [item for item in output if item is not None]

    async def _load_from_session(self, db, monitor_id: str) -> MonitorState | None:
        row = await db.get(AutonomousMonitor, monitor_id)
        if row is None:
            return None
        result = await db.execute(
            select(AutonomousScope)
            .where(AutonomousScope.monitor_id == monitor_id)
            .order_by(AutonomousScope.ordinal)
        )
        scopes = [self._scope_record(scope) for scope in result.scalars().all()]
        return MonitorState(
            monitor_id=row.id,
            goal_id=row.goal_id,
            user_id=row.user_id,
            workspace_id=row.workspace_id,
            original_goal=row.original_goal,
            original_acceptance_criteria=list(row.original_acceptance_criteria or []),
            model_id=row.model_id,
            scopes=scopes,
            status=MonitorStatus(row.status),
            current_scope_id=row.current_scope_id,
            approval_id=row.approval_id,
            approved_operations=list(row.approved_operations or []),
            director_state=dict(row.director_state or {}),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _scope_model(
        monitor_id: str, ordinal: int, scope: ScopeRecord, now: int
    ) -> AutonomousScope:
        return AutonomousScope(
            id=scope.scope_id,
            monitor_id=monitor_id,
            ordinal=ordinal,
            title=scope.title,
            description=scope.description,
            acceptance_criteria=list(scope.acceptance_criteria),
            status=scope.status.value,
            attempt_count=scope.attempt_count,
            worker_task_ids=list(scope.worker_task_ids),
            steering_requests=list(scope.steering_requests),
            verification_evidence=list(scope.verification_evidence),
            failure_evidence=list(scope.failure_evidence),
            failure_signature_counts=dict(scope.failure_signature_counts),
            last_decision=dict(scope.last_decision),
            next_action=scope.next_action,
            history=[item.value for item in scope.history],
            created_at=now,
            updated_at=scope.updated_at,
        )

    @staticmethod
    def _scope_record(row: AutonomousScope) -> ScopeRecord:
        record = ScopeRecord(
            scope_id=row.id,
            title=row.title,
            description=row.description,
            acceptance_criteria=list(row.acceptance_criteria or []),
            status=ScopeStatus(row.status),
            attempt_count=row.attempt_count,
            worker_task_ids=list(row.worker_task_ids or []),
            steering_requests=list(getattr(row, "steering_requests", None) or []),
            verification_evidence=list(row.verification_evidence or []),
            failure_evidence=list(row.failure_evidence or []),
            failure_signature_counts=dict(row.failure_signature_counts or {}),
            last_decision=dict(row.last_decision or {}),
            next_action=row.next_action,
            history=[ScopeStatus(item) for item in (row.history or [])],
            updated_at=row.updated_at,
        )
        return record


class ControlTaskStore:
    async def get(self, task_id: str) -> ControlTask | None:
        async with await get_db() as db:
            return await db.get(ControlTask, task_id)

    async def by_idempotency(self, user_id: str, key: str) -> ControlTask | None:
        async with await get_db() as db:
            result = await db.execute(
                select(ControlTask).where(
                    ControlTask.user_id == user_id,
                    ControlTask.idempotency_key == key,
                )
            )
            return result.scalar_one_or_none()

    async def create(self, task: ControlTask) -> ControlTask:
        async with await get_db() as db:
            db.add(task)
            await db.commit()
            await db.refresh(task)
            return task

    async def update(self, task_id: str, **values: Any) -> None:
        if not values:
            return
        async with await get_db() as db:
            await db.execute(update(ControlTask).where(ControlTask.id == task_id).values(**values))
            await db.commit()

    async def transition_terminal(
        self,
        task_id: str,
        *,
        status: str,
        updated_at: int,
        error: str | None = None,
        cancelled_at: int | None = None,
    ) -> bool:
        """Atomically claim the one durable terminal transition for a task."""
        values: dict[str, Any] = {"status": status, "updated_at": updated_at}
        if error is not None:
            values["error"] = error
        if cancelled_at is not None:
            values["cancelled_at"] = cancelled_at
        async with await get_db() as db:
            result = await db.execute(
                update(ControlTask)
                .where(
                    ControlTask.id == task_id,
                    ControlTask.status.not_in(
                        (
                            "COMPLETE",
                            "COMPLETE_WITH_TOOL_ERRORS",
                            "FAILED",
                            "CANCELLED",
                            "CANCEL_REQUESTED",
                            "REVIEW_REQUIRED",
                            "REJECTED",
                        )
                    ),
                )
                .values(**values)
            )
            await db.commit()
            return result.rowcount == 1

    async def refine_complete_with_tool_errors(self, task_id: str, *, updated_at: int) -> bool:
        """Atomically refine a persisted COMPLETE task when durable tool evidence contradicts clean success."""
        async with await get_db() as db:
            result = await db.execute(
                update(ControlTask)
                .where(
                    ControlTask.id == task_id,
                    ControlTask.status == "COMPLETE",
                )
                .values(status="COMPLETE_WITH_TOOL_ERRORS", updated_at=updated_at)
            )
            await db.commit()
            return result.rowcount == 1

    async def request_review(
        self,
        task_id: str,
        *,
        summary: dict[str, Any],
        ready_at: int,
    ) -> bool:
        """Atomically pause a successful worker turn for user diff review."""
        async with await get_db() as db:
            result = await db.execute(
                update(ControlTask)
                .where(
                    ControlTask.id == task_id,
                    ControlTask.status.not_in(
                        (
                            "COMPLETE",
                            "COMPLETE_WITH_TOOL_ERRORS",
                            "FAILED",
                            "CANCELLED",
                            "CANCEL_REQUESTED",
                            "REJECTED",
                        )
                    ),
                )
                .values(
                    status="REVIEW_REQUIRED",
                    review_status="REQUIRED",
                    review_summary=summary,
                    review_decision=None,
                    review_ready_at=ready_at,
                    reviewed_at=None,
                    updated_at=ready_at,
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def decide_review(
        self,
        task_id: str,
        *,
        decision: str,
        note: str | None,
        decided_at: int,
    ) -> bool:
        """Record an accept or reject decision exactly once."""
        decision_value = decision.upper()
        status = "COMPLETE" if decision_value == "ACCEPT" else "REJECTED"
        review_status = "ACCEPTED" if decision_value == "ACCEPT" else "REJECTED"
        async with await get_db() as db:
            result = await db.execute(
                update(ControlTask)
                .where(
                    ControlTask.id == task_id,
                    ControlTask.status == "REVIEW_REQUIRED",
                    ControlTask.review_status == "REQUIRED",
                )
                .values(
                    status=status,
                    review_status=review_status,
                    review_decision={"decision": decision_value, "note": note, "decided_at": decided_at},
                    reviewed_at=decided_at,
                    updated_at=decided_at,
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def record_changes_requested(
        self,
        task_id: str,
        *,
        note: str,
        decided_at: int,
    ) -> bool:
        """Keep durable evidence for a review-driven scoped follow-up."""
        async with await get_db() as db:
            result = await db.execute(
                update(ControlTask)
                .where(
                    ControlTask.id == task_id,
                    ControlTask.review_status == "REQUIRED",
                    ControlTask.status.in_(("REVIEW_REQUIRED", "RUNNING")),
                )
                .values(
                    review_status="CHANGES_REQUESTED",
                    review_decision={
                        "decision": "REQUEST_CHANGES",
                        "note": note,
                        "decided_at": decided_at,
                    },
                    reviewed_at=decided_at,
                    updated_at=decided_at,
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def request_cancel(self, task_id: str, *, requested_at: int) -> bool:
        """Atomically establish cancellation intent before stopping execution."""
        async with await get_db() as db:
            result = await db.execute(
                update(ControlTask)
                .where(
                    ControlTask.id == task_id,
                    ControlTask.status.not_in(
                        (
                            "COMPLETE",
                            "COMPLETE_WITH_TOOL_ERRORS",
                            "FAILED",
                            "CANCELLED",
                            "CANCEL_REQUESTED",
                            "REJECTED",
                        )
                    ),
                )
                .values(status="CANCEL_REQUESTED", updated_at=requested_at)
            )
            await db.commit()
            return result.rowcount == 1

    async def finalize_cancel(self, task_id: str, *, cancelled_at: int, updated_at: int) -> bool:
        """Commit cancellation only after owned execution is quiescent."""
        async with await get_db() as db:
            result = await db.execute(
                update(ControlTask)
                .where(
                    ControlTask.id == task_id,
                    ControlTask.status == "CANCEL_REQUESTED",
                )
                .values(
                    status="CANCELLED",
                    cancelled_at=cancelled_at,
                    updated_at=updated_at,
                    error="cancelled",
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def invalidate_messages_for_task(self, task_id: str, *, now: int) -> int:
        """Invalidate queued steering before a cancelled task can drain it."""
        async with await get_db() as db:
            result = await db.execute(
                select(ControlMessage).where(
                    ControlMessage.task_id == task_id,
                    ControlMessage.status.not_in(("CONSUMED", "CANCELLED")),
                )
            )
            messages = list(result.scalars().all())
            for message in messages:
                message.status = "CANCELLED"
                message.updated_at = now
                if message.chat_message_id:
                    chat_message = await db.get(ChatMessage, message.chat_message_id)
                    if chat_message is not None:
                        meta = dict(chat_message.meta or {})
                        meta["queued"] = False
                        meta["delivery_status"] = "CANCELLED"
                        await db.execute(
                            update(ChatMessage)
                            .where(ChatMessage.id == chat_message.id)
                            .values(meta=meta)
                        )
            await db.commit()
            return len(messages)

    async def has_pending_control(self, task_id: str, *, user_id: str | None = None) -> bool:
        """Return whether a task still owns an undelivered control message.

        A control interruption briefly has no in-memory worker while the next
        generation is being installed.  This durable check prevents restart
        reconciliation from turning that intentional gap into a terminal
        failure.
        """
        async with await get_db() as db:
            conditions = [
                ControlMessage.task_id == task_id,
                ControlMessage.status.in_(("QUEUED", "DELIVERED")),
            ]
            if user_id is not None:
                conditions.append(ControlMessage.user_id == user_id)
            result = await db.execute(select(ControlMessage.id).where(and_(*conditions)).limit(1))
            return result.scalar_one_or_none() is not None

    async def mark_setup_ready_for_message(self, message_id: str, *, now: int) -> int:
        """Record that the owning worker crossed its first completed tool boundary."""
        async with await get_db() as db:
            task_ids = select(ControlTask.id).where(ControlTask.message_id == message_id)
            result = await db.execute(
                update(ControlMessage)
                .where(
                    ControlMessage.task_id.in_(task_ids),
                    ControlMessage.status.in_({"QUEUED", "DELIVERED"}),
                    ControlMessage.setup_readiness_status.is_(None),
                )
                .values(setup_readiness_status="READY", updated_at=now)
            )
            await db.commit()
            return int(result.rowcount or 0)

    async def enqueue_message(
        self,
        *,
        task_id: str,
        user_id: str,
        chat_id: str,
        content: str,
        dedupe_key: str,
        chat_message_id: str | None,
        now: int,
        monitor_id: str | None = None,
        scope_id: str | None = None,
        intended_message_id: str | None = None,
    ) -> ControlMessage:
        """Create or return one durable follow-up message by its retry key."""
        async with await get_db() as db:
            result = await db.execute(
                select(ControlMessage).where(
                    ControlMessage.user_id == user_id,
                    ControlMessage.task_id == task_id,
                    ControlMessage.dedupe_key == dedupe_key,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                return existing
            message = ControlMessage(
                user_id=user_id,
                task_id=task_id,
                chat_id=chat_id,
                chat_message_id=chat_message_id,
                content=content,
                dedupe_key=dedupe_key,
                status="QUEUED",
                monitor_id=monitor_id,
                scope_id=scope_id,
                intended_message_id=intended_message_id,
                effect_status="PENDING_DELIVERY",
                effect_evidence=[],
                created_at=now,
                updated_at=now,
            )
            db.add(message)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                result = await db.execute(
                    select(ControlMessage).where(
                        ControlMessage.user_id == user_id,
                        ControlMessage.task_id == task_id,
                        ControlMessage.dedupe_key == dedupe_key,
                    )
                )
                existing = result.scalar_one_or_none()
                if existing is None:
                    raise
                return existing
            await db.refresh(message)
            return message

    async def update_message(self, message_id: str, **values: Any) -> bool:
        if not values:
            return False
        async with await get_db() as db:
            statement = update(ControlMessage).where(ControlMessage.id == message_id)
            if values.get("status") not in {None, "CANCELLED"}:
                statement = statement.where(ControlMessage.status != "CANCELLED")
            if values.get("status") == "DELIVERED":
                statement = statement.where(ControlMessage.status == "QUEUED")
            elif values.get("status") == "CONSUMED":
                statement = statement.where(ControlMessage.status == "DELIVERED")
            result = await db.execute(statement.values(**values))
            await db.commit()
            return result.rowcount == 1

    async def consume_message(
        self,
        message_id: str,
        *,
        task_id: str,
        message_id_for_run: str,
        now: int,
    ) -> bool:
        """Atomically record one consumption and preserve cancellation races."""
        async with await get_db() as db:
            result = await db.execute(
                update(ControlMessage)
                .where(
                    ControlMessage.id == message_id,
                    ControlMessage.status == "DELIVERED",
                    ControlMessage.task_id == task_id,
                    or_(
                        ControlMessage.target_message_id.is_(None),
                        ControlMessage.target_message_id == message_id_for_run,
                    ),
                )
                .values(
                    status="CONSUMED",
                    consumed_at=now,
                    consumed_task_id=task_id,
                    consumed_message_id=message_id_for_run,
                    effect_status="PENDING_EFFECT",
                    effect_evidence=[
                        {
                            "kind": "continuation_started",
                            "message_id": message_id_for_run,
                            "observed_at": now,
                        }
                    ],
                    updated_at=now,
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def finalize_message_effect(
        self,
        message_id: str,
        *,
        task_id: str,
        continuation_message_id: str,
        effect_status: str,
        effect_evidence: list[dict[str, Any]],
        now: int,
    ) -> bool:
        """Record a fail-closed outcome for one consumed task control message.

        Normal-task delivery and consumption prove only that a continuation was
        started. Without target-bound effect verification, a terminal
        continuation is explicitly recorded as not observed rather than
        reported as an applied steering instruction.
        """
        if effect_status not in {"EFFECT_NOT_OBSERVED", "DELIVERY_FAILED"}:
            raise ValueError("invalid task steering effect status")
        async with await get_db() as db:
            result = await db.execute(
                update(ControlMessage)
                .where(
                    ControlMessage.id == message_id,
                    ControlMessage.task_id == task_id,
                    ControlMessage.status == "CONSUMED",
                    ControlMessage.consumed_message_id == continuation_message_id,
                    ControlMessage.effect_status == "PENDING_EFFECT",
                )
                .values(
                    effect_status=effect_status,
                    effect_evidence=effect_evidence,
                    effect_observed_at=now,
                    updated_at=now,
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def get_message(self, message_id: str) -> ControlMessage | None:
        async with await get_db() as db:
            return await db.get(ControlMessage, message_id)

    async def repoint_task_message(self, task_id: str, message_id: str, *, now: int) -> bool:
        async with await get_db() as db:
            result = await db.execute(
                update(ControlTask)
                .where(ControlTask.id == task_id, ControlTask.status.not_in(("CANCELLED", "REJECTED")))
                .values(message_id=message_id, status="RUNNING", updated_at=now)
            )
            await db.commit()
            return result.rowcount == 1
