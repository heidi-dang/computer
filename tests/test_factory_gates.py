import unittest

from cptr.services.factory_gates import (
    EvidenceAuthority,
    FactoryGateCategory,
    FactoryGatePlan,
    FactoryGateSpec,
    FactoryGateStatus,
    GateEvidence,
    GateResult,
    validate_gate_evidence,
)


class FactoryGateTests(unittest.TestCase):
    def test_gate_categories_cover_the_factory_verification_contract(self):
        self.assertEqual(
            {category.value for category in FactoryGateCategory},
            {
                "acceptance",
                "reproduction",
                "regression",
                "focused_tests",
                "broader_tests",
                "unit",
                "integration",
                "e2e",
                "typecheck",
                "lint",
                "build",
                "security",
                "isolation",
                "resource",
                "performance",
                "cleanup_lifecycle",
                "adversarial",
                "git_diff_review",
                "git_diff_check",
                "ci",
                "runtime_smoke",
                "live_verify",
            },
        )

    def test_gate_plan_rejects_duplicate_gate_ids_and_unknown_acceptance_coverage(self):
        with self.assertRaisesRegex(ValueError, "duplicate gate"):
            FactoryGatePlan(
                specs=(
                    FactoryGateSpec("lint", FactoryGateCategory.LINT),
                    FactoryGateSpec("lint", FactoryGateCategory.LINT),
                ),
                acceptance_criterion_ids=(),
            )

        with self.assertRaisesRegex(ValueError, "unknown acceptance"):
            FactoryGatePlan(
                specs=(
                    FactoryGateSpec(
                        "acceptance-a",
                        FactoryGateCategory.ACCEPTANCE,
                        acceptance_ids=("criterion-missing",),
                    ),
                ),
                acceptance_criterion_ids=("criterion-known",),
            )

    def test_pass_requires_authoritative_evidence(self):
        spec = FactoryGateSpec("lint", FactoryGateCategory.LINT)
        result = GateResult(
            gate_id="lint",
            status=FactoryGateStatus.PASS,
            evidence_ids=("ev-worker",),
            reason="worker says lint passed",
            evaluated_revision="rev-1",
            evaluated_fingerprint="fp-1",
        )
        advisory = GateEvidence(
            evidence_id="ev-worker",
            digest="digest-worker",
            authority=EvidenceAuthority.ADVISORY,
            revision="rev-1",
            fingerprint="fp-1",
            kind="worker_report",
            source="implementer",
        )

        failures = validate_gate_evidence(
            spec,
            result,
            {advisory.evidence_id: advisory},
            current_revision="rev-1",
            current_fingerprint="fp-1",
        )

        self.assertIn("authoritative machine evidence", " ".join(failures))

    def test_pass_rejects_missing_and_stale_evidence(self):
        spec = FactoryGateSpec("build", FactoryGateCategory.BUILD)
        missing = GateResult(
            gate_id="build",
            status=FactoryGateStatus.PASS,
            evidence_ids=(),
            reason="passed",
            evaluated_revision="rev-1",
            evaluated_fingerprint="fp-1",
        )
        self.assertTrue(
            validate_gate_evidence(
                spec,
                missing,
                {},
                current_revision="rev-1",
                current_fingerprint="fp-1",
            )
        )

        stale_evidence = GateEvidence(
            evidence_id="ev-build",
            digest="digest-build",
            authority=EvidenceAuthority.MACHINE,
            revision="rev-old",
            fingerprint="fp-old",
            kind="command",
            source="server_verifier",
        )
        stale = GateResult(
            gate_id="build",
            status=FactoryGateStatus.PASS,
            evidence_ids=("ev-build",),
            reason="passed",
            evaluated_revision="rev-old",
            evaluated_fingerprint="fp-old",
        )
        failures = validate_gate_evidence(
            spec,
            stale,
            {stale_evidence.evidence_id: stale_evidence},
            current_revision="rev-new",
            current_fingerprint="fp-new",
        )
        self.assertIn("stale", " ".join(failures))

    def test_pass_accepts_current_machine_evidence(self):
        spec = FactoryGateSpec("unit", FactoryGateCategory.UNIT)
        evidence = GateEvidence(
            evidence_id="ev-unit",
            digest="digest-unit",
            authority=EvidenceAuthority.MACHINE,
            revision="rev-1",
            fingerprint="fp-1",
            kind="test_command",
            source="server_verifier",
        )
        result = GateResult(
            gate_id="unit",
            status=FactoryGateStatus.PASS,
            evidence_ids=(evidence.evidence_id,),
            reason="42 tests passed",
            evaluated_revision="rev-1",
            evaluated_fingerprint="fp-1",
        )

        self.assertEqual(
            validate_gate_evidence(
                spec,
                result,
                {evidence.evidence_id: evidence},
                current_revision="rev-1",
                current_fingerprint="fp-1",
            ),
            [],
        )

    def test_not_applicable_requires_explicit_authoritative_resolution(self):
        spec = FactoryGateSpec(
            "e2e",
            FactoryGateCategory.E2E,
            applicable=False,
            applicability_reason="repository has no interactive/runtime surface",
        )
        no_evidence = GateResult(
            gate_id="e2e",
            status=FactoryGateStatus.NOT_APPLICABLE,
            evidence_ids=(),
            reason="not applicable",
        )
        self.assertIn(
            "applicability evidence",
            " ".join(
                validate_gate_evidence(
                    spec,
                    no_evidence,
                    {},
                    current_revision="rev-1",
                    current_fingerprint="fp-1",
                )
            ),
        )

        evidence = GateEvidence(
            evidence_id="ev-profile",
            digest="digest-profile",
            authority=EvidenceAuthority.MACHINE,
            revision="rev-1",
            fingerprint="fp-1",
            kind="repository_profile",
            source="factory_baseline",
        )
        result = GateResult(
            gate_id="e2e",
            status=FactoryGateStatus.NOT_APPLICABLE,
            evidence_ids=(evidence.evidence_id,),
            reason="repository profile proves no applicable E2E surface",
        )
        self.assertEqual(
            validate_gate_evidence(
                spec,
                result,
                {evidence.evidence_id: evidence},
                current_revision="rev-1",
                current_fingerprint="fp-1",
            ),
            [],
        )

    def test_applicable_gate_cannot_be_marked_not_applicable(self):
        spec = FactoryGateSpec("security", FactoryGateCategory.SECURITY, applicable=True)
        result = GateResult(
            gate_id="security",
            status=FactoryGateStatus.NOT_APPLICABLE,
            evidence_ids=("ev-profile",),
            reason="skip it",
        )
        evidence = GateEvidence(
            evidence_id="ev-profile",
            digest="digest-profile",
            authority=EvidenceAuthority.MACHINE,
            revision="rev-1",
            fingerprint="fp-1",
            kind="repository_profile",
            source="factory_baseline",
        )
        self.assertIn(
            "declared applicable",
            " ".join(
                validate_gate_evidence(
                    spec,
                    result,
                    {evidence.evidence_id: evidence},
                    current_revision="rev-1",
                    current_fingerprint="fp-1",
                )
            ),
        )


if __name__ == "__main__":
    unittest.main()
