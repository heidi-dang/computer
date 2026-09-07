"""Evidence-driven promotion gates for Capability OS evolution candidates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ChangeClass(str, Enum):
    OBSERVATIONAL = "observational"
    INTERNAL = "internal"
    BEHAVIOURAL = "behavioural"
    AUTHORITY_CRITICAL = "authority-critical"


class PromotionDecision(str, Enum):
    PROMOTE = "promote"
    REJECT = "reject"
    INSUFFICIENT_EVIDENCE = "insufficient-evidence"
    OWNER_APPROVAL_REQUIRED = "owner-approval-required"


@dataclass(frozen=True)
class ExperimentArm:
    runs: int
    successes: int
    regressions: int
    safety_events: int
    mean_cost: float
    model_id: str = ""
    reasoning_effort: str = ""
    tool_permission_fingerprint: str = ""
    resource_budget_fingerprint: str = ""
    task_distribution_fingerprint: str = ""

    def __post_init__(self) -> None:
        if min(self.runs, self.successes, self.regressions, self.safety_events) < 0:
            raise ValueError("experiment counts must be non-negative")
        if self.successes > self.runs or self.regressions > self.runs:
            raise ValueError("experiment outcome counts cannot exceed runs")
        if self.mean_cost < 0:
            raise ValueError("mean_cost must be non-negative")
        for name in (
            "model_id",
            "reasoning_effort",
            "tool_permission_fingerprint",
            "resource_budget_fingerprint",
            "task_distribution_fingerprint",
        ):
            value = str(getattr(self, name)).strip()
            object.__setattr__(self, name, value)

    @property
    def success_rate(self) -> float:
        return (self.successes / self.runs) if self.runs else 0.0

    @property
    def regression_rate(self) -> float:
        return (self.regressions / self.runs) if self.runs else 0.0


@dataclass(frozen=True)
class ExperimentComparison:
    control: ExperimentArm
    candidate: ExperimentArm


_MATCHED_FIELDS = (
    "model_id",
    "reasoning_effort",
    "tool_permission_fingerprint",
    "resource_budget_fingerprint",
    "task_distribution_fingerprint",
)


class EvolutionGate:
    def __init__(self, *, min_runs_per_arm: int = 5, max_regression_rate: float = 0.02) -> None:
        if min_runs_per_arm <= 1:
            raise ValueError("evolution evaluation requires repeated runs")
        if not 0.0 <= max_regression_rate <= 1.0:
            raise ValueError("max_regression_rate must be within [0, 1]")
        self.min_runs_per_arm = int(min_runs_per_arm)
        self.max_regression_rate = float(max_regression_rate)

    def evaluate(
        self,
        change_class: ChangeClass,
        comparison: ExperimentComparison,
    ) -> PromotionDecision:
        control = comparison.control
        candidate = comparison.candidate
        if control.runs < self.min_runs_per_arm or candidate.runs < self.min_runs_per_arm:
            return PromotionDecision.INSUFFICIENT_EVIDENCE
        if any(not getattr(control, name) or not getattr(candidate, name) for name in _MATCHED_FIELDS):
            return PromotionDecision.INSUFFICIENT_EVIDENCE
        if any(getattr(control, name) != getattr(candidate, name) for name in _MATCHED_FIELDS):
            return PromotionDecision.REJECT

        if candidate.safety_events > control.safety_events:
            return PromotionDecision.REJECT
        if candidate.regression_rate > self.max_regression_rate:
            return PromotionDecision.REJECT
        if candidate.regression_rate > control.regression_rate:
            return PromotionDecision.REJECT
        if candidate.success_rate <= control.success_rate:
            return PromotionDecision.REJECT

        if change_class in {ChangeClass.AUTHORITY_CRITICAL, ChangeClass.BEHAVIOURAL}:
            return PromotionDecision.OWNER_APPROVAL_REQUIRED
        return PromotionDecision.PROMOTE
