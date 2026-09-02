"""Machine-owned phase handlers for recovery, repair, Victory, and cycle completion."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from cptr.services.factory_domain import FactoryState
from cptr.services.factory_gates import (
    EvidenceAuthority,
    FactoryGateCategory,
    FactoryGatePlan,
    FactoryGateSpec,
    FactoryGateStatus,
    GateEvidence,
    GateResult,
)
from cptr.services.factory_victory import FactoryVictoryJudge

from .types import (
    PhaseContext,
    PhaseFailure,
    PhaseFailureCategory,
    PhaseOutcome,
)


class RecoveryPhaseHandler:
    """Resume only the persisted interrupted state; never infer prior success."""

    def __init__(self, *, reconciler: Callable[[Any], Awaitable[Any]] | None = None) -> None:
        self._reconciler = reconciler

    async def execute(self, context: PhaseContext) -> PhaseOutcome:
        if self._reconciler is not None:
            await self._reconciler(context.run)
        if context.run.resumable_state:
            target = FactoryState(context.run.resumable_state)
            reason = f"recovery reconciled; resume persisted state {target.value}"
        else:
            target = FactoryState.BASELINING
            reason = "initial recovery reconciliation complete; begin baselining"
        return PhaseOutcome(next_state=target, reason=reason)


class RepairRequiredPhaseHandler:
    """Choose the smallest repair entry and escalate repeated signatures."""

    def __init__(self, *, repeated_failure_threshold: int = 2) -> None:
        if repeated_failure_threshold <= 1:
            raise ValueError("repeated failure threshold must exceed one")
        self._threshold = int(repeated_failure_threshold)

    async def execute(self, context: PhaseContext) -> PhaseOutcome:
        signatures = context.cycle.failure_signatures or {}
        if not signatures:
            return PhaseOutcome(
                next_state=FactoryState.ROOT_CAUSE_ANALYSIS,
                reason="repair requested without a classified failure; diagnose before mutation",
            )
        latest = max(
            signatures.values(),
            key=lambda item: (int(item.get("last_seen_at") or 0), str(item.get("signature") or "")),
        )
        count = int(latest.get("count") or 0)
        category = str(latest.get("category") or PhaseFailureCategory.UNKNOWN.value)
        configured_budget = None
        if isinstance(context.run.budget, dict):
            raw_budget = context.run.budget.get("max_repair_attempts_per_signature")
            if raw_budget is not None:
                try:
                    configured_budget = int(raw_budget)
                except (TypeError, ValueError):
                    configured_budget = 0
                if configured_budget <= 0:
                    return PhaseOutcome(
                        next_state=FactoryState.BLOCKED,
                        reason="configured per-signature repair budget is invalid; fail closed",
                    )
        if configured_budget is not None and count >= configured_budget:
            return PhaseOutcome(
                next_state=FactoryState.BLOCKED,
                reason=(
                    "per-signature repair budget exhausted after "
                    f"{count} attempts (configured maximum {configured_budget})"
                ),
            )
        if count >= self._threshold or category == PhaseFailureCategory.CAPABILITY.value:
            return PhaseOutcome(
                next_state=FactoryState.CAPABILITY_ANALYSIS,
                reason=(
                    "repeated/ capability-related failure requires capability re-analysis "
                    "instead of another blind implementation retry"
                ),
            )
        return PhaseOutcome(
            next_state=FactoryState.ROOT_CAUSE_ANALYSIS,
            reason="first classified failure requires independent root-cause analysis",
        )


def _gate_plan(value: dict[str, Any]) -> FactoryGatePlan:
    if not isinstance(value, dict):
        raise ValueError("factory cycle gate plan must be an object")
    specs: list[FactoryGateSpec] = []
    for item in value.get("specs") or []:
        if not isinstance(item, dict):
            raise ValueError("factory gate plan spec must be an object")
        specs.append(
            FactoryGateSpec(
                gate_id=str(item.get("gate_id") or ""),
                category=FactoryGateCategory(str(item.get("category") or "")),
                required=bool(item.get("required", True)),
                applicable=bool(item.get("applicable", True)),
                applicability_reason=(
                    str(item.get("applicability_reason"))
                    if item.get("applicability_reason") is not None
                    else None
                ),
                invalidated_by_mutation=bool(item.get("invalidated_by_mutation", True)),
                acceptance_ids=tuple(str(value) for value in item.get("acceptance_ids") or ()),
            )
        )
    return FactoryGatePlan(
        specs=tuple(specs),
        acceptance_criterion_ids=tuple(
            str(item) for item in value.get("acceptance_criterion_ids") or ()
        ),
    )


class VictoryJudgingPhaseHandler:
    """Use only persisted machine gate/evidence state to issue Victory."""

    def __init__(self, *, judge: FactoryVictoryJudge | None = None) -> None:
        self._judge = judge or FactoryVictoryJudge()

    async def execute(self, context: PhaseContext) -> PhaseOutcome:
        plan = _gate_plan(context.cycle.gate_plan or {})
        expected_acceptance_ids = tuple(
            f"criterion-{index}"
            for index, _criterion in enumerate(context.run.acceptance_criteria or (), start=1)
        )
        if plan.acceptance_criterion_ids != expected_acceptance_ids:
            return PhaseOutcome(
                reason="Victory blocked because the persisted gate plan does not match immutable run acceptance criteria",
                failure=PhaseFailure(
                    category=PhaseFailureCategory.TEST,
                    code="VICTORY_ACCEPTANCE_PLAN_MISMATCH",
                    gate_id="victory",
                    summary=(
                        "gate plan acceptance identities differ from immutable run criteria: "
                        f"expected={expected_acceptance_ids!r}, "
                        f"actual={plan.acceptance_criterion_ids!r}"
                    )[:4_000],
                ),
            )
        evidence = {
            row.id: GateEvidence(
                evidence_id=row.id,
                digest=row.digest,
                authority=EvidenceAuthority(row.authority),
                revision=row.revision,
                fingerprint=row.fingerprint,
                kind=row.kind,
                source=row.source,
            )
            for row in context.evidence
        }
        latest: dict[str, Any] = {}
        for row in context.gates:
            current = latest.get(row.gate_id)
            if current is None or int(row.attempt) > int(current.attempt):
                latest[row.gate_id] = row
        gate_results = {
            gate_id: GateResult(
                gate_id=gate_id,
                status=FactoryGateStatus(row.status),
                evidence_ids=tuple(str(item) for item in row.evidence_ids or ()),
                reason=row.reason or "",
                evaluated_revision=row.evaluated_revision,
                evaluated_fingerprint=row.evaluated_fingerprint,
            )
            for gate_id, row in latest.items()
        }
        decision = self._judge.evaluate(
            gate_plan=plan,
            gate_results=gate_results,
            evidence=evidence,
            current_revision=context.cycle.target_revision,
            current_fingerprint=context.cycle.target_fingerprint,
        )
        if decision.passed:
            return PhaseOutcome(
                next_state=FactoryState.COMMITTING,
                reason="all required machine gates satisfy Victory",
                victory_decision=decision,
            )
        return PhaseOutcome(
            reason="Victory blocked by missing, failed, or stale machine evidence",
            failure=PhaseFailure(
                category=PhaseFailureCategory.SECURITY
                if any("security/adversarial" in item for item in decision.failures)
                else PhaseFailureCategory.TEST,
                code="VICTORY_GATE_FAILURE",
                gate_id="victory",
                summary="; ".join(decision.failures)[:4_000] or "Victory gate evaluation failed",
            ),
        )


class CycleCompletePhaseHandler:
    """Close a bounded run or start another audit cycle according to run policy."""

    async def execute(self, context: PhaseContext) -> PhaseOutcome:
        configured = context.run.policy.get("max_cycles") if isinstance(context.run.policy, dict) else None
        try:
            max_cycles = int(configured) if configured is not None else 1
        except (TypeError, ValueError):
            max_cycles = 1
        max_cycles = max(1, max_cycles)
        if int(context.cycle.ordinal) >= max_cycles:
            return PhaseOutcome(
                next_state=FactoryState.COMPLETE,
                reason="configured factory cycle budget is satisfied",
            )
        return PhaseOutcome(
            next_state=FactoryState.AUDITING,
            reason="cycle complete and configured cycle budget permits another audit",
        )
