import unittest

from cptr.services.verification import DefaultIndependentVerifier


class IndependentVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_prose_is_not_enough_when_git_check_fails(self):
        result = await DefaultIndependentVerifier().verify(
            task={"status": "COMPLETE", "output": "Everything passed"},
            evidence={"independent": {"git_diff_check": {"passed": False}}},
        )

        self.assertFalse(result.passed)
        self.assertIn("git diff --check reported errors", result.failures)

    async def test_durable_success_and_independent_checks_can_pass(self):
        result = await DefaultIndependentVerifier().verify(
            task={"status": "COMPLETE", "output": "worker prose"},
            evidence={"independent": {"git_diff_check": {"passed": True}}},
        )

        self.assertTrue(result.passed)
        self.assertEqual(
            {item["name"] for item in result.checks},
            {
                "durable_terminal_success",
                "git_diff_check",
            },
        )


if __name__ == "__main__":
    unittest.main()
