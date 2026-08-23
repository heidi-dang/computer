import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from cptr.services.verification import (
    VERIFICATION_CATEGORIES,
    DefaultIndependentVerifier,
    VerificationCommand,
    _commands_from_payload,
)


class IndependentVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_prose_is_not_enough_when_git_check_fails(self):
        result = await DefaultIndependentVerifier().verify(
            task={"status": "COMPLETE", "output": "Everything passed"},
            evidence={
                "independent": {
                    "authoritative": True,
                    "source": "verifier",
                    "observation": "verifier_check",
                    "observed_outcome": "succeeded",
                    "attempt_id": "",
                    "specialist_claim": None,
                    "git_diff_check": {"passed": False},
                }
            },
        )

        self.assertFalse(result.passed)
        self.assertIn("git diff --check reported errors", result.failures)

    async def test_durable_success_and_independent_checks_can_pass(self):
        result = await DefaultIndependentVerifier().verify(
            task={"status": "COMPLETE", "output": "worker prose"},
            evidence={
                "independent": {
                    "authoritative": True,
                    "source": "verifier",
                    "observation": "verifier_check",
                    "observed_outcome": "succeeded",
                    "attempt_id": "",
                    "specialist_claim": None,
                    "git_diff_check": {"passed": True},
                }
            },
        )

        self.assertTrue(result.passed)
        self.assertEqual(
            {item["name"] for item in result.checks},
            {
                "durable_terminal_success",
                "git_diff_check",
            },
        )

    async def test_configured_commands_run_independently_and_capture_bounded_evidence(self):
        with tempfile.TemporaryDirectory() as workspace:
            command = VerificationCommand(
                name="smoke",
                category="runtime_smoke",
                argv=(
                    sys.executable,
                    "-c",
                    "print('independent smoke passed')",
                ),
                timeout_seconds=5,
            )

            result = await DefaultIndependentVerifier(commands=[command]).verify(
                task={"status": "COMPLETE", "output": "worker prose"},
                evidence={
                    "independent": {
                        "authoritative": True,
                        "source": "verifier",
                        "observation": "verifier_check",
                        "observed_outcome": "succeeded",
                        "attempt_id": "",
                        "specialist_claim": None,
                        "workspace_path": workspace,
                        "git_diff_check": {"passed": True},
                    }
                },
            )

        self.assertTrue(result.passed)
        command_check = next(item for item in result.checks if item.get("name") == "smoke")
        self.assertTrue(command_check["passed"])
        self.assertEqual(command_check["exit_code"], 0)
        self.assertIn("independent smoke passed", command_check["stdout"])
        self.assertIsInstance(command_check["started_at"], int)
        self.assertIsInstance(command_check["finished_at"], int)
        self.assertIn("duration_ms", command_check)
        self.assertFalse(command_check["timed_out"])

    async def test_configured_command_failure_is_not_hidden_by_worker_prose(self):
        with tempfile.TemporaryDirectory() as workspace:
            command = VerificationCommand(
                name="pytest",
                category="focused_tests",
                argv=(sys.executable, "-c", "import sys; print('fixture failed'); sys.exit(3)"),
                timeout_seconds=5,
            )

            result = await DefaultIndependentVerifier(commands=[command]).verify(
                task={"status": "COMPLETE", "output": "tests passed"},
                evidence={
                    "independent": {
                        "authoritative": True,
                        "source": "verifier",
                        "observation": "verifier_check",
                        "observed_outcome": "succeeded",
                        "attempt_id": "",
                        "specialist_claim": None,
                        "workspace_path": workspace,
                        "git_diff_check": {"passed": True},
                    }
                },
            )

        self.assertFalse(result.passed)
        command_check = next(item for item in result.checks if item.get("name") == "pytest")
        self.assertFalse(command_check["passed"])
        self.assertEqual(command_check["exit_code"], 3)
        self.assertIn("fixture failed", command_check["stdout"])
        self.assertTrue(any("pytest" in failure for failure in result.failures))

    async def test_commands_can_be_loaded_from_configuration_without_shell_interpolation(self):
        with tempfile.TemporaryDirectory() as workspace:
            old = os.environ.get("CPTR_VERIFICATION_COMMANDS_JSON")
            os.environ["CPTR_VERIFICATION_COMMANDS_JSON"] = json.dumps(
                [{"name": "configured", "argv": [sys.executable, "-c", "print('ok')"]}]
            )
            try:
                result = await DefaultIndependentVerifier().verify(
                    task={"status": "COMPLETE"},
                    evidence={
                        "independent": {
                            "authoritative": True,
                            "source": "verifier",
                            "observation": "verifier_check",
                            "observed_outcome": "succeeded",
                            "attempt_id": "",
                            "specialist_claim": None,
                            "workspace_path": Path(workspace),
                            "git_diff_check": {"passed": True},
                        }
                    },
                )
            finally:
                if old is None:
                    os.environ.pop("CPTR_VERIFICATION_COMMANDS_JSON", None)
                else:
                    os.environ["CPTR_VERIFICATION_COMMANDS_JSON"] = old

        self.assertTrue(result.passed)
        self.assertTrue(any(item.get("name") == "configured" for item in result.checks))

    def test_configuration_supports_each_workspace_verification_category(self):
        payload = [
            {"name": category, "category": category, "argv": [sys.executable, "-c", "pass"]}
            for category in sorted(VERIFICATION_CATEGORIES)
        ]

        commands = _commands_from_payload(payload)

        self.assertEqual({command.category for command in commands}, VERIFICATION_CATEGORIES)

    def test_unknown_workspace_verification_category_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown verification category"):
            _commands_from_payload(
                [{"name": "bad", "category": "arbitrary", "argv": [sys.executable, "-c", "pass"]}]
            )


if __name__ == "__main__":
    unittest.main()
