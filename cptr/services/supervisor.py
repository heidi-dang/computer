"""Durable-supervisor domain contracts and the resumable monitor loop.

The persistence adapter is intentionally injected.  This keeps the lifecycle
rules independently testable and lets the HTTP/API layer share the same
state machine without introducing another worker engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from cptr.env import TASK_CANCELLATION_TIMEOUT_SECONDS
from cptr.services.verification import DefaultIndependentVerifier, IndependentVerifier
from cptr.utils.redaction import redact_sensitive
from cptr.utils.workspace_fingerprint import changed_paths

logger = logging.getLogger(__name__)


class ScopeStatus(StrEnum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    WORKING = "WORKING"
    AGENT_COMPLETE = "AGENT_COMPLETE"
    VERIFYING = "VERIFYING"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class MonitorStatus(StrEnum):
    RUNNING = "RUNNING"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPLETE = "COMPLETE"


TERMINAL_TASK_STATUSES = {"COMPLETE", "COMPLETED", "SUCCEEDED", "FAILED", "ERROR", "CANCELLED"}
APPROVAL_PATTERNS = (
    re.compile(r"\bgit\s+push\b", re.IGNORECASE),
    re.compile(r"\bpush\s+(?:to\s+)?(?:github|gitlab|origin)\b", re.IGNORECASE),
    re.compile(r"\bpush\b.*\bexternal\s+git\s+remote\b", re.IGNORECASE),
    re.compile(r"\b(?:production|prod)\s+deploy(?:ment)?\b", re.IGNORECASE),
    re.compile(r"\b(?:deploy|release)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:drop|delete|purge|destroy)\b.*\b(?:database|all|bucket|storage)\b", re.IGNORECASE
    ),
    re.compile(r"\bcredential(?:s)?\s+rotation\b", re.IGNORECASE),
    re.compile(r"\b(?:purchase|paid|costly)\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class Decision:
    scope_satisfied: bool = False
    goal_satisfied: bool = False
    defects: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    next_action_required: bool = False
    next_assignment: str | None = None
    blocking_reason: str | None = None


@dataclass
class ScopeRecord:
    scope_id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    status: ScopeStatus = ScopeStatus.PENDING
    attempt_count: int = 0
    worker_task_ids: list[str] = field(default_factory=list)
    steering_requests: list[dict[str, Any]] = field(default_factory=list)
    verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    failure_evidence: list[dict[str, Any]] = field(default_factory=list)
    failure_signature_counts: dict[str, int] = field(default_factory=dict)
    last_decision: dict[str, Any] = field(default_factory=dict)
    next_action: str | None = None
    history: list[ScopeStatus] = field(default_factory=list)
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def transition(self, status: ScopeStatus) -> None:
        if self.status != status:
            self.history.append(status)
            self.status = status
            self.updated_at = int(time.time() * 1000)


@dataclass
class MonitorState:
    monitor_id: str
    goal_id: str
    user_id: str
    workspace_id: str
    original_goal: str
    original_acceptance_criteria: list[str]
    model_id: str
    scopes: list[ScopeRecord]
    status: MonitorStatus = MonitorStatus.RUNNING
    current_scope_id: str | None = None
    approval_id: str | None = None
    approved_operations: list[str] = field(default_factory=list)
    director_state: dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class EvidenceRecord:
    evidence_id: str
    monitor_id: str
    scope_id: str | None
    kind: str
    payload: dict[str, Any]
    created_at: int


@dataclass
class ApprovalRecord:
    approval_id: str
    monitor_id: str
    operation: str
    reason: str
    status: str = "PENDING"
    requested_at: int = field(default_factory=lambda: int(time.time() * 1000))
    decided_at: int | None = None
    decided_by: str | None = None


class SupervisorStore(Protocol):
    async def create_monitor(
        self, monitor: MonitorState, idempotency_key: str | None
    ) -> MonitorState: ...

    async def get_monitor(self, monitor_id: str) -> MonitorState | None: ...

    async def save_monitor(self, monitor: MonitorState) -> None: ...

    async def request_cancel_monitor(self, monitor_id: str) -> bool: ...

    async def finalize_cancel_monitor(self, monitor_id: str) -> bool: ...

    async def block_cancel_monitor(self, monitor_id: str) -> bool: ...

    async def claim_monitor(self, monitor_id: str) -> bool: ...

    async def release_monitor(self, monitor_id: str) -> None: ...

    async def append_evidence(
        self, monitor_id: str, scope_id: str | None, kind: str, payload: dict[str, Any]
    ) -> EvidenceRecord: ...

    async def list_evidence(self, monitor_id: str) -> list[EvidenceRecord]: ...

    async def create_approval(
        self, monitor_id: str, operation: str, reason: str
    ) -> ApprovalRecord: ...

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None: ...

    async def decide_approval(
        self, approval_id: str, *, status: str, decided_by: str
    ) -> ApprovalRecord: ...

    async def claim_workspace(self, workspace_id: str, monitor_id: str) -> bool: ...

    async def release_workspace(self, workspace_id: str, monitor_id: str) -> None: ...


class InMemorySupervisorStore:
    """Small deterministic store used by unit tests and local service wiring."""

    def __init__(self) -> None:
        self.monitors: dict[str, MonitorState] = {}
        self.idempotency: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.evidence: list[EvidenceRecord] = []
        self.approvals: dict[str, ApprovalRecord] = {}
        self._workspace_leases: dict[str, str] = {}

    async def create_monitor(
        self, monitor: MonitorState, idempotency_key: str | None
    ) -> MonitorState:
        if idempotency_key and idempotency_key in self.idempotency:
            return self.monitors[self.idempotency[idempotency_key]]
        self.monitors[monitor.monitor_id] = monitor
        if idempotency_key:
            self.idempotency[idempotency_key] = monitor.monitor_id
        return monitor

    async def get_monitor(self, monitor_id: str) -> MonitorState | None:
        return self.monitors.get(monitor_id)

    async def save_monitor(self, monitor: MonitorState) -> None:
        existing = self.monitors.get(monitor.monitor_id)
        terminal = {
            MonitorStatus.COMPLETE,
            MonitorStatus.CANCELLED,
            MonitorStatus.BLOCKED,
            MonitorStatus.FAILED,
            MonitorStatus.CANCEL_REQUESTED,
        }
        releasable_terminal = terminal - {MonitorStatus.CANCEL_REQUESTED}
        if existing and existing.status in terminal and monitor.status != existing.status:
            return
        monitor.updated_at = int(time.time() * 1000)
        self.monitors[monitor.monitor_id] = monitor
        if monitor.status in releasable_terminal:
            await self.release_workspace(monitor.workspace_id, monitor.monitor_id)

    async def request_cancel_monitor(self, monitor_id: str) -> bool:
        monitor = self.monitors.get(monitor_id)
        if monitor is None:
            return False
        if monitor.status in {
            MonitorStatus.COMPLETE,
            MonitorStatus.CANCELLED,
            MonitorStatus.BLOCKED,
            MonitorStatus.FAILED,
            MonitorStatus.CANCEL_REQUESTED,
        }:
            return False
        monitor.status = MonitorStatus.CANCEL_REQUESTED
        return True

    async def finalize_cancel_monitor(self, monitor_id: str) -> bool:
        monitor = self.monitors.get(monitor_id)
        if monitor is None or monitor.status != MonitorStatus.CANCEL_REQUESTED:
            return False
        monitor.status = MonitorStatus.CANCELLED
        for scope in monitor.scopes:
            if scope.status not in {ScopeStatus.VERIFIED, ScopeStatus.CANCELLED}:
                scope.transition(ScopeStatus.CANCELLED)
            scope.next_action = None
        await self.release_workspace(monitor.workspace_id, monitor.monitor_id)
        return True

    async def cancel_monitor(self, monitor_id: str) -> bool:
        if not await self.request_cancel_monitor(monitor_id):
            return False
        return await self.finalize_cancel_monitor(monitor_id)

    async def block_cancel_monitor(self, monitor_id: str) -> bool:
        monitor = self.monitors.get(monitor_id)
        if monitor is None or monitor.status != MonitorStatus.CANCEL_REQUESTED:
            return False
        monitor.status = MonitorStatus.BLOCKED
        for scope in monitor.scopes:
            if scope.status not in {ScopeStatus.VERIFIED, ScopeStatus.CANCELLED}:
                scope.transition(ScopeStatus.BLOCKED)
            scope.next_action = "Owned execution did not quiesce within the cancellation bound."
        return True

    async def claim_monitor(self, monitor_id: str) -> bool:
        lock = self._locks.setdefault(monitor_id, asyncio.Lock())
        if lock.locked():
            return False
        await lock.acquire()
        return True

    async def release_monitor(self, monitor_id: str) -> None:
        lock = self._locks.get(monitor_id)
        if lock and lock.locked():
            lock.release()

    async def append_evidence(
        self, monitor_id: str, scope_id: str | None, kind: str, payload: dict[str, Any]
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            evidence_id=f"evidence_{uuid.uuid4().hex[:20]}",
            monitor_id=monitor_id,
            scope_id=scope_id,
            kind=kind,
            payload=redact_sensitive(payload),
            created_at=int(time.time() * 1000),
        )
        self.evidence.append(record)
        return record

    async def list_evidence(self, monitor_id: str) -> list[EvidenceRecord]:
        return [item for item in self.evidence if item.monitor_id == monitor_id]

    async def create_approval(self, monitor_id: str, operation: str, reason: str) -> ApprovalRecord:
        record = ApprovalRecord(
            approval_id=f"approval_{uuid.uuid4().hex[:20]}",
            monitor_id=monitor_id,
            operation=operation,
            reason=reason,
        )
        self.approvals[record.approval_id] = record
        return record

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        return self.approvals.get(approval_id)

    async def decide_approval(
        self, approval_id: str, *, status: str, decided_by: str
    ) -> ApprovalRecord:
        record = self.approvals[approval_id]
        record.status = status
        record.decided_at = int(time.time() * 1000)
        record.decided_by = decided_by
        return record

    async def claim_workspace(self, workspace_id: str, monitor_id: str) -> bool:
        current = self._workspace_leases.get(workspace_id)
        if current is not None and current != monitor_id:
            owner = self.monitors.get(current)
            if owner is None or owner.status in {
                MonitorStatus.COMPLETE,
                MonitorStatus.CANCELLED,
                MonitorStatus.BLOCKED,
                MonitorStatus.FAILED,
            }:
                self._workspace_leases.pop(workspace_id, None)
            else:
                return False
        self._workspace_leases[workspace_id] = monitor_id
        return True

    async def release_workspace(self, workspace_id: str, monitor_id: str) -> None:
        if self._workspace_leases.get(workspace_id) == monitor_id:
            self._workspace_leases.pop(workspace_id, None)


class SupervisorAgent(Protocol):
    async def start_task(self, **kwargs: Any) -> dict[str, Any]: ...

    async def get_task(self, task_id: str, **kwargs: Any) -> dict[str, Any]: ...

    async def get_output(self, task_id: str, **kwargs: Any) -> dict[str, Any]: ...

    async def get_diff(self, workspace_id: str, **kwargs: Any) -> dict[str, Any]: ...

    async def get_verification_evidence(
        self, workspace_id: str, **kwargs: Any
    ) -> dict[str, Any]: ...

    async def cancel_task(self, task_id: str, **kwargs: Any) -> dict[str, Any]: ...


class SupervisorDirector(Protocol):
    async def evaluate(self, **kwargs: Any) -> Decision: ...

    async def diagnose(self, **kwargs: Any) -> Decision: ...

    async def plan_next_action(self, **kwargs: Any) -> Decision: ...

    async def final_gate(self, **kwargs: Any) -> Decision: ...


def normalize_failure_signature(failure: dict[str, Any]) -> str:
    """Normalize stable failure facts so line-number/log changes do not reset retries."""
    category = str(failure.get("category") or failure.get("type") or "unknown").strip().lower()
    scope_id = str(failure.get("scope_id") or "").strip().lower()
    message = str(failure.get("message") or failure.get("reason") or "").lower()
    message = re.sub(r"\b(line|ln|at)\s+\d+\b", "", message)
    message = re.sub(r"\b[0-9a-f]{7,40}\b", "<hash>", message)
    message = re.sub(r"\s+", " ", message).strip()
    return hashlib.sha256(f"{category}|{scope_id}|{message}".encode()).hexdigest()[:24]


class AutonomousSupervisor:
    def __init__(
        self,
        *,
        store: SupervisorStore,
        agent: SupervisorAgent,
        director: SupervisorDirector,
        verifier: IndependentVerifier | None = None,
        max_attempts: int = 5,
    ) -> None:
        self.store = store
        self.agent = agent
        self.director = director
        self.verifier = verifier or DefaultIndependentVerifier()
        self.max_attempts = max(1, max_attempts)
        self._active_runs: dict[str, asyncio.Task] = {}

    async def create_goal(
        self,
        *,
        user_id: str,
        workspace_id: str,
        goal: str,
        acceptance_criteria: list[str],
        model_id: str,
        idempotency_key: str | None = None,
    ) -> MonitorState:
        normalized_goal = goal.strip()
        criteria = [item.strip() for item in acceptance_criteria if item.strip()]
        if not normalized_goal:
            raise ValueError("goal must not be blank")
        if not criteria:
            raise ValueError("at least one acceptance criterion is required")
        scopes = [
            ScopeRecord(
                scope_id=f"scope_{uuid.uuid4().hex[:16]}",
                title=criterion[:120],
                description=f"{normalized_goal}: {criterion}",
                acceptance_criteria=[criterion],
            )
            for criterion in criteria
        ]
        monitor = MonitorState(
            monitor_id=f"mon_{uuid.uuid4().hex[:20]}",
            goal_id=f"goal_{uuid.uuid4().hex[:20]}",
            user_id=user_id,
            workspace_id=workspace_id,
            original_goal=normalized_goal,
            original_acceptance_criteria=list(criteria),
            model_id=model_id,
            scopes=scopes,
        )
        return await self.store.create_monitor(monitor, idempotency_key)

    async def record_steering(
        self,
        monitor_id: str,
        *,
        scope_id: str,
        control_message_id: str,
        intended_task_id: str,
        intended_generation_id: str | None,
        baseline_diff_fingerprint: str | None = None,
        baseline_workspace_snapshot: dict[str, Any] | None = None,
    ) -> MonitorState:
        """Bind autonomous control to the worker generation it targeted."""
        monitor = await self._required_monitor(monitor_id)
        scope = next((item for item in monitor.scopes if item.scope_id == scope_id), None)
        if scope is None or monitor.status != MonitorStatus.RUNNING:
            raise ValueError("monitor is no longer steerable")
        record = {
            "control_message_id": control_message_id,
            "intended_task_id": intended_task_id,
            "intended_generation_id": intended_generation_id,
            "baseline_diff_fingerprint": baseline_diff_fingerprint,
            "baseline_workspace_snapshot": baseline_workspace_snapshot,
            "baseline_workspace_fingerprint": (baseline_workspace_snapshot or {}).get(
                "fingerprint"
            ),
            "post_consumption_workspace_fingerprint": None,
            "effect_observed_at": None,
            "effect_status": "PENDING",
            "status": "QUEUED",
        }
        if not any(
            item.get("control_message_id") == control_message_id for item in scope.steering_requests
        ):
            scope.steering_requests.append(record)
        await self.store.save_monitor(monitor)
        return monitor

    async def approve(self, monitor_id: str, *, approval_id: str, approved: bool) -> MonitorState:
        monitor = await self._required_monitor(monitor_id)
        approval = await self.store.get_approval(approval_id)
        if (
            monitor.status != MonitorStatus.APPROVAL_REQUIRED
            or monitor.approval_id != approval_id
            or approval is None
            or approval.monitor_id != monitor_id
            or approval.status != "PENDING"
        ):
            raise ValueError("approval request is no longer pending")
        await self.store.decide_approval(
            approval_id,
            status="APPROVED" if approved else "DENIED",
            decided_by=monitor.user_id,
        )
        monitor.approval_id = None
        if approved:
            if approval.operation not in monitor.approved_operations:
                monitor.approved_operations.append(approval.operation)
            monitor.status = MonitorStatus.RUNNING
        else:
            monitor.status = MonitorStatus.BLOCKED
        if not approved:
            scope = next(
                (item for item in monitor.scopes if item.scope_id == monitor.current_scope_id), None
            )
            if scope:
                scope.transition(ScopeStatus.BLOCKED)
                scope.next_action = approval.reason
            await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)
        await self.store.save_monitor(monitor)
        return monitor

    async def cancel(self, monitor_id: str) -> MonitorState:
        monitor = await self._required_monitor(monitor_id)
        request_cancel = getattr(self.store, "request_cancel_monitor", None)
        if callable(request_cancel):
            requested = await request_cancel(monitor_id)
            current = await self._required_monitor(monitor_id)
            if not requested and current.status != MonitorStatus.CANCEL_REQUESTED:
                return current
        else:
            cancel_monitor = getattr(self.store, "cancel_monitor", None)
            if callable(cancel_monitor) and not await cancel_monitor(monitor_id):
                return await self._required_monitor(monitor_id)

        active_run = self._active_runs.get(monitor_id)
        if active_run and active_run is not asyncio.current_task() and not active_run.done():
            active_run.cancel()

        monitor = await self._required_monitor(monitor_id)
        cancellation_blocked = False
        for scope in monitor.scopes:
            task_ids = list(dict.fromkeys(scope.worker_task_ids))
            cancel_task = getattr(self.agent, "cancel_task", None)
            if callable(cancel_task):
                for task_id in task_ids:
                    try:
                        result = await cancel_task(task_id, user_id=monitor.user_id)
                        if isinstance(result, dict) and result.get("cancelled") is False:
                            # A monitor may retain terminal worker attempts from an
                            # earlier repair cycle.  Those tasks are already quiescent;
                            # only an explicit non-terminal result means cancellation
                            # failed to stop owned execution.
                            result_status = str(result.get("status") or "").upper()
                            if result_status not in {"COMPLETE", "FAILED", "CANCELLED"}:
                                cancellation_blocked = True
                    except Exception:  # noqa: BLE001 - cancellation remains durable
                        logger.warning("worker cancellation failed for task %s", task_id)
                        cancellation_blocked = True
            if scope.status not in {ScopeStatus.VERIFIED, ScopeStatus.CANCELLED}:
                scope.transition(ScopeStatus.CANCELLED)
            scope.next_action = None

        if active_run and active_run is not asyncio.current_task():
            try:
                await asyncio.wait_for(
                    asyncio.shield(active_run), timeout=TASK_CANCELLATION_TIMEOUT_SECONDS
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not active_run.cancelled():
                    logger.warning("supervisor run did not quiesce for monitor %s", monitor_id)

        if cancellation_blocked:
            block_cancel = getattr(self.store, "block_cancel_monitor", None)
            if callable(block_cancel):
                await block_cancel(monitor_id)
            else:
                monitor.status = MonitorStatus.BLOCKED
                for scope in monitor.scopes:
                    if scope.status not in {ScopeStatus.VERIFIED, ScopeStatus.CANCELLED}:
                        scope.transition(ScopeStatus.BLOCKED)
                    scope.next_action = (
                        "Owned execution did not quiesce within the cancellation bound."
                    )
            await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)
            return await self._required_monitor(monitor_id)

        finalize_cancel = getattr(self.store, "finalize_cancel_monitor", None)
        if callable(finalize_cancel):
            await finalize_cancel(monitor_id)
        monitor = await self._required_monitor(monitor_id)
        monitor.status = MonitorStatus.CANCELLED
        await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)
        await self.store.save_monitor(monitor)
        return await self._required_monitor(monitor_id)

    async def run_once(self, monitor_id: str) -> MonitorState:
        if not await self.store.claim_monitor(monitor_id):
            monitor = await self._required_monitor(monitor_id)
            return monitor
        current_run = asyncio.current_task()
        if current_run is not None:
            self._active_runs[monitor_id] = current_run
        try:
            monitor = await self._required_monitor(monitor_id)
            if monitor.status != MonitorStatus.RUNNING:
                return monitor

            scope = next(
                (
                    item
                    for item in monitor.scopes
                    if item.status not in {ScopeStatus.VERIFIED, ScopeStatus.CANCELLED}
                ),
                None,
            )
            if scope is None:
                return await self._run_final_gate(monitor)
            monitor.current_scope_id = scope.scope_id

            if scope.status in {
                ScopeStatus.PENDING,
                ScopeStatus.ASSIGNED,
                ScopeStatus.REPAIR_REQUIRED,
            }:
                assignment = scope.next_action or scope.description
                await self._try_delegate(monitor, scope, assignment)
                await self.store.save_monitor(monitor)
                return monitor

            if scope.status in {
                ScopeStatus.WORKING,
                ScopeStatus.AGENT_COMPLETE,
                ScopeStatus.VERIFYING,
            }:
                await self._observe_and_verify(monitor, scope)
                await self.store.save_monitor(monitor)
                if all(item.status == ScopeStatus.VERIFIED for item in monitor.scopes):
                    return await self._run_final_gate(monitor)
                return monitor

            return monitor
        finally:
            if self._active_runs.get(monitor_id) is current_run:
                self._active_runs.pop(monitor_id, None)
            await self.store.release_monitor(monitor_id)

    async def _monitor_is_running(self, monitor_id: str) -> bool:
        monitor = await self.store.get_monitor(monitor_id)
        return monitor is not None and monitor.status == MonitorStatus.RUNNING

    async def _observe_and_verify(self, monitor: MonitorState, scope: ScopeRecord) -> None:
        if not await self._monitor_is_running(monitor.monitor_id):
            return
        task_id = scope.worker_task_ids[-1] if scope.worker_task_ids else None
        if not task_id:
            scope.transition(ScopeStatus.REPAIR_REQUIRED)
            scope.next_action = "Delegate the scope to a worker."
            return
        task = await self.agent.get_task(task_id, user_id=monitor.user_id)
        if not await self._monitor_is_running(monitor.monitor_id):
            return
        task_status = str(task.get("status") or "").upper()
        await self._append_evidence(monitor, scope, "worker_state", task)
        if task_status not in TERMINAL_TASK_STATUSES:
            scope.transition(ScopeStatus.WORKING)
            return
        if task_status in {"FAILED", "ERROR", "CANCELLED"}:
            await self._repair_or_block(
                monitor, scope, {"category": "worker_failure", "message": task.get("error")}
            )
            return

        steering_records = await self._steering_provenance_records(scope)
        if steering_records:
            invalid_steering = next(
                (
                    item
                    for item in steering_records
                    if item.get("status") != "CONSUMED"
                    or item.get("consumed_task_id") != item.get("intended_task_id")
                ),
                None,
            )
            if invalid_steering is not None:
                scope.next_action = "Wait for the intended worker to consume the steering control."
                await self._append_evidence(
                    monitor,
                    scope,
                    "steering_provenance",
                    {"requests": steering_records, "status": "NOT_VERIFIED"},
                )
                return

            effect_failure = await self._observe_steering_effects(
                monitor, scope, task_id, steering_records
            )
            if effect_failure is not None:
                await self._append_evidence(
                    monitor,
                    scope,
                    "steering_provenance",
                    {"requests": steering_records, "status": "NOT_VERIFIED"},
                )
                await self._repair_or_block(
                    monitor,
                    scope,
                    {
                        "category": "steering_effect_not_observed",
                        "scope_id": scope.scope_id,
                        "message": effect_failure,
                    },
                )
                return

            # Refresh the records after effect observation.  The observation
            # writes effect status/snapshots onto the durable scope record, so
            # the copies loaded before that call are intentionally stale.
            steering_records = await self._steering_provenance_records(scope)

        required_evidence_failure = self._required_steering_evidence_failure(
            scope, steering_records, task_id
        )
        if required_evidence_failure is not None:
            # Generic terminal-task and Git checks are necessary infrastructure
            # evidence, not proof of a semantic steering criterion.  Enforce
            # this gate before invoking the verifier/director so neither a
            # successful worker summary nor a permissive director decision can
            # turn missing control provenance into VERIFIED.
            await self._append_evidence(
                monitor,
                scope,
                "criterion_evidence",
                {
                    "status": "UNSATISFIED",
                    "required": "same_worker_steering",
                    "message": required_evidence_failure,
                    "steering_provenance": steering_records,
                },
            )
            await self._repair_or_block(
                monitor,
                scope,
                {
                    "category": "mandatory_criterion_evidence_missing",
                    "scope_id": scope.scope_id,
                    "message": required_evidence_failure,
                },
            )
            return

        scope.transition(ScopeStatus.AGENT_COMPLETE)
        scope.transition(ScopeStatus.VERIFYING)
        evidence = {
            "task": await self.agent.get_output(task_id, user_id=monitor.user_id),
            "diff": await self.agent.get_diff(monitor.workspace_id, user_id=monitor.user_id),
        }
        if steering_records:
            evidence["steering_provenance"] = steering_records
        if not await self._monitor_is_running(monitor.monitor_id):
            return
        get_verification_evidence = getattr(self.agent, "get_verification_evidence", None)
        evidence["independent"] = (
            await get_verification_evidence(
                monitor.workspace_id,
                user_id=monitor.user_id,
            )
            if callable(get_verification_evidence)
            else {}
        )
        await self._append_evidence(monitor, scope, "worker_output", evidence)
        verification = await self.verifier.verify(
            task=task,
            evidence=evidence,
            monitor=monitor,
            scope=scope,
        )
        if not await self._monitor_is_running(monitor.monitor_id):
            return
        for check in verification.checks:
            if check.get("verification_command"):
                await self._append_evidence(monitor, scope, "verification_command", check)
        await self._append_evidence(
            monitor,
            scope,
            "verification_result",
            {
                "passed": verification.passed,
                "checks": verification.checks,
                "failures": verification.failures,
            },
        )
        if not verification.passed:
            await self._repair_or_block(
                monitor,
                scope,
                {
                    "category": "independent_verification_failure",
                    "scope_id": scope.scope_id,
                    "message": "; ".join(verification.failures),
                },
            )
            return
        try:
            if not await self._monitor_is_running(monitor.monitor_id):
                return
            decision = await self.director.evaluate(
                monitor=monitor,
                scope=scope,
                evidence=evidence,
                original_goal=monitor.original_goal,
                original_acceptance_criteria=monitor.original_acceptance_criteria,
            )
            if not await self._monitor_is_running(monitor.monitor_id):
                return
        except Exception:
            logger.exception("supervisor director evaluate failed for scope %s", scope.scope_id)
            await self._repair_or_block(
                monitor,
                scope,
                {
                    "category": "director_failure",
                    "scope_id": scope.scope_id,
                    "message": "scope verification could not be evaluated",
                },
            )
            return
        verification_fingerprint = hashlib.sha256(
            json.dumps(
                redact_sensitive(
                    {
                        "scope_id": scope.scope_id,
                        "task_id": task_id,
                        "generation_id": next(
                            (
                                item.get("consumed_message_id")
                                for item in steering_records
                                if item.get("consumed_task_id") == task_id
                            ),
                            None,
                        ),
                        "evidence": evidence,
                        "steering": steering_records,
                    }
                ),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        if scope.last_decision.get("_verification_fingerprint") == verification_fingerprint:
            await self._append_evidence(
                monitor,
                scope,
                "verification_convergence",
                {"status": "UNCHANGED", "fingerprint": verification_fingerprint},
            )
            await self._repair_or_block(
                monitor,
                scope,
                {
                    "category": "verification_unchanged",
                    "scope_id": scope.scope_id,
                    "message": "verification evidence did not change between attempts",
                },
            )
            return
        scope.last_decision["_verification_fingerprint"] = verification_fingerprint
        self._sync_director_state(monitor)
        scope.last_decision = {
            **decision.__dict__,
            "_verification_fingerprint": verification_fingerprint,
        }
        await self._append_evidence(monitor, scope, "director_decision", scope.last_decision)
        if decision.scope_satisfied and not decision.defects and not decision.regressions:
            scope.verification_evidence.append(evidence)
            scope.transition(ScopeStatus.VERIFIED)
            scope.next_action = None
            return
        failure = {
            "category": "verification_failure",
            "scope_id": scope.scope_id,
            "message": "; ".join(decision.defects + decision.regressions) or "scope not satisfied",
            "signature": normalize_failure_signature(
                {
                    "category": "verification_failure",
                    "scope_id": scope.scope_id,
                    "message": ";".join(decision.defects + decision.regressions),
                }
            ),
        }
        await self._repair_or_block(monitor, scope, failure, decision=decision)

    async def _repair_or_block(
        self,
        monitor: MonitorState,
        scope: ScopeRecord,
        failure: dict[str, Any],
        *,
        decision: Decision | None = None,
    ) -> None:
        scope.attempt_count += 1
        scope.failure_evidence.append(failure)
        signature = str(failure.get("signature") or normalize_failure_signature(failure))
        failure["signature"] = signature
        same_signature_attempt = scope.failure_signature_counts.get(signature, 0) + 1
        scope.failure_signature_counts[signature] = same_signature_attempt
        failure["signature_attempt"] = same_signature_attempt
        await self._append_evidence(monitor, scope, "failure", failure)
        if scope.attempt_count >= self.max_attempts or same_signature_attempt >= self.max_attempts:
            scope.transition(ScopeStatus.BLOCKED)
            monitor.status = MonitorStatus.BLOCKED
            await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)
            return
        escalation = {
            1: "normal repair",
            2: "explicit root-cause re-analysis",
            3: "alternative implementation strategy",
            4: "independent verification/reviewer strategy",
        }.get(same_signature_attempt, "escalated repair")
        failure["escalation"] = escalation
        try:
            if decision is None:
                decision = await self.director.diagnose(
                    monitor=monitor, scope=scope, failure=failure
                )
            else:
                diagnosis = await self.director.diagnose(
                    monitor=monitor, scope=scope, failure=failure
                )
                if diagnosis.next_assignment:
                    decision = diagnosis
            plan = await self.director.plan_next_action(
                monitor=monitor, scope=scope, decision=decision
            )
            self._sync_director_state(monitor)
        except Exception:
            logger.exception(
                "supervisor director repair planning failed for scope %s", scope.scope_id
            )
            scope.last_decision = {
                "next_action_required": True,
                "next_assignment": "Retry after the supervisor director recovers.",
            }
            scope.next_action = "Retry after the supervisor director recovers."
            scope.transition(ScopeStatus.REPAIR_REQUIRED)
            return
        scope.last_decision = plan.__dict__.copy()
        scope.next_action = (
            plan.next_assignment or decision.next_assignment or "Re-evaluate the failed scope."
        )
        scope.next_action = f"[{escalation}] {scope.next_action}"
        scope.transition(ScopeStatus.REPAIR_REQUIRED)
        await self._try_delegate(monitor, scope, scope.next_action)

    async def _try_delegate(
        self, monitor: MonitorState, scope: ScopeRecord, assignment: str
    ) -> None:
        if not await self._monitor_is_running(monitor.monitor_id):
            return
        approval_operation = assignment[:120]
        if (
            self._requires_approval(assignment)
            and approval_operation not in monitor.approved_operations
        ):
            approval = await self.store.create_approval(
                monitor.monitor_id,
                operation=approval_operation,
                reason="This assignment may perform an external or destructive action.",
            )
            monitor.approval_id = approval.approval_id
            monitor.status = MonitorStatus.APPROVAL_REQUIRED
            await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)
            await self._append_evidence(
                monitor,
                scope,
                "approval_requested",
                {
                    "approval_id": approval.approval_id,
                    "operation": approval.operation,
                    "reason": approval.reason,
                },
            )
            return
        if not await self.store.claim_workspace(monitor.workspace_id, monitor.monitor_id):
            scope.next_action = "Waiting for the workspace writer lease to be released."
            return
        delegated = False
        try:
            await self._delegate(monitor, scope, assignment)
            delegated = (
                await self._monitor_is_running(monitor.monitor_id)
                and scope.status == ScopeStatus.WORKING
            )
        except Exception:  # noqa: BLE001 - a worker provider failure must be persisted
            await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)
            scope.attempt_count += 1
            failure = {
                "category": "worker_start_failure",
                "scope_id": scope.scope_id,
                "message": "worker could not be started",
                "signature": normalize_failure_signature(
                    {"category": "worker_start_failure", "scope_id": scope.scope_id}
                ),
            }
            scope.failure_evidence.append(failure)
            signature = failure["signature"]
            signature_attempt = scope.failure_signature_counts.get(signature, 0) + 1
            scope.failure_signature_counts[signature] = signature_attempt
            failure["signature_attempt"] = signature_attempt
            await self._append_evidence(monitor, scope, "failure", failure)
            if scope.attempt_count >= self.max_attempts or signature_attempt >= self.max_attempts:
                scope.transition(ScopeStatus.BLOCKED)
                monitor.status = MonitorStatus.BLOCKED
                await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)
            else:
                scope.transition(ScopeStatus.REPAIR_REQUIRED)
                escalation = {
                    1: "normal repair",
                    2: "explicit root-cause re-analysis",
                    3: "alternative implementation strategy",
                    4: "independent verification/reviewer strategy",
                }.get(signature_attempt, "escalated repair")
                scope.next_action = (
                    f"[{escalation}] Resolve the worker-start failure and retry the assignment."
                )
        finally:
            if not delegated:
                await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)

    async def _delegate(self, monitor: MonitorState, scope: ScopeRecord, assignment: str) -> None:
        if not await self._monitor_is_running(monitor.monitor_id):
            return
        scope.transition(ScopeStatus.ASSIGNED)
        key = f"{monitor.monitor_id}:{scope.scope_id}:{scope.attempt_count + 1}"
        scoped_assignment = (
            f"Work only in the CPTR workspace identified by {monitor.workspace_id}. "
            "workspace_scope=current. "
            "inspection_scope=assignment. "
            "Do not search, read, modify, or verify other workspaces or historical fixtures "
            "unless the original goal explicitly requires cross-workspace work. "
            "Only inspect files named by this assignment or created during this monitor.\n\n"
            f"Assignment: {assignment}"
        )
        task = await self.agent.start_task(
            user_id=monitor.user_id,
            workspace_id=monitor.workspace_id,
            prompt=scoped_assignment,
            model_id=monitor.model_id,
            idempotency_key=key,
        )
        if str(task.get("status") or "").upper() in {"FAILED", "ERROR", "CANCELLED"}:
            raise RuntimeError("idempotent worker task is already terminal and unsuccessful")
        task_id = str(task["id"])
        if not await self._monitor_is_running(monitor.monitor_id):
            cancel_task = getattr(self.agent, "cancel_task", None)
            if callable(cancel_task):
                await cancel_task(task_id, user_id=monitor.user_id)
            return
        if task_id not in scope.worker_task_ids:
            scope.worker_task_ids.append(task_id)
        scope.transition(ScopeStatus.WORKING)

    async def _run_final_gate(self, monitor: MonitorState) -> MonitorState:
        if not await self._monitor_is_running(monitor.monitor_id):
            return await self._required_monitor(monitor.monitor_id)

        # This is a second, final invariant check.  It protects the terminal
        # goal transition even if a future code path or persisted state marks a
        # scope VERIFIED without passing the criterion gate above.  Director
        # output is never authoritative for mandatory semantic evidence.
        for scope in monitor.scopes:
            records = await self._steering_provenance_records(scope)
            failure = self._required_steering_evidence_failure(
                scope,
                records,
                scope.worker_task_ids[-1] if scope.worker_task_ids else None,
            )
            if failure is None:
                continue
            await self._append_evidence(
                monitor,
                scope,
                "criterion_evidence",
                {
                    "status": "UNSATISFIED",
                    "required": "same_worker_steering",
                    "message": failure,
                    "steering_provenance": records,
                },
            )
            monitor.status = MonitorStatus.RUNNING
            scope.transition(ScopeStatus.REPAIR_REQUIRED)
            await self._repair_or_block(
                monitor,
                scope,
                {
                    "category": "mandatory_criterion_evidence_missing",
                    "scope_id": scope.scope_id,
                    "message": failure,
                },
            )
            return await self._save_and_return(monitor)
        try:
            decision = await self.director.final_gate(
                monitor=monitor,
                scopes=monitor.scopes,
                original_goal=monitor.original_goal,
                original_acceptance_criteria=monitor.original_acceptance_criteria,
            )
            if not await self._monitor_is_running(monitor.monitor_id):
                return await self._required_monitor(monitor.monitor_id)
        except Exception:  # noqa: BLE001 - preserve a retryable state across provider outages
            monitor.status = MonitorStatus.RUNNING
            if monitor.scopes:
                scope = monitor.scopes[0]
                scope.failure_evidence.append(
                    {
                        "category": "director_failure",
                        "message": "final gate could not be evaluated",
                    }
                )
                scope.next_action = "Retry the final gate after the supervisor director recovers."
                scope.transition(ScopeStatus.REPAIR_REQUIRED)
            return await self._save_and_return(monitor)
        self._sync_director_state(monitor)
        await self._append_evidence(monitor, None, "final_gate", decision.__dict__.copy())
        if decision.goal_satisfied and not decision.defects and not decision.regressions:
            monitor.status = MonitorStatus.COMPLETE
            await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)
            return await self._save_and_return(monitor)
        monitor.status = MonitorStatus.RUNNING
        scope = monitor.scopes[0]
        await self._repair_or_block(
            monitor,
            scope,
            {
                "category": "final_gate_failure",
                "scope_id": scope.scope_id,
                "message": "; ".join(decision.defects + decision.regressions)
                or "final gate did not accept the original goal",
            },
            decision=decision,
        )
        return await self._save_and_return(monitor)

    @staticmethod
    def _requires_same_worker_steering(scope: ScopeRecord) -> bool:
        """Return whether the immutable criterion explicitly requires steering proof.

        This deliberately recognizes semantic requirement language rather than
        relying on worker prose or on the director.  Ordinary repository
        investigation criteria remain unaffected; a criterion must mention
        steering/control and a consumption/effect/same-worker requirement.
        """
        text = " ".join(scope.acceptance_criteria).lower()
        has_control = bool(
            re.search(r"\b(?:steering|steer|control\s+message|autonomous\s+control)\b", text)
        )
        has_semantic_requirement = bool(
            re.search(
                r"\b(?:same\s+worker|intended\s+worker|consum\w*|effect[_\s-]*observed|post[-\s]?control|after\s+consum)",
                text,
            )
        )
        return has_control and has_semantic_requirement

    @classmethod
    def _required_steering_evidence_failure(
        cls,
        scope: ScopeRecord,
        records: list[dict[str, Any]],
        task_id: str | None,
    ) -> str | None:
        """Validate deterministic evidence for a steering-specific criterion."""
        if not cls._requires_same_worker_steering(scope):
            return None
        if not records:
            return "required steering control was never recorded"
        for record in records:
            control_id = record.get("control_message_id")
            if not control_id:
                return "required steering control_message_id is missing"
            if record.get("status") != "CONSUMED":
                return f"steering control {control_id} did not reach CONSUMED"
            intended_task_id = record.get("intended_task_id")
            consumed_task_id = record.get("consumed_task_id")
            if not intended_task_id or not consumed_task_id:
                return f"steering control {control_id} is missing task provenance"
            if intended_task_id != consumed_task_id:
                return f"steering control {control_id} was consumed by the wrong worker"
            if task_id is not None and consumed_task_id != task_id:
                return f"steering control {control_id} was not consumed by the completed worker"
            if not record.get("consumed_message_id") or record.get("consumed_at") is None:
                return f"steering control {control_id} is missing consumption evidence"
            baseline = record.get("baseline_workspace_snapshot")
            baseline_fingerprint = record.get("baseline_workspace_fingerprint") or (
                baseline.get("fingerprint") if isinstance(baseline, dict) else None
            )
            post_fingerprint = record.get("post_consumption_workspace_fingerprint")
            if not baseline_fingerprint or not post_fingerprint:
                return f"steering control {control_id} is missing workspace fingerprints"
            if record.get("effect_status") != "EFFECT_OBSERVED":
                return f"steering control {control_id} has no EFFECT_OBSERVED evidence"
            observed_at = record.get("effect_observed_at")
            consumed_at = record.get("consumed_at")
            if not isinstance(observed_at, (int, float)) or not isinstance(
                consumed_at, (int, float)
            ):
                return f"steering control {control_id} is missing effect timing evidence"
            if observed_at <= consumed_at:
                return f"steering control {control_id} has no post-consumption effect"
            if not record.get("effect_changed_paths"):
                return f"steering control {control_id} has no qualifying workspace change"
        return None

    @staticmethod
    def _requires_approval(assignment: str) -> bool:
        for pattern in APPROVAL_PATTERNS:
            for match in pattern.finditer(assignment):
                prefix = assignment[max(0, match.start() - 120) : match.start()]
                # Treat a comma-separated negative list as one prohibition,
                # while keeping a later positive sentence actionable.
                clause = re.split(r"[.!?;\n]", prefix)[-1]
                negative = re.search(
                    r"\b(?:do\s+not|don['’]?t|never|without)\b",
                    clause,
                    re.IGNORECASE,
                )
                if negative and not re.search(
                    r"\b(?:but|except|however|then)\b",
                    clause[negative.end() :],
                    re.IGNORECASE,
                ):
                    continue
                return True
        return False

    async def _observe_steering_effects(
        self,
        monitor: MonitorState,
        scope: ScopeRecord,
        task_id: str,
        records: list[dict[str, Any]],
    ) -> str | None:
        """Attribute a post-consumption workspace change to the intended worker."""
        get_workspace_fingerprint = getattr(self.agent, "get_workspace_fingerprint", None)
        for record in records:
            if record.get("effect_status") == "EFFECT_OBSERVED":
                continue
            if record.get("effect_status") == "EFFECT_NOT_OBSERVED":
                return "The intended worker consumed steering but produced no qualifying workspace effect."
            baseline = record.get("baseline_workspace_snapshot")
            if not baseline:
                # Older steering records only have the Git diff fallback. Preserve
                # their established behavior while all new records use snapshots.
                if record.get("baseline_diff_fingerprint"):
                    current_diff = await self.agent.get_diff(
                        monitor.workspace_id, user_id=monitor.user_id
                    )
                    current_fingerprint = hashlib.sha256(
                        json.dumps(
                            redact_sensitive(current_diff), sort_keys=True, default=str
                        ).encode("utf-8")
                    ).hexdigest()
                    if current_fingerprint == record["baseline_diff_fingerprint"]:
                        self._set_steering_effect(
                            scope,
                            record["control_message_id"],
                            status="EFFECT_NOT_OBSERVED",
                            observed_at=int(time.time() * 1000),
                        )
                        return "The intended worker consumed steering but produced no new workspace evidence."
                continue
            if record.get("consumed_task_id") != record.get("intended_task_id"):
                return "Steering was not consumed by the intended worker task."
            if task_id != record.get("intended_task_id"):
                self._set_steering_effect(
                    scope,
                    record["control_message_id"],
                    status="EFFECT_NOT_OBSERVED",
                    observed_at=int(time.time() * 1000),
                )
                return "A replacement worker cannot satisfy the same-worker steering effect."
            if not callable(get_workspace_fingerprint):
                return "Workspace content evidence is unavailable for steering effect attribution."
            post = await get_workspace_fingerprint(monitor.workspace_id, user_id=monitor.user_id)
            post = redact_sensitive(post)
            observed_at = int(time.time() * 1000)
            baseline_fingerprint = baseline.get("fingerprint")
            post_fingerprint = post.get("fingerprint") if isinstance(post, dict) else None
            if not baseline_fingerprint or not post_fingerprint:
                self._set_steering_effect(
                    scope,
                    record["control_message_id"],
                    status="EFFECT_NOT_OBSERVED",
                    post_snapshot=post,
                    observed_at=observed_at,
                )
                return "Steering effect snapshots were incomplete."
            if baseline_fingerprint == post_fingerprint:
                self._set_steering_effect(
                    scope,
                    record["control_message_id"],
                    status="EFFECT_NOT_OBSERVED",
                    post_snapshot=post,
                    observed_at=observed_at,
                )
                return "The intended worker consumed steering but produced no qualifying workspace effect."
            self._set_steering_effect(
                scope,
                record["control_message_id"],
                status="EFFECT_OBSERVED",
                post_snapshot=post,
                observed_at=observed_at,
                changed=changed_paths(baseline, post),
            )
        return None

    @staticmethod
    def _set_steering_effect(
        scope: ScopeRecord,
        control_message_id: str,
        *,
        status: str,
        post_snapshot: dict[str, Any] | None = None,
        observed_at: int | None = None,
        changed: list[str] | None = None,
    ) -> None:
        for record in scope.steering_requests:
            if record.get("control_message_id") != control_message_id:
                continue
            record["effect_status"] = status
            record["effect_observed_at"] = observed_at
            if post_snapshot is not None:
                record["post_consumption_workspace_snapshot"] = post_snapshot
                record["post_consumption_workspace_fingerprint"] = post_snapshot.get("fingerprint")
            if changed is not None:
                record["effect_changed_paths"] = changed
            return

    async def _append_evidence(
        self, monitor: MonitorState, scope: ScopeRecord | None, kind: str, payload: dict[str, Any]
    ) -> None:
        await self.store.append_evidence(
            monitor.monitor_id,
            scope.scope_id if scope else None,
            kind,
            payload,
        )

    async def _steering_provenance(self, scope: ScopeRecord) -> dict[str, Any] | None:
        records = await self._steering_provenance_records(scope)
        return records[-1] if records else None

    async def _steering_provenance_records(self, scope: ScopeRecord) -> list[dict[str, Any]]:
        if not scope.steering_requests:
            return []
        get_message = getattr(self.store, "get_message", None)
        if not callable(get_message):
            return [{**request, "status": "UNKNOWN"} for request in scope.steering_requests]
        records = []
        for request in scope.steering_requests:
            message = await get_message(request["control_message_id"])
            if message is None:
                records.append({**request, "status": "MISSING"})
            else:
                status = str(getattr(message, "status", "UNKNOWN")).upper()
                request["status"] = status
                request["consumed_task_id"] = getattr(message, "consumed_task_id", None)
                request["consumed_message_id"] = getattr(message, "consumed_message_id", None)
                request["consumed_at"] = getattr(message, "consumed_at", None)
                records.append(
                    {
                        **request,
                        "status": status,
                    }
                )
        return records

    def _sync_director_state(self, monitor: MonitorState) -> None:
        state_for = getattr(self.director, "state_for", None)
        if callable(state_for):
            state = state_for(monitor.monitor_id)
            if isinstance(state, dict):
                monitor.director_state.update(state)

    async def _save_and_return(self, monitor: MonitorState) -> MonitorState:
        await self.store.save_monitor(monitor)
        return monitor

    async def _required_monitor(self, monitor_id: str) -> MonitorState:
        monitor = await self.store.get_monitor(monitor_id)
        if monitor is None:
            raise KeyError(f"monitor not found: {monitor_id}")
        return monitor
