"""Adversarial closure checks for the integrated CPTR/FlowDeck authority model."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cptr.flowdeck.evidence import EvidenceValidationError, validate_terminal_evidence
from cptr.routers.control import _user
from cptr.services.verification import DefaultIndependentVerifier


class Task21AuthorityClosureTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_evidence_cannot_establish_success(self):
        result = await DefaultIndependentVerifier().verify(
            task={"id": "attempt-1", "status": "COMPLETE", "output": "done"},
            evidence={"independent": {"git_diff_check": {"passed": True}}},
        )
        self.assertFalse(result.passed)
        self.assertIn("terminal evidence is required", result.failures)

    async def test_agent_prose_and_approval_are_not_terminal_evidence(self):
        result = await DefaultIndependentVerifier().verify(
            task={"id": "attempt-1", "status": "COMPLETE", "output": "verified"},
            evidence={
                "approval": {"status": "APPROVED"},
                "independent": {
                    "authoritative": True,
                    "source": "verifier",
                    "observation": "verifier_check",
                    "observed_outcome": "succeeded",
                    "attempt_id": "attempt-2",
                    "specialist_claim": "done",
                    "git_diff_check": {"passed": True},
                },
            },
        )
        self.assertFalse(result.passed)
        self.assertTrue(any("attempt identity" in failure for failure in result.failures))

    async def test_failed_cancelled_unknown_and_manual_states_never_pass(self):
        for outcome in ("failed", "cancelled", "unknown", "manual_review_required"):
            result = await DefaultIndependentVerifier().verify(
                task={"id": "attempt-1", "status": "COMPLETE"},
                evidence={
                    "outcome": outcome,
                    "independent": {
                        "authoritative": True,
                        "source": "verifier",
                        "observation": "verifier_check",
                        "observed_outcome": outcome,
                        "attempt_id": "attempt-1",
                        "specialist_claim": None,
                        "git_diff_check": {"passed": True},
                    },
                },
            )
            self.assertFalse(result.passed, outcome)

    def test_identity_mismatch_is_rejected_by_shared_contract(self):
        with self.assertRaises(EvidenceValidationError):
            validate_terminal_evidence(
                {
                    "authoritative": True,
                    "source": "verifier",
                    "observation": "verifier_check",
                    "observed_outcome": "succeeded",
                    "attempt_id": "new-attempt",
                    "specialist_claim": None,
                },
                outcome="succeeded",
                attempt_id="old-attempt",
            )

    async def test_control_plane_is_disabled_by_default(self):
        request = SimpleNamespace(headers={}, scope={"type": "http"})
        with patch.dict(os.environ, {"CPTR_CONTROL_PLANE_ENABLED": "false"}, clear=False):
            with self.assertRaisesRegex(Exception, "control plane is unavailable"):
                await _user(request, "task:read")


if __name__ == "__main__":
    unittest.main()