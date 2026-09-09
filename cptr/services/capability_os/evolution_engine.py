"""Durable evidence-backed experiment lifecycle for Capability OS evolution.

Experiments reuse the existing tamper-evident evidence chain instead of adding a
second state store.  Each experiment is server-identified, binds exact control
and candidate artifact digests plus a matched execution context, and derives arm
summaries from immutable per-run observations.  Evaluation never mutates an
artifact; promotion remains an explicit control-plane operation.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from cptr.services.capability_os.evidence import EvidenceService
from cptr.services.capability_os.evolution import (
    ChangeClass,
    EvolutionGate,
    ExperimentArm,
    ExperimentComparison,
    PromotionDecision,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore


class EvolutionExperimentError(ValueError):
    pass


class ExperimentMode(str, Enum):
    SHADOW = "shadow"
    REPLAY = "replay"


class ExperimentArmName(str, Enum):
    CONTROL = "control"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class ExperimentContext:
    model_id: str
    reasoning_effort: str
    tool_permission_fingerprint: str
    resource_budget_fingerprint: str
    task_distribution_fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "model_id",
            "reasoning_effort",
            "tool_permission_fingerprint",
            "resource_budget_fingerprint",
            "task_distribution_fingerprint",
        ):
            value = str(getattr(self, name)).strip()
            if not value or len(value) > 512:
                raise EvolutionExperimentError(f"experiment context {name} must be bounded and non-empty")
            object.__setattr__(self, name, value)

    @classmethod
    def from_dict(cls, value: Any) -> "ExperimentContext":
        if not isinstance(value, dict):
            raise EvolutionExperimentError("experiment context must be an object")
        unknown = set(value) - {
            "modelId",
            "reasoningEffort",
            "toolPermissionFingerprint",
            "resourceBudgetFingerprint",
            "taskDistributionFingerprint",
        }
        if unknown:
            raise EvolutionExperimentError(f"unknown experiment context field: {sorted(unknown)[0]}")
        return cls(
            model_id=str(value.get("modelId") or ""),
            reasoning_effort=str(value.get("reasoningEffort") or ""),
            tool_permission_fingerprint=str(value.get("toolPermissionFingerprint") or ""),
            resource_budget_fingerprint=str(value.get("resourceBudgetFingerprint") or ""),
            task_distribution_fingerprint=str(value.get("taskDistributionFingerprint") or ""),
        )

    def to_api(self) -> dict[str, str]:
        return {
            "modelId": self.model_id,
            "reasoningEffort": self.reasoning_effort,
            "toolPermissionFingerprint": self.tool_permission_fingerprint,
            "resourceBudgetFingerprint": self.resource_budget_fingerprint,
            "taskDistributionFingerprint": self.task_distribution_fingerprint,
        }


@dataclass(frozen=True)
class ExperimentPlan:
    experiment_id: str
    change_class: ChangeClass
    mode: ExperimentMode
    hypothesis: str
    control_artifact_digest: str
    candidate_artifact_digest: str
    context: ExperimentContext
    min_runs_per_arm: int

    def to_api(self) -> dict[str, Any]:
        return {
            "experimentId": self.experiment_id,
            "changeClass": self.change_class.value,
            "mode": self.mode.value,
            "hypothesis": self.hypothesis,
            "controlArtifactDigest": self.control_artifact_digest,
            "candidateArtifactDigest": self.candidate_artifact_digest,
            "context": self.context.to_api(),
            "minRunsPerArm": self.min_runs_per_arm,
        }


@dataclass(frozen=True)
class ExperimentObservation:
    evidence_id: str
    source_evidence_id: str
    source_evidence_digest: str
    arm: ExperimentArmName
    success: bool
    regression: bool
    safety_events: int
    cost: float

    def __post_init__(self) -> None:
        if self.safety_events < 0 or self.safety_events > 1000:
            raise EvolutionExperimentError("experiment safetyEvents must be within [0, 1000]")
        if self.cost < 0 or self.cost > 1_000_000_000:
            raise EvolutionExperimentError("experiment cost must be bounded and non-negative")


@dataclass(frozen=True)
class ExperimentEvaluation:
    decision: PromotionDecision
    control: ExperimentArm
    candidate: ExperimentArm
    observation_evidence_ids: tuple[str, ...]
    evaluation_evidence_id: str | None = None

    def to_api(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "control": asdict(self.control),
            "candidate": asdict(self.candidate),
            "observationEvidenceIds": list(self.observation_evidence_ids),
            "evaluationEvidenceId": self.evaluation_evidence_id,
        }


class EvolutionExperimentEngine:
    _CREATED = "evolution.experiment.created"
    _OBSERVATION = "evolution.experiment.observation"
    _EVALUATED = "evolution.experiment.evaluated"
    _CANCELLED = "evolution.experiment.cancelled"
    _PROMOTION_INTENT = "evolution.experiment.promotion.intent"
    _PROMOTION = "evolution.experiment.promotion"

    def __init__(
        self,
        *,
        store: SqlCapabilityOsStore,
        evidence: EvidenceService,
        gate: EvolutionGate,
        max_runs_per_arm: int = 1000,
    ) -> None:
        if max_runs_per_arm < gate.min_runs_per_arm or max_runs_per_arm > 10_000:
            raise ValueError("max_runs_per_arm is outside supported bounds")
        self.store = store
        self.evidence = evidence
        self.gate = gate
        self.max_runs_per_arm = int(max_runs_per_arm)

    async def _rows(self, task_id: str, experiment_id: str):
        experiment_id = str(experiment_id).strip()
        if not experiment_id or len(experiment_id) > 200:
            raise EvolutionExperimentError("experimentId is invalid")
        return await self.store.list_evidence_for_run(
            task_id,
            experiment_id,
            limit=(self.max_runs_per_arm * 2) + 100,
        )

    @staticmethod
    def _parse_plan(row) -> ExperimentPlan:
        claims = dict(row.claims or {})
        return ExperimentPlan(
            experiment_id=str(row.run_id or ""),
            change_class=ChangeClass(str(claims.get("changeClass") or "")),
            mode=ExperimentMode(str(claims.get("mode") or "")),
            hypothesis=str(claims.get("hypothesis") or ""),
            control_artifact_digest=str(claims.get("controlArtifactDigest") or ""),
            candidate_artifact_digest=str(claims.get("candidateArtifactDigest") or ""),
            context=ExperimentContext.from_dict(claims.get("context")),
            min_runs_per_arm=int(claims.get("minRunsPerArm") or 0),
        )

    async def _load(self, task_id: str, experiment_id: str):
        rows = await self._rows(task_id, experiment_id)
        created = [row for row in rows if row.kind == self._CREATED]
        if len(created) != 1:
            if not created:
                raise KeyError("evolution experiment not found")
            raise EvolutionExperimentError("evolution experiment has ambiguous creation evidence")
        return self._parse_plan(created[0]), rows

    @staticmethod
    def _terminal(rows) -> bool:
        return any(row.kind in {EvolutionExperimentEngine._CANCELLED, EvolutionExperimentEngine._PROMOTION} for row in rows)

    async def create(
        self,
        *,
        task_id: str,
        change_class: ChangeClass,
        mode: ExperimentMode,
        hypothesis: str,
        control_artifact_digest: str,
        candidate_artifact_digest: str,
        context: ExperimentContext,
        min_runs_per_arm: int | None = None,
    ) -> tuple[ExperimentPlan, str]:
        hypothesis = str(hypothesis).strip()
        if not hypothesis or len(hypothesis) > 4000:
            raise EvolutionExperimentError("experiment hypothesis must be bounded and non-empty")
        control_digest = str(control_artifact_digest).strip()
        candidate_digest = str(candidate_artifact_digest).strip()
        if not control_digest or not candidate_digest:
            raise EvolutionExperimentError("experiment requires control and candidate artifact digests")
        requested_min = int(min_runs_per_arm or self.gate.min_runs_per_arm)
        if requested_min < self.gate.min_runs_per_arm or requested_min > self.max_runs_per_arm:
            raise EvolutionExperimentError("experiment minRunsPerArm is outside supported bounds")
        experiment_id = f"evoexp_{uuid.uuid4().hex}"
        plan = ExperimentPlan(
            experiment_id=experiment_id,
            change_class=change_class,
            mode=mode,
            hypothesis=hypothesis,
            control_artifact_digest=control_digest,
            candidate_artifact_digest=candidate_digest,
            context=context,
            min_runs_per_arm=requested_min,
        )
        row = await self.evidence.record(
            task_id=task_id,
            run_id=experiment_id,
            kind=self._CREATED,
            producer_identity="capability-os-control",
            claims={
                "version": 1,
                **{key: value for key, value in plan.to_api().items() if key != "experimentId"},
            },
            artifact_digest=candidate_digest,
        )
        await self.store.create_experiment_projection(
            experiment_id=experiment_id,
            task_id=task_id,
            change_class=change_class.value,
            mode=mode.value,
            hypothesis=hypothesis,
            control_artifact_digest=control_digest,
            candidate_artifact_digest=candidate_digest,
            context=context.to_api(),
            min_runs_per_arm=requested_min,
            created_at_ms=int(row.created_at_ms),
        )
        return plan, row.evidence_id

    @staticmethod
    def _server_outcome(source) -> tuple[bool, bool, int, float]:
        claims = dict(source.claims or {})
        outcome = claims.get("evolutionOutcome")
        if not isinstance(outcome, dict):
            raise EvolutionExperimentError(
                "experiment source evidence lacks server-owned evolutionOutcome"
            )
        unknown = set(outcome) - {"success", "regression", "safetyEvents", "cost"}
        if unknown:
            raise EvolutionExperimentError(
                f"unknown server evolution outcome field: {sorted(unknown)[0]}"
            )
        if set(outcome) != {"success", "regression", "safetyEvents", "cost"}:
            raise EvolutionExperimentError("server evolutionOutcome is incomplete")
        if not isinstance(outcome["success"], bool) or not isinstance(outcome["regression"], bool):
            raise EvolutionExperimentError("server evolutionOutcome booleans are invalid")
        if isinstance(outcome["safetyEvents"], bool) or not isinstance(outcome["safetyEvents"], int):
            raise EvolutionExperimentError("server evolutionOutcome safetyEvents is invalid")
        if isinstance(outcome["cost"], bool) or not isinstance(outcome["cost"], (int, float)):
            raise EvolutionExperimentError("server evolutionOutcome cost is invalid")
        safety_events = int(outcome["safetyEvents"])
        cost = float(outcome["cost"])
        if safety_events < 0 or safety_events > 1000:
            raise EvolutionExperimentError("experiment safetyEvents must be within [0, 1000]")
        if cost < 0 or cost > 1_000_000_000:
            raise EvolutionExperimentError("experiment cost must be bounded and non-negative")
        return bool(outcome["success"]), bool(outcome["regression"]), safety_events, cost

    async def observe(
        self,
        *,
        task_id: str,
        experiment_id: str,
        arm: ExperimentArmName,
        source_evidence_id: str,
    ) -> ExperimentObservation:
        plan, rows = await self._load(task_id, experiment_id)
        if self._terminal(rows):
            raise EvolutionExperimentError("evolution experiment is closed")
        observations = [row for row in rows if row.kind == self._OBSERVATION]
        if len([row for row in observations if (row.claims or {}).get("arm") == arm.value]) >= self.max_runs_per_arm:
            raise EvolutionExperimentError("evolution experiment arm reached the run limit")
        source_id = str(source_evidence_id).strip()
        if not source_id or len(source_id) > 200:
            raise EvolutionExperimentError("sourceEvidenceId is invalid")
        if any(str((row.claims or {}).get("sourceEvidenceId") or "") == source_id for row in observations):
            raise EvolutionExperimentError("source evidence is already counted in this experiment")
        source = await self.store.get_evidence(source_id)
        if source is None or source.task_id != task_id:
            raise KeyError("experiment source evidence not found")
        if source.producer_identity.startswith("capability-os-client:"):
            raise EvolutionExperimentError("experiment observations require server-produced source evidence")
        if source.kind.startswith("evolution.experiment."):
            raise EvolutionExperimentError("experiment lifecycle evidence cannot be used as an observation source")
        expected_digest = (
            plan.control_artifact_digest if arm is ExperimentArmName.CONTROL else plan.candidate_artifact_digest
        )
        if source.artifact_digest != expected_digest:
            raise EvolutionExperimentError("experiment source evidence belongs to the wrong artifact arm")
        success, regression, safety_events, cost = self._server_outcome(source)
        observation = ExperimentObservation(
            evidence_id="",
            source_evidence_id=source_id,
            source_evidence_digest=str(source.digest),
            arm=arm,
            success=success,
            regression=regression,
            safety_events=safety_events,
            cost=cost,
        )
        row = await self.evidence.record(
            task_id=task_id,
            run_id=plan.experiment_id,
            kind=self._OBSERVATION,
            producer_identity="capability-os-control",
            claims={
                "arm": arm.value,
                "sourceEvidenceId": source_id,
                "sourceEvidenceDigest": source.digest,
                "success": observation.success,
                "regression": observation.regression,
                "safetyEvents": observation.safety_events,
                "cost": observation.cost,
                "outcomeSource": "server-evidence",
            },
            artifact_digest=expected_digest,
        )
        await self.store.add_experiment_run_projection(
            experiment_id=plan.experiment_id,
            arm=arm.value,
            source_evidence_id=source_id,
            source_evidence_digest=str(source.digest),
            success=observation.success,
            regression=observation.regression,
            safety_events=observation.safety_events,
            cost=observation.cost,
            created_at_ms=int(row.created_at_ms),
        )
        return ExperimentObservation(
            evidence_id=row.evidence_id,
            source_evidence_id=observation.source_evidence_id,
            source_evidence_digest=observation.source_evidence_digest,
            arm=observation.arm,
            success=observation.success,
            regression=observation.regression,
            safety_events=observation.safety_events,
            cost=observation.cost,
        )

    @staticmethod
    def _arm(plan: ExperimentPlan, observations, arm: ExperimentArmName) -> ExperimentArm:
        selected = [row for row in observations if str((row.claims or {}).get("arm") or "") == arm.value]
        runs = len(selected)
        successes = sum(bool((row.claims or {}).get("success")) for row in selected)
        regressions = sum(bool((row.claims or {}).get("regression")) for row in selected)
        safety_events = sum(int((row.claims or {}).get("safetyEvents") or 0) for row in selected)
        total_cost = sum(float((row.claims or {}).get("cost") or 0.0) for row in selected)
        context = plan.context
        return ExperimentArm(
            runs=runs,
            successes=successes,
            regressions=regressions,
            safety_events=safety_events,
            mean_cost=(total_cost / runs) if runs else 0.0,
            model_id=context.model_id,
            reasoning_effort=context.reasoning_effort,
            tool_permission_fingerprint=context.tool_permission_fingerprint,
            resource_budget_fingerprint=context.resource_budget_fingerprint,
            task_distribution_fingerprint=context.task_distribution_fingerprint,
        )

    async def evaluate(self, *, task_id: str, experiment_id: str) -> ExperimentEvaluation:
        plan, rows = await self._load(task_id, experiment_id)
        if self._terminal(rows):
            raise EvolutionExperimentError("evolution experiment is closed")
        observations = [row for row in rows if row.kind == self._OBSERVATION]
        control = self._arm(plan, observations, ExperimentArmName.CONTROL)
        candidate = self._arm(plan, observations, ExperimentArmName.CANDIDATE)
        if control.runs < plan.min_runs_per_arm or candidate.runs < plan.min_runs_per_arm:
            decision = PromotionDecision.INSUFFICIENT_EVIDENCE
        else:
            decision = self.gate.evaluate(
                plan.change_class,
                ExperimentComparison(control=control, candidate=candidate),
            )
        observation_ids = tuple(row.evidence_id for row in observations)
        row = await self.evidence.record(
            task_id=task_id,
            run_id=plan.experiment_id,
            kind=self._EVALUATED,
            producer_identity="capability-os-control",
            claims={
                "changeClass": plan.change_class.value,
                "decision": decision.value,
                "control": asdict(control),
                "candidate": asdict(candidate),
                "observationEvidenceIds": list(observation_ids),
            },
            artifact_digest=plan.candidate_artifact_digest,
        )
        return ExperimentEvaluation(
            decision=decision,
            control=control,
            candidate=candidate,
            observation_evidence_ids=observation_ids,
            evaluation_evidence_id=row.evidence_id,
        )

    async def latest_evaluation(self, *, task_id: str, experiment_id: str) -> tuple[ExperimentPlan, ExperimentEvaluation, list]:
        plan, rows = await self._load(task_id, experiment_id)
        evaluations = [row for row in rows if row.kind == self._EVALUATED]
        if not evaluations:
            raise EvolutionExperimentError("evolution experiment has not been evaluated")
        row = evaluations[-1]
        claims = dict(row.claims or {})
        def parsed_arm(value: Any) -> ExperimentArm:
            if not isinstance(value, dict):
                raise EvolutionExperimentError("evolution evaluation evidence is invalid")
            return ExperimentArm(**value)
        evaluation = ExperimentEvaluation(
            decision=PromotionDecision(str(claims.get("decision") or "")),
            control=parsed_arm(claims.get("control")),
            candidate=parsed_arm(claims.get("candidate")),
            observation_evidence_ids=tuple(map(str, claims.get("observationEvidenceIds") or ())),
            evaluation_evidence_id=row.evidence_id,
        )
        return plan, evaluation, rows

    async def plan(self, *, task_id: str, experiment_id: str) -> ExperimentPlan:
        plan, _rows = await self._load(task_id, experiment_id)
        return plan

    async def status(self, *, task_id: str, experiment_id: str) -> dict[str, Any]:
        plan, rows = await self._load(task_id, experiment_id)
        observations = [row for row in rows if row.kind == self._OBSERVATION]
        control_runs = sum((row.claims or {}).get("arm") == ExperimentArmName.CONTROL.value for row in observations)
        candidate_runs = sum((row.claims or {}).get("arm") == ExperimentArmName.CANDIDATE.value for row in observations)
        evaluations = [row for row in rows if row.kind == self._EVALUATED]
        promotion = next((row for row in reversed(rows) if row.kind == self._PROMOTION), None)
        cancelled = next((row for row in reversed(rows) if row.kind == self._CANCELLED), None)
        return {
            "plan": plan.to_api(),
            "state": "promoted" if promotion else "cancelled" if cancelled else "active",
            "controlRuns": control_runs,
            "candidateRuns": candidate_runs,
            "latestDecision": (
                str((evaluations[-1].claims or {}).get("decision") or "") if evaluations else None
            ),
            "latestEvaluationEvidenceId": evaluations[-1].evidence_id if evaluations else None,
            "promotionEvidenceId": promotion.evidence_id if promotion else None,
            "cancelEvidenceId": cancelled.evidence_id if cancelled else None,
        }

    async def cancel(self, *, task_id: str, experiment_id: str, reason: str) -> str:
        plan, rows = await self._load(task_id, experiment_id)
        if self._terminal(rows):
            raise EvolutionExperimentError("evolution experiment is already closed")
        reason = str(reason).strip()
        if not reason or len(reason) > 2000:
            raise EvolutionExperimentError("experiment cancellation reason must be bounded and non-empty")
        row = await self.evidence.record(
            task_id=task_id,
            run_id=plan.experiment_id,
            kind=self._CANCELLED,
            producer_identity="capability-os-control",
            claims={"reason": reason},
            artifact_digest=plan.candidate_artifact_digest,
        )
        await self.store.set_experiment_state(
            plan.experiment_id,
            state="cancelled",
            updated_at_ms=int(row.created_at_ms),
        )
        return row.evidence_id

    async def record_promotion_intent(
        self,
        *,
        task_id: str,
        experiment_id: str,
        evaluation: ExperimentEvaluation,
        from_state: str,
        target_state: str,
        owner_approval_verified: bool,
    ) -> str:
        plan, rows = await self._load(task_id, experiment_id)
        if self._terminal(rows):
            raise EvolutionExperimentError("evolution experiment is closed")
        latest = [row for row in rows if row.kind == self._EVALUATED]
        if not latest or latest[-1].evidence_id != evaluation.evaluation_evidence_id:
            raise EvolutionExperimentError("promotion requires the latest experiment evaluation")
        if rows[-1].evidence_id != evaluation.evaluation_evidence_id:
            raise EvolutionExperimentError("promotion requires reevaluation after newer experiment evidence")
        row = await self.evidence.record(
            task_id=task_id,
            run_id=plan.experiment_id,
            kind=self._PROMOTION_INTENT,
            producer_identity="capability-os-control",
            claims={
                "decision": evaluation.decision.value,
                "evaluationEvidenceId": evaluation.evaluation_evidence_id,
                "fromState": str(from_state),
                "targetState": str(target_state),
                "ownerApprovalVerified": bool(owner_approval_verified),
            },
            artifact_digest=plan.candidate_artifact_digest,
        )
        return row.evidence_id

    async def record_promotion(
        self,
        *,
        task_id: str,
        experiment_id: str,
        evaluation: ExperimentEvaluation,
        intent_evidence_id: str,
        from_state: str,
        target_state: str,
        owner_approval_verified: bool,
    ) -> str:
        plan, rows = await self._load(task_id, experiment_id)
        if self._terminal(rows):
            raise EvolutionExperimentError("evolution experiment is closed")
        if not rows or rows[-1].kind != self._PROMOTION_INTENT:
            raise EvolutionExperimentError("promotion requires a fresh promotion intent")
        intent = rows[-1]
        if intent.evidence_id != str(intent_evidence_id):
            raise EvolutionExperimentError("promotion intent evidence does not match")
        claims = dict(intent.claims or {})
        if str(claims.get("evaluationEvidenceId") or "") != evaluation.evaluation_evidence_id:
            raise EvolutionExperimentError("promotion intent references another evaluation")
        row = await self.evidence.record(
            task_id=task_id,
            run_id=plan.experiment_id,
            kind=self._PROMOTION,
            producer_identity="capability-os-control",
            claims={
                "decision": evaluation.decision.value,
                "evaluationEvidenceId": evaluation.evaluation_evidence_id,
                "intentEvidenceId": intent.evidence_id,
                "fromState": str(from_state),
                "targetState": str(target_state),
                "ownerApprovalVerified": bool(owner_approval_verified),
            },
            artifact_digest=plan.candidate_artifact_digest,
        )
        await self.store.record_promotion_projection(
            experiment_id=plan.experiment_id,
            candidate_artifact_digest=plan.candidate_artifact_digest,
            decision=evaluation.decision.value,
            from_state=str(from_state),
            target_state=str(target_state),
            evaluation_evidence_id=str(evaluation.evaluation_evidence_id or ""),
            intent_evidence_id=intent.evidence_id,
            promotion_evidence_id=row.evidence_id,
            owner_approval_verified=owner_approval_verified,
            created_at_ms=int(row.created_at_ms),
        )
        await self.store.set_experiment_state(
            plan.experiment_id,
            state="promoted",
            updated_at_ms=int(row.created_at_ms),
        )
        return row.evidence_id
