import unittest

from cptr.services.factory_gates import (
    EvidenceAuthority,
    FactoryGateCategory,
    FactoryGatePlan,
    FactoryGateSpec,
    FactoryGateStatus,
    GateEvidence,
    GateResult,
)
from cptr.services.factory_victory import FactoryVictoryDecision, FactoryVictoryJudge


class FactoryVictoryTests(unittest.TestCase):
    def setUp(self):
        self.plan = FactoryGatePlan(
            specs=(
                FactoryGateSpec(
                    "acceptance-a",
                    FactoryGateCategory.ACCEPTANCE,
                    acceptance_ids=("criterion-a",),
                ),
                FactoryGateSpec("lint", FactoryGateCategory.LINT),
                FactoryGateSpec("git-diff-check", FactoryGateCategory.GIT_DIFF_CHECK),
            ),
            acceptance_criterion_ids=("criterion-a",),
        )
        self.evidence = {
            "ev-acceptance": self._machine_evidence("ev-acceptance", "acceptance_probe"),
            "ev-lint": self._machine_evidence("ev-lint", "command"),
            "ev-diff": self._machine_evidence("ev-diff", "git_diff_check"),
        }
        self.results = {
            "acceptance-a": self._pass("acceptance-a", "ev-acceptance"),
            "lint": self._pass("lint", "ev-lint"),
            "git-diff-check": self._pass("git-diff-check", "ev-diff"),
        }

    def test_all_required_current_machine_evidence_reaches_victory(self):
        decision = FactoryVictoryJudge().evaluate(
            gate_plan=self.plan,
            gate_results=self.results,
            evidence=self.evidence,
            current_revision="rev-1",
            current_fingerprint="fp-1",
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.failures, ())
        self.assertEqual(
            set(decision.satisfied_gate_ids),
            {"acceptance-a", "lint", "git-diff-check"},
        )
        self.assertEqual(decision.evaluated_revision, "rev-1")

    def test_missing_pending_or_failed_required_gate_prevents_victory(self):
        cases = {
            "missing": {key: value for key, value in self.results.items() if key != "lint"},
            "pending": {
                **self.results,
                "lint": GateResult(
                    gate_id="lint",
                    status=FactoryGateStatus.PENDING,
                    evidence_ids=(),
                    reason="not run",
                ),
            },
            "failed": {
                **self.results,
                "lint": GateResult(
                    gate_id="lint",
                    status=FactoryGateStatus.FAIL,
                    evidence_ids=("ev-lint",),
                    reason="lint failed",
                    evaluated_revision="rev-1",
                    evaluated_fingerprint="fp-1",
                ),
            },
        }
        for name, results in cases.items():
            with self.subTest(name=name):
                decision = self._evaluate(results=results)
                self.assertFalse(decision.passed)
                self.assertIn("lint", " ".join(decision.failures))

    def test_worker_success_prose_cannot_override_failed_machine_gate(self):
        advisory = GateEvidence(
            evidence_id="ev-worker",
            digest="digest-worker",
            authority=EvidenceAuthority.ADVISORY,
            revision="rev-1",
            fingerprint="fp-1",
            kind="worker_report",
            source="implementer",
        )
        results = {
            **self.results,
            "lint": GateResult(
                gate_id="lint",
                status=FactoryGateStatus.PASS,
                evidence_ids=("ev-worker",),
                reason="all tests passed, trust me",
                evaluated_revision="rev-1",
                evaluated_fingerprint="fp-1",
            ),
        }

        decision = FactoryVictoryJudge().evaluate(
            gate_plan=self.plan,
            gate_results=results,
            evidence={**self.evidence, "ev-worker": advisory},
            current_revision="rev-1",
            current_fingerprint="fp-1",
        )

        self.assertFalse(decision.passed)
        self.assertIn("authoritative machine evidence", " ".join(decision.failures))

    def test_stale_revision_evidence_prevents_victory(self):
        stale = self._machine_evidence(
            "ev-lint-stale", "command", revision="rev-old", fingerprint="fp-old"
        )
        results = {
            **self.results,
            "lint": GateResult(
                gate_id="lint",
                status=FactoryGateStatus.PASS,
                evidence_ids=(stale.evidence_id,),
                reason="old lint passed",
                evaluated_revision="rev-old",
                evaluated_fingerprint="fp-old",
            ),
        }
        decision = FactoryVictoryJudge().evaluate(
            gate_plan=self.plan,
            gate_results=results,
            evidence={**self.evidence, stale.evidence_id: stale},
            current_revision="rev-1",
            current_fingerprint="fp-1",
        )
        self.assertFalse(decision.passed)
        self.assertIn("stale", " ".join(decision.failures))

    def test_unresolved_security_or_adversarial_blocker_prevents_victory(self):
        decision = self._evaluate(
            unresolved_security_findings=(
                "SEC-7: untrusted capability requested credential access",
            )
        )
        self.assertFalse(decision.passed)
        self.assertIn("SEC-7", " ".join(decision.failures))

    def test_uncovered_acceptance_criterion_prevents_victory(self):
        plan = FactoryGatePlan(
            specs=(FactoryGateSpec("lint", FactoryGateCategory.LINT),),
            acceptance_criterion_ids=("criterion-a",),
        )
        decision = FactoryVictoryJudge().evaluate(
            gate_plan=plan,
            gate_results={"lint": self.results["lint"]},
            evidence=self.evidence,
            current_revision="rev-1",
            current_fingerprint="fp-1",
        )
        self.assertFalse(decision.passed)
        self.assertIn("criterion-a", " ".join(decision.failures))

    def test_required_non_applicable_gate_must_have_authoritative_resolution(self):
        plan = FactoryGatePlan(
            specs=(
                FactoryGateSpec(
                    "e2e",
                    FactoryGateCategory.E2E,
                    applicable=False,
                    applicability_reason="no runtime surface",
                ),
            ),
            acceptance_criterion_ids=(),
        )
        bad = FactoryVictoryJudge().evaluate(
            gate_plan=plan,
            gate_results={
                "e2e": GateResult(
                    gate_id="e2e",
                    status=FactoryGateStatus.NOT_APPLICABLE,
                    evidence_ids=(),
                    reason="skip it",
                )
            },
            evidence={},
            current_revision="rev-1",
            current_fingerprint="fp-1",
        )
        self.assertFalse(bad.passed)

        profile = self._machine_evidence("ev-profile", "repository_profile")
        good = FactoryVictoryJudge().evaluate(
            gate_plan=plan,
            gate_results={
                "e2e": GateResult(
                    gate_id="e2e",
                    status=FactoryGateStatus.NOT_APPLICABLE,
                    evidence_ids=(profile.evidence_id,),
                    reason="machine repository profile confirms no runtime surface",
                )
            },
            evidence={profile.evidence_id: profile},
            current_revision="rev-1",
            current_fingerprint="fp-1",
        )
        self.assertTrue(good.passed)

    def test_victory_decision_cannot_be_constructed_by_a_worker_or_model_payload(self):
        with self.assertRaises(TypeError):
            FactoryVictoryDecision(
                passed=True,
                failures=(),
                satisfied_gate_ids=("lint",),
                evaluated_revision="rev-1",
                evaluated_fingerprint="fp-1",
            )

    def _evaluate(self, *, results=None, unresolved_security_findings=()):
        return FactoryVictoryJudge().evaluate(
            gate_plan=self.plan,
            gate_results=results or self.results,
            evidence=self.evidence,
            current_revision="rev-1",
            current_fingerprint="fp-1",
            unresolved_security_findings=unresolved_security_findings,
        )

    def _machine_evidence(self, evidence_id, kind, *, revision="rev-1", fingerprint="fp-1"):
        return GateEvidence(
            evidence_id=evidence_id,
            digest=f"digest-{evidence_id}",
            authority=EvidenceAuthority.MACHINE,
            revision=revision,
            fingerprint=fingerprint,
            kind=kind,
            source="server_verifier",
        )

    def _pass(self, gate_id, evidence_id):
        return GateResult(
            gate_id=gate_id,
            status=FactoryGateStatus.PASS,
            evidence_ids=(evidence_id,),
            reason="machine verification passed",
            evaluated_revision="rev-1",
            evaluated_fingerprint="fp-1",
        )


if __name__ == "__main__":
    unittest.main()
