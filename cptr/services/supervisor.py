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

from cptr.services.verification import DefaultIndependentVerifier, IndependentVerifier

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
APPROVAL_PATTERNS = (
    re.compile(r"\bgit\s+push\b", re.IGNORECASE),
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

    async def append_evidence(
        self, monitor_id: str, scope_id: str | None, kind: str, payload: dict[str, Any]
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            evidence_id=f"evidence_{uuid.uuid4().hex[:20]}",
            monitor_id=monitor_id,
            scope_id=scope_id,
            kind=kind,
            payload=dict(payload),
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
        await self.store.release_workspace(monitor.workspace_id, monitor.monitor_id)
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
        await self._append_evidence(monitor, scope, "worker_state", task)
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
            decision = await self.director.evaluate(
                monitor=monitor,
                scope=scope,
                evidence=evidence,
                original_goal=monitor.original_goal,
                original_acceptance_criteria=monitor.original_acceptance_criteria,
            )
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
        self._sync_director_state(monitor)
        scope.last_decision = decision.__dict__.copy()
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
        if str(task.get("status") or "").upper() in {"FAILED", "ERROR", "CANCELLED"}:
            raise RuntimeError("idempotent worker task is already terminal and unsuccessful")
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
    def _requires_approval(assignment: str) -> bool:
        return any(pattern.search(assignment) for pattern in APPROVAL_PATTERNS)

    async def _append_evidence(
        self, monitor: MonitorState, scope: ScopeRecord | None, kind: str, payload: dict[str, Any]
    ) -> None:
        await self.store.append_evidence(
            monitor.monitor_id,
            scope.scope_id if scope else None,
            kind,
            payload,
        )

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
