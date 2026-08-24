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

from cptr.flowdeck.evidence import (
    bind_durable_identity,
    validate_reconciliation_evidence,
    validate_terminal_evidence,
)
from cptr.models.flowdeck import (
    FlowDeckApproval,
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
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    CANCELLED = "CANCELLED"


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


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


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

    async def get_step(self, run_id: str, *, sequence: int = 0) -> FlowDeckStep:
        async with self.session_factory() as db:
            step = await db.scalar(
                select(FlowDeckStep).where(
                    FlowDeckStep.run_id == run_id,
                    FlowDeckStep.sequence == sequence,
                )
            )
            if not step:
                raise LifecycleError("unknown step")
            return step

    async def create_child_step(
        self,
        *,
        run_id: str,
        name: str,
        now: int | None = None,
    ) -> FlowDeckStep:
        """Create a durable child step in a running coordinator run."""
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            run = await self._run(db, run_id)
            self._require(run.status, {RunStatus.RUNNING.value, RunStatus.RECOVERING.value})
            sequence = await db.scalar(
                select(func.max(FlowDeckStep.sequence)).where(FlowDeckStep.run_id == run_id)
            )
            step = FlowDeckStep(
                id=_id(),
                run_id=run_id,
                sequence=(sequence or 0) + 1,
                name=name,
                status=StepStatus.PENDING.value,
                created_at=now,
                updated_at=now,
            )
            db.add(step)
            await self._event(
                db, run_id, "CHILD_STEP_CREATED", {"step_id": step.id, "name": name}, now
            )
            return step

        return await self._transaction(operation)

    async def get_run_by_request_key(self, request_key: str) -> FlowDeckRun | None:
        async with self.session_factory() as db:
            return await db.scalar(
                select(FlowDeckRun).where(FlowDeckRun.request_key == request_key)
            )

    async def get_run(self, run_id: str) -> FlowDeckRun | None:
        """Read a run by ID for internal lifecycle fencing."""
        async with self.session_factory() as db:
            return await db.get(FlowDeckRun, run_id)

    async def get_run_for_owner(
        self, *, run_id: str, owner: str, workspace: str
    ) -> FlowDeckRun | None:
        async with self.session_factory() as db:
            return await db.scalar(
                select(FlowDeckRun).where(
                    FlowDeckRun.id == run_id,
                    FlowDeckRun.owner == owner,
                    FlowDeckRun.workspace == workspace,
                )
            )

    async def cancel_run(
        self, *, run_id: str, owner: str, workspace: str, now: int | None = None
    ) -> FlowDeckRun:
        """Cancel future work while preserving uncertainty for active attempts."""
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            run = await self._run(db, run_id)
            if run.owner != owner or run.workspace != workspace:
                raise LifecycleError("run ownership or workspace mismatch")
            if run.status == RunStatus.CANCELLED.value:
                return run
            self._require(
                run.status,
                {
                    RunStatus.PENDING.value,
                    RunStatus.RUNNING.value,
                    RunStatus.ORPHANED.value,
                    RunStatus.RECOVERING.value,
                },
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
            for item in operations:
                if item.status == OperationStatus.INTENT_RECORDED.value:
                    item.status = OperationStatus.MANUAL_REVIEW_REQUIRED.value
                elif item.status == OperationStatus.RUNNING.value:
                    item.status = OperationStatus.OUTCOME_UNKNOWN.value
                    item.outcome = None
                    attempts = list(
                        (
                            await db.scalars(
                                select(FlowDeckPhysicalAttempt).where(
                                    FlowDeckPhysicalAttempt.operation_id == item.id,
                                    FlowDeckPhysicalAttempt.status.in_(
                                        {
                                            AttemptStatus.PREPARED.value,
                                            AttemptStatus.RUNNING.value,
                                        }
                                    ),
                                )
                            )
                        ).all()
                    )
                    for attempt in attempts:
                        attempt.status = AttemptStatus.UNKNOWN.value
                        attempt.error = "cancelled while outcome was uncertain"
                        attempt.ended_at = now
            steps = list(
                (
                    await db.scalars(
                        select(FlowDeckStep).where(FlowDeckStep.run_id == run_id)
                    )
                ).all()
            )
            for step in steps:
                if step.status in {StepStatus.PENDING.value, StepStatus.RUNNING.value}:
                    step.status = StepStatus.CANCELLED.value
                    step.updated_at = now
            run.status = RunStatus.CANCELLED.value
            run.updated_at = now
            await self._event(db, run_id, "RUN_CANCELLED", {}, now)
            return run

        return await self._transaction(operation)

    async def get_run_operations(self, run_id: str) -> list[FlowDeckLogicalOperation]:
        async with self.session_factory() as db:
            result = await db.scalars(
                select(FlowDeckLogicalOperation).where(
                    FlowDeckLogicalOperation.run_id == run_id
                )
            )
            return list(result.all())

    async def finish_step(
        self,
        step_id: str,
        *,
        status: StepStatus,
        now: int | None = None,
    ) -> None:
        if status not in {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.MANUAL_REVIEW_REQUIRED,
        }:
            raise LifecycleError("step must finish with a terminal status")
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            step = await db.get(FlowDeckStep, step_id)
            if not step:
                raise LifecycleError("unknown step")
            self._require(step.status, {StepStatus.RUNNING.value})
            step.status = status.value
            step.updated_at = now
            await self._event(
                db,
                step.run_id,
                "STEP_COMPLETED",
                {"step_id": step_id, "status": status.value},
                now,
            )

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
                    same = (
                        existing.run_id == run_id
                        and existing.capability == capability
                        and existing.target == target
                        and existing.reconcile_kind == reconcile_kind
                    )
                    if not same:
                        raise DuplicateRequestError("idempotency key has incompatible intent")
                    return existing, False
            raise

    async def prepare_attempt(
        self,
        *,
        operation_id: str,
        owner: str,
        fencing_epoch: int = 0,
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
            if run.workspace and fencing_epoch:
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

    async def request_approval(
        self,
        *,
        operation_id: str,
        capability: str,
        now: int | None = None,
    ) -> tuple[FlowDeckApproval, bool]:
        """Record approval intent only; approval never executes an operation."""
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            logical = await self._operation(db, operation_id)
            existing = await db.scalar(
                select(FlowDeckApproval).where(FlowDeckApproval.operation_id == operation_id)
            )
            if existing:
                if existing.capability != capability:
                    raise DuplicateRequestError("approval capability does not match operation")
                return existing, False
            approval = FlowDeckApproval(
                id=_id(),
                run_id=logical.run_id,
                operation_id=operation_id,
                capability=capability,
                status=ApprovalStatus.PENDING.value,
                requested_at=now,
            )
            db.add(approval)
            await self._event(
                db,
                logical.run_id,
                "APPROVAL_REQUESTED",
                {"operation_id": operation_id, "capability": capability},
                now,
            )
            return approval, True

        return await self._transaction(operation)

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        resolved_by: str,
        evidence: dict[str, Any],
        now: int | None = None,
    ) -> None:
        """Persist a human/verifier decision without granting execution authority."""
        if status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise LifecycleError("approval must resolve to approved or rejected")
        if not resolved_by.strip():
            raise LifecycleError("approval resolver is required")
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            approval = await db.get(FlowDeckApproval, approval_id)
            if not approval:
                raise LifecycleError("unknown approval")
            self._require(approval.status, {ApprovalStatus.PENDING.value})
            approval.status = status.value
            approval.resolved_at = now
            approval.resolved_by = resolved_by
            approval.evidence = evidence
            await self._event(
                db,
                approval.run_id,
                "APPROVAL_RESOLVED",
                {"approval_id": approval_id, "status": status.value},
                now,
            )

        await self._transaction(operation)

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
            if attempt.status == AttemptStatus.UNKNOWN.value:
                return
            self._require(
                attempt.status,
                {AttemptStatus.PREPARED.value, AttemptStatus.RUNNING.value},
            )
            op = await self._operation(db, attempt.operation_id)
            self._require(op.status, {OperationStatus.RUNNING.value})
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

    async def assert_attempt_active(self, attempt_id: str) -> None:
        """Reject output from an attempt that was cancelled or superseded."""

        async def operation(db: AsyncSession):
            attempt = await db.get(FlowDeckPhysicalAttempt, attempt_id)
            if not attempt:
                raise LifecycleError("unknown attempt")
            if attempt.status not in {
                AttemptStatus.PREPARED.value,
                AttemptStatus.RUNNING.value,
            }:
                raise StaleWriterError("physical attempt is no longer active")
            op = await self._operation(db, attempt.operation_id)
            if op.status != OperationStatus.RUNNING.value:
                raise StaleWriterError("logical operation is no longer running")

        await self._transaction(operation)

    async def finish_attempt(
        self,
        attempt_id: str,
        *,
        owner: str,
        fencing_epoch: int = 0,
        outcome: str,
        evidence: dict[str, Any],
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
            self._require(
                attempt.status,
                {AttemptStatus.PREPARED.value, AttemptStatus.RUNNING.value},
            )
            if attempt.fencing_epoch != fencing_epoch:
                raise StaleWriterError("attempt fencing epoch is stale")
            op = await self._operation(db, attempt.operation_id)
            self._require(op.status, {OperationStatus.RUNNING.value})
            run = await self._run(db, op.run_id)
            try:
                bound_evidence = bind_durable_identity(
                    evidence,
                    run_id=run.id,
                    operation_id=op.id,
                    step_id=op.step_id,
                    workspace=run.workspace,
                    owner=run.owner,
                    operation_fingerprint=(
                        f"{op.capability}:{op.target}:{op.reconcile_kind}"
                    ),
                )
                validate_terminal_evidence(
                    bound_evidence,
                    outcome=outcome,
                    attempt_id=attempt_id,
                )
            except ValueError as exc:
                raise LifecycleError(str(exc)) from exc
            if run.workspace and fencing_epoch:
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
            attempt.error = None
            op.status = (
                OperationStatus.SUCCEEDED.value
                if outcome == "succeeded"
                else OperationStatus.FAILED.value
            )
            op.outcome = outcome
            op.authoritative_evidence = bound_evidence
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
            if any(
                item.status != OperationStatus.MANUAL_REVIEW_REQUIRED.value
                and (
                    not isinstance(item.authoritative_evidence, dict)
                    or item.authoritative_evidence.get("authoritative") is not True
                )
                for item in operations
            ):
                raise LifecycleError("run requires authoritative evidence for every operation")
            if status == RunStatus.SUCCEEDED and any(
                item.status != OperationStatus.SUCCEEDED.value for item in operations
            ):
                raise LifecycleError("successful run requires every operation to succeed")
            run.status = status.value
            run.updated_at = now
            await self._event(
                db, run_id, "RUN_COMPLETED", {"status": status.value}, now
            )

        await self._transaction(operation)

    async def record_clarification(self, run_id: str, *, message: str) -> None:
        """Record a coordinator-owned non-executing clarification."""
        now = self.clock()

        async def operation(db: AsyncSession):
            run = await self._run(db, run_id)
            self._require(run.status, {RunStatus.RUNNING.value, RunStatus.RECOVERING.value})
            await self._event(
                db,
                run_id,
                "RUN_CLARIFICATION",
                {"outcome": "clarification", "message": message, "executed": False},
                now,
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
                },
            )
            positively_reconciled = False
            if outcome in {"succeeded", "failed"}:
                try:
                    run = await self._run(db, op.run_id)
                    bound_evidence = bind_durable_identity(
                        evidence or {},
                        run_id=run.id,
                        operation_id=op.id,
                        step_id=op.step_id,
                        workspace=run.workspace,
                        owner=run.owner,
                        operation_fingerprint=(
                            f"{op.capability}:{op.target}:{op.reconcile_kind}"
                        ),
                    )
                    validate_reconciliation_evidence(bound_evidence, outcome=outcome)
                except ValueError:
                    pass
                else:
                    positively_reconciled = True
            if positively_reconciled:
                op.status = (
                    OperationStatus.SUCCEEDED.value
                    if outcome == "succeeded"
                    else OperationStatus.FAILED.value
                )
                op.outcome = outcome
                op.authoritative_evidence = bound_evidence
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

    async def orphan_run(self, run_id: str, *, now: int | None = None) -> None:
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            run = await self._run(db, run_id)
            self._require(run.status, {RunStatus.RUNNING.value, RunStatus.RECOVERING.value})
            run.status = RunStatus.ORPHANED.value
            run.updated_at = now
            await self._event(db, run_id, "RUN_ORPHANED", {}, now)

        await self._transaction(operation)

    async def require_manual_review(
        self, run_id: str, *, reason: str, now: int | None = None
    ) -> None:
        """Close an interrupted run without converting uncertain work to success."""
        now = self.clock() if now is None else now

        async def operation(db: AsyncSession):
            run = await self._run(db, run_id)
            self._require(
                run.status,
                {
                    RunStatus.PENDING.value,
                    RunStatus.RUNNING.value,
                    RunStatus.RECOVERING.value,
                },
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
            for item in operations:
                if item.status in {
                    OperationStatus.INTENT_RECORDED.value,
                    OperationStatus.RUNNING.value,
                    OperationStatus.OUTCOME_UNKNOWN.value,
                }:
                    item.status = OperationStatus.MANUAL_REVIEW_REQUIRED.value
                    item.updated_at = now
            steps = list(
                (
                    await db.scalars(
                        select(FlowDeckStep).where(FlowDeckStep.run_id == run_id)
                    )
                ).all()
            )
            for step in steps:
                if step.status in {StepStatus.PENDING.value, StepStatus.RUNNING.value}:
                    step.status = StepStatus.MANUAL_REVIEW_REQUIRED.value
                    step.updated_at = now
            run.status = RunStatus.MANUAL_REVIEW_REQUIRED.value
            run.updated_at = now
            await self._event(
                db,
                run_id,
                "RUN_MANUAL_REVIEW",
                {"status": RunStatus.MANUAL_REVIEW_REQUIRED.value, "reason": reason},
                now,
            )

        await self._transaction(operation)

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

            try:
                return await self._transaction(operation)
            except IntegrityError:
                # Two workers can both observe an empty lease table before
                # either transaction inserts. The unique constraint is the
                # authoritative arbiter; losing that race means the lease is
                # unavailable, not that the caller should bypass fencing.
                return None

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

    async def release_workspace_lease(
        self,
        *,
        workspace: str,
        owner: str,
        epoch: int,
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
                )
                .values(expires_at=now)
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

    async def record_event(
        self, run_id: str, kind: str, payload: dict[str, Any]
    ) -> FlowDeckEvent:
        """Append a lifecycle event through the durable transaction boundary."""
        now = self.clock()

        async def operation(db: AsyncSession):
            run = await self._run(db, run_id)
            self._require(
                run.status,
                {RunStatus.PENDING.value, RunStatus.RUNNING.value},
            )
            return await self._event(db, run_id, kind, payload, now)

        return await self._transaction(operation)

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
        # FlowDeck lifecycle events are observational only. Broadcast the
        # durable record through CPTR's existing authenticated Socket.IO
        # channel; model/tool execution remains exclusively in CPTR.
        try:
            run = await db.get(FlowDeckRun, run_id)
            if run:
                from cptr.socket.main import emit_to_user

                await emit_to_user(
                    run.owner,
                    {
                        "type": "flowdeck:event",
                        "flowdeck_run_id": run_id,
                        "kind": kind,
                        "payload": payload,
                        "sequence": event.sequence,
                        "created_at": now,
                    },
                )
        except Exception:
            # Event delivery must never change durable lifecycle semantics.
            pass
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