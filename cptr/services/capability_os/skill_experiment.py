"""Matched-budget skill experiment scheduler with delta_Q measurement.

Runs A/B (or multi-variant) experiments across skill genomes on a shared set
of task references, measures per-variant success rates (Q), and recommends
promote / retain / discard based on the delta between the challenger and the
baseline variant 'A'.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class ExperimentVariant:
    variant_id: str           # e.g. 'A', 'B', 'C'
    skill_id: str
    skill_genome: dict
    description: str = ""


@dataclass
class ExperimentConfig:
    experiment_id: str
    name: str
    hypothesis: str
    task_refs: list
    variants: list
    token_budget: int
    timeout_seconds: float
    promotion_threshold: float = 0.05


@dataclass
class VariantResult:
    variant_id: str
    task_ref: str
    success: bool
    tokens_used: int = 0
    tool_calls: int = 0
    elapsed_ms: float = 0.0
    verifier_passed: bool = False
    error: str = None


@dataclass
class ExperimentResult:
    experiment_id: str
    variant_results: list
    delta_q: dict
    winner: str
    recommendation: str
    completed_at: float = field(default_factory=time.time)
    error: str = None


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class SkillExperimentScheduler:
    """Run matched-budget A/B experiments and derive delta_Q recommendations."""

    async def run_experiment(self, config):
        """Execute all variants across all task refs and return a result."""
        per_variant_budget = config.token_budget // max(len(config.variants), 1)

        tasks = [
            self._run_variant_on_task(variant, task_ref, per_variant_budget, config.timeout_seconds)
            for variant in config.variants
            for task_ref in config.task_refs
        ]
        variant_results = list(await asyncio.gather(*tasks))

        delta_q = self._compute_delta_q(variant_results)
        winner = self._select_winner(delta_q, config.promotion_threshold)
        recommendation = self._make_recommendation(delta_q, winner)

        return ExperimentResult(
            experiment_id=config.experiment_id,
            variant_results=variant_results,
            delta_q=delta_q,
            winner=winner,
            recommendation=recommendation,
            completed_at=time.time(),
        )

    async def _run_variant_on_task(self, variant, task_ref, budget, timeout):
        """Stub: returns a failed result until wired to a real executor."""
        return VariantResult(
            variant_id=variant.variant_id,
            task_ref=task_ref,
            success=False,
            error="stub: not yet wired",
        )

    def _compute_delta_q(self, results):
        """Compute Q (success rate) per variant; delta = Q[v] - Q['A']."""
        counts = {}
        for r in results:
            counts.setdefault(r.variant_id, []).append(r.success)

        q = {
            vid: sum(outcomes) / len(outcomes)
            for vid, outcomes in counts.items()
            if outcomes
        }

        baseline = q.get("A", 0.0)
        return {vid: round(score - baseline, 10) for vid, score in q.items()}

    def _select_winner(self, delta_q, threshold):
        """Return the variant with the highest delta >= threshold, or None."""
        candidates = {
            vid: delta
            for vid, delta in delta_q.items()
            if vid != "A" and delta >= threshold
        }
        if not candidates:
            return None
        return max(candidates, key=lambda v: candidates[v])

    def _make_recommendation(self, delta_q, winner):
        """
        promote  - a challenger beat the threshold
        retain   - no challenger beats threshold, none worse
        discard  - at least one challenger is strictly below baseline (delta < 0)
        """
        if winner is not None:
            return "promote"

        challenger_deltas = [d for vid, d in delta_q.items() if vid != "A"]
        if any(d < 0 for d in challenger_deltas):
            return "discard"
        return "retain"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_experiment_config(
    name,
    hypothesis,
    task_refs,
    baseline_skill_id,
    candidate_skill_id,
    token_budget=10000,
    timeout_seconds=60.0,
    promotion_threshold=0.05,
):
    """Create a two-variant (A=baseline, B=candidate) ExperimentConfig."""
    return ExperimentConfig(
        experiment_id=str(uuid.uuid4()),
        name=name,
        hypothesis=hypothesis,
        task_refs=list(task_refs),
        variants=[
            ExperimentVariant(
                variant_id="A",
                skill_id=baseline_skill_id,
                skill_genome={},
                description="baseline",
            ),
            ExperimentVariant(
                variant_id="B",
                skill_id=candidate_skill_id,
                skill_genome={},
                description="candidate",
            ),
        ],
        token_budget=token_budget,
        timeout_seconds=timeout_seconds,
        promotion_threshold=promotion_threshold,
    )
