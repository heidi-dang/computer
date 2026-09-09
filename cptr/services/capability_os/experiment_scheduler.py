"""Matched-budget scheduler for Capability OS evolution experiments.

The scheduler owns pairing/order and context matching; an injected server-side
runner owns model execution. Runner outputs are accepted only indirectly through
the existing EvolutionExperimentEngine, which verifies server-produced evidence,
artifact-arm identity, and immutable outcome claims.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from cptr.services.capability_os.evolution_engine import (
    EvolutionExperimentEngine,
    ExperimentArmName,
    ExperimentContext,
    ExperimentEvaluation,
    ExperimentPlan,
)


class MatchedExperimentScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class HeldOutTaskCase:
    case_id: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        case_id = str(self.case_id).strip()
        if not case_id or len(case_id) > 200:
            raise MatchedExperimentScheduleError("held-out task case id is invalid")
        if not isinstance(self.payload, dict):
            raise MatchedExperimentScheduleError("held-out task payload must be an object")
        object.__setattr__(self, "case_id", case_id)


@dataclass(frozen=True)
class ScheduledArmRequest:
    task_id: str
    experiment_id: str
    case_id: str
    artifact_digest: str
    arm: ExperimentArmName
    payload: dict[str, Any]
    context: ExperimentContext


@dataclass(frozen=True)
class ScheduledArmResult:
    source_evidence_id: str

    def __post_init__(self) -> None:
        value = str(self.source_evidence_id).strip()
        if not value or len(value) > 200:
            raise MatchedExperimentScheduleError(
                "scheduled arm runner returned invalid evidence id"
            )
        object.__setattr__(self, "source_evidence_id", value)


MatchedArmRunner = Callable[
    [ScheduledArmRequest], Awaitable[ScheduledArmResult] | ScheduledArmResult
]


@dataclass(frozen=True)
class MatchedExperimentScheduleResult:
    experiment_id: str
    case_count: int
    observation_evidence_ids: tuple[str, ...]
    evaluation: ExperimentEvaluation

    def to_api(self) -> dict[str, Any]:
        return {
            "experimentId": self.experiment_id,
            "caseCount": self.case_count,
            "observationEvidenceIds": list(self.observation_evidence_ids),
            "evaluation": self.evaluation.to_api(),
        }


class MatchedExperimentScheduler:
    def __init__(
        self,
        *,
        engine: EvolutionExperimentEngine,
        runner: MatchedArmRunner,
        max_cases: int = 100,
    ) -> None:
        if max_cases < 1 or max_cases > 1000:
            raise ValueError("matched experiment scheduler case bound is invalid")
        self._engine = engine
        self._runner = runner
        self._max_cases = int(max_cases)

    async def _run_arm(self, request: ScheduledArmRequest) -> ScheduledArmResult:
        result = self._runner(request)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ScheduledArmResult):
            raise MatchedExperimentScheduleError(
                "matched experiment runner returned invalid result"
            )
        return result

    @staticmethod
    def _artifact_for(plan: ExperimentPlan, arm: ExperimentArmName) -> str:
        return (
            plan.control_artifact_digest
            if arm is ExperimentArmName.CONTROL
            else plan.candidate_artifact_digest
        )

    async def run(
        self,
        *,
        task_id: str,
        experiment_id: str,
        task_distribution_fingerprint: str,
        cases: tuple[HeldOutTaskCase, ...],
    ) -> MatchedExperimentScheduleResult:
        if not cases or len(cases) > self._max_cases:
            raise MatchedExperimentScheduleError(
                "held-out task set size is outside scheduler bounds"
            )
        case_ids = [case.case_id for case in cases]
        if len(case_ids) != len(set(case_ids)):
            raise MatchedExperimentScheduleError("held-out task case ids must be unique")
        plan = await self._engine.plan(task_id=task_id, experiment_id=experiment_id)
        fingerprint = str(task_distribution_fingerprint).strip()
        if fingerprint != plan.context.task_distribution_fingerprint:
            raise MatchedExperimentScheduleError(
                "held-out task set fingerprint does not match experiment context"
            )

        observation_ids: list[str] = []
        for index, case in enumerate(cases):
            # Alternate which arm executes first to avoid a systematic order bias
            # while preserving the exact same immutable context for both arms.
            order = (
                (ExperimentArmName.CONTROL, ExperimentArmName.CANDIDATE)
                if index % 2 == 0
                else (ExperimentArmName.CANDIDATE, ExperimentArmName.CONTROL)
            )
            for arm in order:
                scheduled = ScheduledArmRequest(
                    task_id=task_id,
                    experiment_id=experiment_id,
                    case_id=case.case_id,
                    artifact_digest=self._artifact_for(plan, arm),
                    arm=arm,
                    payload=dict(case.payload),
                    context=plan.context,
                )
                arm_result = await self._run_arm(scheduled)
                observation = await self._engine.observe(
                    task_id=task_id,
                    experiment_id=experiment_id,
                    arm=arm,
                    source_evidence_id=arm_result.source_evidence_id,
                )
                observation_ids.append(observation.evidence_id)

        evaluation = await self._engine.evaluate(
            task_id=task_id,
            experiment_id=experiment_id,
        )
        return MatchedExperimentScheduleResult(
            experiment_id=experiment_id,
            case_count=len(cases),
            observation_evidence_ids=tuple(observation_ids),
            evaluation=evaluation,
        )
