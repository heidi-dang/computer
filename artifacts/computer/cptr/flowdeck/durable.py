"""Durable FlowDeck lifecycle and recovery primitives.

This module deliberately owns state, ordering, and fencing only. CPTR remains
the sole owner of model/tool execution; no method here invokes an adapter.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cptr.models.flowdeck import (
    FlowDeckEvent,
    FlowDeckLogicalOperation,
    FlowDeckPhysicalAttempt,
    FlowDeckRecoveryLease,
    FlowDeckRun,
    FlowDeckStep,
    FlowDeckWorkspaceLease,
)


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    ORPHANED = "ORPHANED"
    RECOVERING = "RECOVERING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


class OperationStatus(str, Enum):
    INTENT_RECORDED = "INTENT_RECORDED"
    RUNNING = "RUNNING"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


class AttemptStatus(str, Enum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class LifecycleError(RuntimeError):
    """Raised when a durable state transition is not safe."""


class DuplicateRequestError(LifecycleError):
    """Raised when the same key is reused with incompatible request data."""


class LeaseUnavailableError(LifecycleError):
    """Raised when an exclusive workspace/recovery lease is unavailable."""


class StaleWriterError(LifecycleError):
    """Raised when a writer presents an expired or fenced lease epoch."""


@dataclass(frozen=True)
class LeaseGrant:
    workspace: str
    run_id: str
    owner: str
    epoch: int
    expires_at: int


@dataclass(frozen=True)
class RecoveryGrant:
    run_id: str
    owner: str
    epoch: int
    expires_at: int


def _now_ms() -> int:
    return int(time.time() * 1000)


def _id() -> str:
    return str(uuid.uuid4())


class DurableFlowDeck:
    """A short-transaction SQLite state machine for FlowDeck orchestration."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], int] = _now_ms,
        busy_retries: int = 3,
    ):
        self.session_factory = session_factory
        self.clock = clock
        self.busy_retries = busy_retries
        self._process_lock = asyncio.Lock()

    async def _transaction(self, operation):
        last_error = None
        for attempt in range(self.busy_retries + 1):
            try:
                async with self.session_factory() as db, db.begin():
                    return await operation(db)
            except OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() or attempt >= self.busy_retries:
                    raise
                await asyncio.sleep(0.01 * (attempt + 1))
        raise last_error  # pragma: no cover

    async def create_run(
        self,
        *,
        request_key: str,
        owner: str,
        workspace: str | None = None,
        step_name: str = "root",
    ) -> tuple[FlowDeckRun, bool]:
        """Create one run per request key; duplicate requests return the original."""

        async def operation(db: AsyncSession):
            existing = await db.scalar(
                select(FlowDeckRun).where(FlowDeckRun.request_key == request_key)
            )
            if existing:
                if existing.owner != owner or existing.workspace != workspace:
                    raise DuplicateRequestError("request key already has different ownership")
                return existing, False

            now = self.clock()
            run = FlowDeckRun(
                id=_id(),
                request_key=request_key,
                owner=owner,
                workspace=workspace,
                status=RunStatus.PENDING.value,
                created_at=now,
                updated_at=now,
                version=1,
            )
            step = FlowDeckStep(
                id=_id(),
                run_id=run.id,
                sequence=0,
                name=step_name,
                status=StepStatus.PENDING.value,
                created_at=now,
                updated_at=now,
            )
            db.add_all([run, step])
            await self._event(db, run.id, "RUN_CREATED", {"request_key": request_key}, now)
            return run, True

        try:
            return await self._transaction(operation)
        except IntegrityError:
            async with self.session_factory() as db:
                existing = await db.scalar(
                    select(FlowDeckRun).where(FlowDeckRun.request_key == request_key)
                )
                if existing and existing.owner == owner and existing.workspace == workspace:
                    return existing, False
            raise

    async def start_run(self, run_id: str, *, now: int | None = None) -> None:
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            run = await self._run(db, run_id)
            self._require(run.status, {RunStatus.PENDING.value, RunStatus.ORPHANED.value})
            run.status = RunStatus.RUNNING.value
            run.heartbeat_at = now
            run.updated_at = now
            await self._event(db, run_id, "RUN_STARTED", {}, now)

        await self._transaction(operation)

    async def start_step(self, step_id: str, *, now: int | None = None) -> None:
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            step = await db.get(FlowDeckStep, step_id)
            if not step:
                raise LifecycleError("unknown step")
            self._require(step.status, {StepStatus.PENDING.value})
            step.status = StepStatus.RUNNING.value
            step.updated_at = now
            await self._event(db, step.run_id, "STEP_STARTED", {"step_id": step_id}, now)

        await self._transaction(operation)

    async def record_intent(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        capability: str,
        target: str,
        reconcile_kind: str,
        step_id: str | None = None,
        now: int | None = None,
    ) -> tuple[FlowDeckLogicalOperation, bool]:
        """Persist intent before any caller-owned side effect."""
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            existing = await db.scalar(
                select(FlowDeckLogicalOperation).where(
                    FlowDeckLogicalOperation.idempotency_key == idempotency_key
                )
            )
            if existing:
                same = (
                    existing.run_id == run_id
                    and existing.capability == capability
                    and existing.target == target
                    and existing.reconcile_kind == reconcile_kind
                )
                if not same:
                    raise DuplicateRequestError("idempotency key has incompatible intent")
                return existing, False

            run = await self._run(db, run_id)
            self._require(run.status, {RunStatus.PENDING.value, RunStatus.RUNNING.value})
            operation_row = FlowDeckLogicalOperation(
                id=_id(),
                run_id=run_id,
                step_id=step_id,
                idempotency_key=idempotency_key,
                capability=capability,
                target=target,
                reconcile_kind=reconcile_kind,
                status=OperationStatus.INTENT_RECORDED.value,
                intent_at=now,
                updated_at=now,
            )
            db.add(operation_row)
            await self._event(
                db,
                run_id,
                "OPERATION_INTENT_RECORDED",
                {"operation_id": operation_row.id, "idempotency_key": idempotency_key},
                now,
            )
            return operation_row, True

        try:
            return await self._transaction(operation)
        except IntegrityError:
            async with self.session_factory() as db:
                existing = await db.scalar(
                    select(FlowDeckLogicalOperation).where(
                        FlowDeckLogicalOperation.idempotency_key == idempotency_key
                    )
                )
                if existing:
                    return existing, False
            raise

    async def prepare_attempt(
        self,
        *,
        operation_id: str,
        owner: str,
        fencing_epoch: int,
        now: int | None = None,
    ) -> FlowDeckPhysicalAttempt:
        """Create a distinct attempt only after intent exists and fencing is valid."""
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            op = await self._operation(db, operation_id)
            self._require(
                op.status,
                {OperationStatus.INTENT_RECORDED.value, OperationStatus.FAILED.value},
            )
            run = await self._run(db, op.run_id)
            if run.workspace:
                await self._assert_workspace_lease(
                    db, run.workspace, run.id, owner, fencing_epoch, now
                )
            last = await db.scalar(
                select(func.max(FlowDeckPhysicalAttempt.attempt_no)).where(
                    FlowDeckPhysicalAttempt.operation_id == operation_id
                )
            )
            attempt = FlowDeckPhysicalAttempt(
                id=_id(),
                operation_id=operation_id,
                attempt_no=(last or 0) + 1,
                status=AttemptStatus.PREPARED.value,
                fencing_epoch=fencing_epoch,
                started_at=now,
                heartbeat_at=now,
            )
            db.add(attempt)
            op.status = OperationStatus.RUNNING.value
            op.updated_at = now
            await self._event(
                db,
                op.run_id,
                "ATTEMPT_PREPARED",
                {"operation_id": operation_id, "attempt_id": attempt.id},
                now,
            )
            return attempt

        return await self._transaction(operation)

    async def mark_attempt_unknown(
        self,
        attempt_id: str,
        *,
        error: str = "interrupted",
        now: int | None = None,
    ) -> None:
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            attempt = await db.get(FlowDeckPhysicalAttempt, attempt_id)
            if not attempt:
                raise LifecycleError("unknown attempt")
            op = await self._operation(db, attempt.operation_id)
            attempt.status = AttemptStatus.UNKNOWN.value
            attempt.error = error[:1000]
            attempt.ended_at = now
            op.status = OperationStatus.OUTCOME_UNKNOWN.value
            op.outcome = None
            op.updated_at = now
            await self._event(
                db,
                op.run_id,
                "OUTCOME_UNKNOWN",
                {"operation_id": op.id, "attempt_id": attempt_id},
                now,
            )

        await self._transaction(operation)

    async def finish_attempt(
        self,
        attempt_id: str,
        *,
        owner: str,
        fencing_epoch: int,
        outcome: str,
        now: int | None = None,
    ) -> None:
        """Commit a positively observed attempt outcome under the current fence."""
        if outcome not in {"succeeded", "failed"}:
            raise LifecycleError("attempt outcome must be succeeded or failed")
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            attempt = await db.get(FlowDeckPhysicalAttempt, attempt_id)
            if not attempt:
                raise LifecycleError("unknown attempt")
            op = await self._operation(db, attempt.operation_id)
            self._require(op.status, {OperationStatus.RUNNING.value})
            run = await self._run(db, op.run_id)
            if run.workspace:
                await self._assert_workspace_lease(
                    db, run.workspace, run.id, owner, fencing_epoch, now
                )
            attempt.status = (
                AttemptStatus.SUCCEEDED.value
                if outcome == "succeeded"
                else AttemptStatus.FAILED.value
            )
            attempt.outcome = outcome
            attempt.ended_at = now
            attempt.heartbeat_at = now
            op.status = (
                OperationStatus.SUCCEEDED.value
                if outcome == "succeeded"
                else OperationStatus.FAILED.value
            )
            op.outcome = outcome
            op.updated_at = now
            await self._event(
                db,
                op.run_id,
                "ATTEMPT_FINISHED",
                {"operation_id": op.id, "attempt_id": attempt_id, "outcome": outcome},
                now,
            )

        await self._transaction(operation)

    async def complete_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        now: int | None = None,
    ) -> None:
        """Close a run only after its durable operations have terminal outcomes."""
        if status not in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.MANUAL_REVIEW_REQUIRED,
        }:
            raise LifecycleError("run must complete with a terminal status")
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            run = await self._run(db, run_id)
            self._require(
                run.status,
                {RunStatus.RUNNING.value, RunStatus.RECOVERING.value},
            )
            operations = list(
                (
                    await db.scalars(
                        select(FlowDeckLogicalOperation).where(
                            FlowDeckLogicalOperation.run_id == run_id
                        )
                    )
                ).all()
            )
            if any(
                item.status
                not in {
                    OperationStatus.SUCCEEDED.value,
                    OperationStatus.FAILED.value,
                    OperationStatus.MANUAL_REVIEW_REQUIRED.value,
                }
                for item in operations
            ):
                raise LifecycleError("run has non-terminal operations")
            run.status = status.value
            run.updated_at = now
            await self._event(
                db, run_id, "RUN_COMPLETED", {"status": status.value}, now
            )

        await self._transaction(operation)

    async def reconcile_operation(
        self,
        operation_id: str,
        *,
        outcome: str | None,
        evidence: dict[str, Any] | None,
        now: int | None = None,
    ) -> OperationStatus:
        """Apply only verifier/runtime evidence to an unknown operation."""
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            op = await self._operation(db, operation_id)
            self._require(
                op.status,
                {
                    OperationStatus.OUTCOME_UNKNOWN.value,
                    OperationStatus.MANUAL_REVIEW_REQUIRED.value,
                },
            )
            source = (evidence or {}).get("source")
            authoritative = (evidence or {}).get("authoritative") is True
            positively_reconciled = outcome in {"succeeded", "failed"} and authoritative and source in {
                "verifier",
                "runtime",
            }
            if positively_reconciled:
                op.status = (
                    OperationStatus.SUCCEEDED.value
                    if outcome == "succeeded"
                    else OperationStatus.FAILED.value
                )
                op.outcome = outcome
                op.authoritative_evidence = evidence
                event_kind = "OUTCOME_RECONCILED"
            else:
                op.status = OperationStatus.MANUAL_REVIEW_REQUIRED.value
                op.authoritative_evidence = None
                event_kind = "MANUAL_REVIEW_REQUIRED"
            op.updated_at = now
            await self._event(
                db,
                op.run_id,
                event_kind,
                {"operation_id": operation_id, "reconcile_kind": op.reconcile_kind},
                now,
            )
            return OperationStatus(op.status)

        return await self._transaction(operation)

    async def heartbeat_run(self, run_id: str, *, now: int | None = None) -> bool:
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            result = await db.execute(
                update(FlowDeckRun)
                .where(
                    FlowDeckRun.id == run_id,
                    FlowDeckRun.status == RunStatus.RUNNING.value,
                )
                .values(heartbeat_at=now, updated_at=now)
            )
            return result.rowcount == 1

        return await self._transaction(operation)

    async def mark_orphaned(self, *, stale_before: int, now: int | None = None) -> list[str]:
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            result = await db.execute(
                select(FlowDeckRun).where(
                    FlowDeckRun.status == RunStatus.RUNNING.value,
                    FlowDeckRun.heartbeat_at.is_not(None),
                    FlowDeckRun.heartbeat_at < stale_before,
                )
            )
            runs = list(result.scalars().all())
            for run in runs:
                run.status = RunStatus.ORPHANED.value
                run.updated_at = now
                await self._event(db, run.id, "RUN_ORPHANED", {}, now)
            return [run.id for run in runs]

        return await self._transaction(operation)

    async def acquire_workspace_lease(
        self,
        *,
        workspace: str,
        run_id: str,
        owner: str,
        ttl_ms: int,
        now: int | None = None,
    ) -> LeaseGrant | None:
        now = self.clock() if now is None else now
        async with self._process_lock:
            async def operation(db: AsyncSession):
                lease = await db.get(FlowDeckWorkspaceLease, workspace)
                if lease and lease.expires_at > now and lease.owner != owner:
                    return None
                epoch = (lease.epoch + 1) if lease else 1
                if lease:
                    lease.run_id = run_id
                    lease.owner = owner
                    lease.epoch = epoch
                    lease.acquired_at = now
                    lease.heartbeat_at = now
                    lease.expires_at = now + ttl_ms
                else:
                    lease = FlowDeckWorkspaceLease(
                        workspace=workspace,
                        run_id=run_id,
                        owner=owner,
                        epoch=epoch,
                        acquired_at=now,
                        heartbeat_at=now,
                        expires_at=now + ttl_ms,
                    )
                    db.add(lease)
                return LeaseGrant(workspace, run_id, owner, epoch, now + ttl_ms)

            return await self._transaction(operation)

    async def heartbeat_workspace_lease(
        self,
        *,
        workspace: str,
        owner: str,
        epoch: int,
        ttl_ms: int,
        now: int | None = None,
    ) -> bool:
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            result = await db.execute(
                update(FlowDeckWorkspaceLease)
                .where(
                    FlowDeckWorkspaceLease.workspace == workspace,
                    FlowDeckWorkspaceLease.owner == owner,
                    FlowDeckWorkspaceLease.epoch == epoch,
                    FlowDeckWorkspaceLease.expires_at >= now,
                )
                .values(heartbeat_at=now, expires_at=now + ttl_ms)
            )
            return result.rowcount == 1

        return await self._transaction(operation)

    async def assert_workspace_fence(
        self,
        *,
        workspace: str,
        run_id: str,
        owner: str,
        epoch: int,
        now: int | None = None,
    ) -> None:
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            await self._assert_workspace_lease(db, workspace, run_id, owner, epoch, now)

        await self._transaction(operation)

    async def acquire_recovery_lease(
        self,
        *,
        run_id: str,
        owner: str,
        ttl_ms: int,
        now: int | None = None,
    ) -> RecoveryGrant | None:
        now = self.clock() if now is None else now
        async with self._process_lock:
            async def operation(db: AsyncSession):
                run = await self._run(db, run_id)
                if run.status not in {
                    RunStatus.ORPHANED.value,
                    RunStatus.RECOVERING.value,
                }:
                    return None
                lease = await db.get(FlowDeckRecoveryLease, run_id)
                if lease and lease.expires_at > now and lease.owner != owner:
                    return None
                if run.status == RunStatus.RECOVERING.value and lease and lease.expires_at > now:
                    return None
                epoch = (lease.epoch + 1) if lease else 1
                if lease:
                    lease.owner = owner
                    lease.epoch = epoch
                    lease.acquired_at = now
                    lease.heartbeat_at = now
                    lease.expires_at = now + ttl_ms
                else:
                    lease = FlowDeckRecoveryLease(
                        run_id=run_id,
                        owner=owner,
                        epoch=epoch,
                        acquired_at=now,
                        heartbeat_at=now,
                        expires_at=now + ttl_ms,
                    )
                    db.add(lease)
                run.status = RunStatus.RECOVERING.value
                run.updated_at = now
                await self._event(
                    db, run_id, "RUN_RECOVERING", {"owner": owner, "epoch": epoch}, now
                )
                return RecoveryGrant(run_id, owner, epoch, now + ttl_ms)

            return await self._transaction(operation)

    async def heartbeat_recovery_lease(
        self,
        *,
        run_id: str,
        owner: str,
        epoch: int,
        ttl_ms: int,
        now: int | None = None,
    ) -> bool:
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            result = await db.execute(
                update(FlowDeckRecoveryLease)
                .where(
                    FlowDeckRecoveryLease.run_id == run_id,
                    FlowDeckRecoveryLease.owner == owner,
                    FlowDeckRecoveryLease.epoch == epoch,
                    FlowDeckRecoveryLease.expires_at >= now,
                )
                .values(heartbeat_at=now, expires_at=now + ttl_ms)
            )
            return result.rowcount == 1

        return await self._transaction(operation)

    async def complete_recovery(
        self,
        *,
        run_id: str,
        owner: str,
        epoch: int,
        status: RunStatus,
        now: int | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        if status not in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.MANUAL_REVIEW_REQUIRED,
        }:
            raise LifecycleError("recovery must complete with a terminal status")

        async def operation(db: AsyncSession):
            run = await self._run(db, run_id)
            lease = await db.get(FlowDeckRecoveryLease, run_id)
            if (
                not lease
                or lease.owner != owner
                or lease.epoch != epoch
                or lease.expires_at < now
            ):
                raise StaleWriterError("recovery lease is stale")
            run.status = status.value
            run.updated_at = now
            await self._event(
                db, run_id, "RECOVERY_COMPLETED", {"status": status.value, "epoch": epoch}, now
            )
            await db.delete(lease)

        await self._transaction(operation)

    async def list_events(self, run_id: str) -> list[FlowDeckEvent]:
        async with self.session_factory() as db:
            result = await db.execute(
                select(FlowDeckEvent)
                .where(FlowDeckEvent.run_id == run_id)
                .order_by(FlowDeckEvent.sequence)
            )
            return list(result.scalars().all())

    async def _event(
        self,
        db: AsyncSession,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        now: int,
    ) -> FlowDeckEvent:
        last = await db.scalar(
            select(func.max(FlowDeckEvent.sequence)).where(FlowDeckEvent.run_id == run_id)
        )
        event = FlowDeckEvent(
            id=_id(),
            run_id=run_id,
            sequence=(last or 0) + 1,
            kind=kind,
            payload=payload,
            created_at=now,
        )
        db.add(event)
        return event

    async def _run(self, db: AsyncSession, run_id: str) -> FlowDeckRun:
        run = await db.get(FlowDeckRun, run_id)
        if not run:
            raise LifecycleError("unknown run")
        return run

    async def _operation(self, db: AsyncSession, operation_id: str) -> FlowDeckLogicalOperation:
        operation = await db.get(FlowDeckLogicalOperation, operation_id)
        if not operation:
            raise LifecycleError("unknown logical operation")
        return operation

    @staticmethod
    def _require(value: str, allowed: set[str]) -> None:
        if value not in allowed:
            raise LifecycleError(f"invalid transition from {value}")

    @staticmethod
    async def _assert_workspace_lease(
        db: AsyncSession,
        workspace: str,
        run_id: str,
        owner: str,
        epoch: int,
        now: int,
    ) -> None:
        lease = await db.get(FlowDeckWorkspaceLease, workspace)
        if (
            not lease
            or lease.run_id != run_id
            or lease.owner != owner
            or lease.epoch != epoch
            or lease.expires_at < now
        ):
            raise StaleWriterError("workspace lease is stale or unavailable")