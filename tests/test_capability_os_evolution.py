import unittest

from cptr.services.capability_os.evolution import (
    ChangeClass,
    EvolutionGate,
    ExperimentArm,
    ExperimentComparison,
    PromotionDecision,
)


class CapabilityOsEvolutionTests(unittest.TestCase):
    def arm(self, runs, successes, regressions, safety_events, mean_cost, **overrides):
        values = {
            "runs": runs,
            "successes": successes,
            "regressions": regressions,
            "safety_events": safety_events,
            "mean_cost": mean_cost,
            "model_id": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "tool_permission_fingerprint": "perm-v1",
            "resource_budget_fingerprint": "budget-v1",
            "task_distribution_fingerprint": "holdout-v1",
        }
        values.update(overrides)
        return ExperimentArm(**values)

    def test_candidate_requires_repeated_matched_evidence_not_single_subjective_success(self):
        gate = EvolutionGate(min_runs_per_arm=5, max_regression_rate=0.02)
        comparison = ExperimentComparison(
            control=self.arm(1, 0, 0, 0, 10.0),
            candidate=self.arm(1, 1, 0, 0, 9.0),
        )
        decision = gate.evaluate(ChangeClass.INTERNAL, comparison)
        self.assertEqual(decision, PromotionDecision.INSUFFICIENT_EVIDENCE)

    def test_authority_critical_change_never_auto_promotes(self):
        gate = EvolutionGate(min_runs_per_arm=5, max_regression_rate=0.02)
        comparison = ExperimentComparison(
            control=self.arm(10, 8, 0, 0, 10.0),
            candidate=self.arm(10, 10, 0, 0, 8.0),
        )
        decision = gate.evaluate(ChangeClass.AUTHORITY_CRITICAL, comparison)
        self.assertEqual(decision, PromotionDecision.OWNER_APPROVAL_REQUIRED)

    def test_internal_candidate_must_improve_success_without_safety_or_regression_degradation(self):
        gate = EvolutionGate(min_runs_per_arm=5, max_regression_rate=0.02)
        better = ExperimentComparison(
            control=self.arm(100, 90, 1, 0, 10.0),
            candidate=self.arm(100, 96, 1, 0, 9.0),
        )
        self.assertEqual(gate.evaluate(ChangeClass.INTERNAL, better), PromotionDecision.PROMOTE)

        unsafe = ExperimentComparison(
            control=self.arm(100, 90, 0, 0, 10.0),
            candidate=self.arm(100, 99, 0, 1, 8.0),
        )
        self.assertEqual(gate.evaluate(ChangeClass.INTERNAL, unsafe), PromotionDecision.REJECT)

        regressed = ExperimentComparison(
            control=self.arm(100, 90, 0, 0, 10.0),
            candidate=self.arm(100, 99, 3, 0, 8.0),
        )
        self.assertEqual(gate.evaluate(ChangeClass.INTERNAL, regressed), PromotionDecision.REJECT)

    def test_promotion_requires_matched_budget_fingerprints(self):
        gate = EvolutionGate(min_runs_per_arm=5, max_regression_rate=0.02)
        missing = ExperimentComparison(
            control=ExperimentArm(10, 8, 0, 0, 10.0),
            candidate=ExperimentArm(10, 10, 0, 0, 8.0),
        )
        self.assertEqual(
            gate.evaluate(ChangeClass.INTERNAL, missing),
            PromotionDecision.INSUFFICIENT_EVIDENCE,
        )
        mismatched = ExperimentComparison(
            control=self.arm(10, 8, 0, 0, 10.0),
            candidate=self.arm(
                10, 10, 0, 0, 8.0,
                resource_budget_fingerprint="budget-v2",
            ),
        )
        self.assertEqual(
            gate.evaluate(ChangeClass.INTERNAL, mismatched),
            PromotionDecision.REJECT,
        )


if __name__ == "__main__":
    unittest.main()
