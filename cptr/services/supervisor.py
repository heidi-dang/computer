"""Durable-supervisor domain contracts and the resumable monitor loop.

The persistence adapter is intentionally injected.  This keeps the lifecycle
rules independently testable and lets the HTTP/API layer share the same
state machine without introducing another worker engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

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
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPLETE = "COMPLETE"


TERMINAL_TASK_STATUSES = {"COMPLETE", "COMPLETED", "SUCCEEDED", "FAILED", "ERROR", "CANCELLED"}


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
    verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    failure_evidence: list[dict[str, Any]] = field(default_factory=list)
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
    director_state: dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))


class SupervisorStore(Protocol):
    async def create_monitor(
        self, monitor: MonitorState, idempotency_key: str | None
    ) -> MonitorState: ...

    async def get_monitor(self, monitor_id: str) -> MonitorState | None: ...

    async def save_monitor(self, monitor: MonitorState) -> None: ...

    async def claim_monitor(self, monitor_id: str) -> bool: ...

    async def release_monitor(self, monitor_id: str) -> None: ...


class InMemorySupervisorStore:
    """Small deterministic store used by unit tests and local service wiring."""

    def __init__(self) -> None:
        self.monitors: dict[str, MonitorState] = {}
        self.idempotency: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
        monitor.updated_at = int(time.time() * 1000)
        self.monitors[monitor.monitor_id] = monitor

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


class SupervisorAgent(Protocol):
    async def start_task(self, **kwargs: Any) -> dict[str, Any]: ...

    async def get_task(self, task_id: str, **kwargs: Any) -> dict[str, Any]: ...

    async def get_output(self, task_id: str, **kwargs: Any) -> dict[str, Any]: ...

    async def get_diff(self, workspace_id: str, **kwargs: Any) -> dict[str, Any]: ...

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
        max_attempts: int = 5,
    ) -> None:
        self.store = store
        self.agent = agent
        self.director = director
        self.max_attempts = max(1, max_attempts)

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

    async def approve(self, monitor_id: str, *, approval_id: str, approved: bool) -> MonitorState:
        monitor = await self._required_monitor(monitor_id)
        if monitor.status != MonitorStatus.APPROVAL_REQUIRED or monitor.approval_id != approval_id:
            raise ValueError("approval request is no longer pending")
        monitor.approval_id = None
        monitor.status = MonitorStatus.RUNNING if approved else MonitorStatus.BLOCKED
        await self.store.save_monitor(monitor)
        return monitor

    async def cancel(self, monitor_id: str) -> MonitorState:
        monitor = await self._required_monitor(monitor_id)
        monitor.status = MonitorStatus.CANCELLED
        for scope in monitor.scopes:
            if scope.status not in {ScopeStatus.VERIFIED, ScopeStatus.CANCELLED}:
                task_id = scope.worker_task_ids[-1] if scope.worker_task_ids else None
                cancel_task = getattr(self.agent, "cancel_task", None)
                if task_id and callable(cancel_task):
                    try:
                        await cancel_task(task_id, user_id=monitor.user_id)
                    except Exception:  # noqa: BLE001 - cancellation remains durable
                        logger.warning("worker cancellation failed for task %s", task_id)
                scope.transition(ScopeStatus.CANCELLED)
        await self.store.save_monitor(monitor)
        return monitor

    async def run_once(self, monitor_id: str) -> MonitorState:
        if not await self.store.claim_monitor(monitor_id):
            monitor = await self._required_monitor(monitor_id)
            return monitor
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
            await self.store.release_monitor(monitor_id)

    async def _observe_and_verify(self, monitor: MonitorState, scope: ScopeRecord) -> None:
        task_id = scope.worker_task_ids[-1] if scope.worker_task_ids else None
        if not task_id:
            scope.transition(ScopeStatus.REPAIR_REQUIRED)
            scope.next_action = "Delegate the scope to a worker."
            return
        task = await self.agent.get_task(task_id, user_id=monitor.user_id)
        task_status = str(task.get("status") or "").upper()
        if task_status not in TERMINAL_TASK_STATUSES:
            scope.transition(ScopeStatus.WORKING)
            return
        if task_status in {"FAILED", "ERROR", "CANCELLED"}:
            await self._repair_or_block(
                monitor, scope, {"category": "worker_failure", "message": task.get("error")}
            )
            return

        scope.transition(ScopeStatus.AGENT_COMPLETE)
        scope.transition(ScopeStatus.VERIFYING)
        evidence = {
            "task": await self.agent.get_output(task_id, user_id=monitor.user_id),
            "diff": await self.agent.get_diff(monitor.workspace_id, user_id=monitor.user_id),
        }
        try:
            decision = await self.director.evaluate(
                monitor=monitor,
                scope=scope,
                evidence=evidence,
                original_goal=monitor.original_goal,
                original_acceptance_criteria=monitor.original_acceptance_criteria,
            )
        except Exception:  # noqa: BLE001 - preserve a retryable state across provider outages
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
        self._sync_director_state(monitor)
        scope.last_decision = decision.__dict__.copy()
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
        if scope.attempt_count >= self.max_attempts:
            scope.transition(ScopeStatus.BLOCKED)
            monitor.status = MonitorStatus.BLOCKED
            return
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
        except Exception:  # noqa: BLE001 - retry once the director is available again
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
        scope.transition(ScopeStatus.REPAIR_REQUIRED)
        await self._try_delegate(monitor, scope, scope.next_action)

    async def _try_delegate(
        self, monitor: MonitorState, scope: ScopeRecord, assignment: str
    ) -> None:
        try:
            await self._delegate(monitor, scope, assignment)
        except Exception:  # noqa: BLE001 - a worker provider failure must be persisted
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
            if scope.attempt_count >= self.max_attempts:
                scope.transition(ScopeStatus.BLOCKED)
                monitor.status = MonitorStatus.BLOCKED
            else:
                scope.transition(ScopeStatus.REPAIR_REQUIRED)
                scope.next_action = "Resolve the worker-start failure and retry the assignment."

    async def _delegate(self, monitor: MonitorState, scope: ScopeRecord, assignment: str) -> None:
        scope.transition(ScopeStatus.ASSIGNED)
        key = f"{monitor.monitor_id}:{scope.scope_id}:{scope.attempt_count + 1}"
        task = await self.agent.start_task(
            user_id=monitor.user_id,
            workspace_id=monitor.workspace_id,
            prompt=assignment,
            model_id=monitor.model_id,
            idempotency_key=key,
        )
        task_id = str(task["id"])
        if task_id not in scope.worker_task_ids:
            scope.worker_task_ids.append(task_id)
        scope.transition(ScopeStatus.WORKING)

    async def _run_final_gate(self, monitor: MonitorState) -> MonitorState:
        try:
            decision = await self.director.final_gate(
                monitor=monitor,
                scopes=monitor.scopes,
                original_goal=monitor.original_goal,
                original_acceptance_criteria=monitor.original_acceptance_criteria,
            )
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
        if decision.goal_satisfied and not decision.defects and not decision.regressions:
            monitor.status = MonitorStatus.COMPLETE
            return monitor
        monitor.status = MonitorStatus.RUNNING
        monitor.scopes[0].transition(ScopeStatus.REPAIR_REQUIRED)
        monitor.scopes[0].next_action = decision.next_assignment or "Repair the final-gate failure."
        return await self._save_and_return(monitor)

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
