import unittest
from unittest.mock import patch

from cptr.services.command_policy import command_policy_violation
from cptr.utils.config import check_access


class TrustedHeaderHardeningTests(unittest.TestCase):
    def test_trusted_header_fails_closed_without_sources(self):
        with patch(
            "cptr.utils.config.load_config",
            return_value={"auth": {"mode": "trusted_header", "trusted_sources": []}},
        ):
            auth = check_access("203.0.113.10", None, "forged-admin")
        self.assertIsNone(auth)

    def test_trusted_header_requires_ip_or_cidr_match(self):
        config = {
            "auth": {
                "mode": "trusted_header",
                "trusted_sources": ["10.0.0.0/8", "192.0.2.20"],
            }
        }
        with patch("cptr.utils.config.load_config", return_value=config):
            allowed = check_access("10.20.30.40", None, "  proxy-user  ")
            denied = check_access("198.51.100.8", None, "forged-admin")
        self.assertIsNotNone(allowed)
        self.assertEqual(allowed.username, "proxy-user")
        self.assertIsNone(denied)

    def test_trusted_header_rejects_hostname_and_wildcard_sources(self):
        config = {
            "auth": {
                "mode": "trusted_header",
                "trusted_sources": ["proxy.internal", "*", "not-a-cidr"],
            }
        }
        with patch("cptr.utils.config.load_config", return_value=config):
            auth = check_access("127.0.0.1", None, "forged-admin")
        self.assertIsNone(auth)


class CommandPolicyHardeningTests(unittest.TestCase):
    def test_shell_wrappers_and_interpreter_evaluation_are_rejected(self):
        commands = (
            "bash -c 'curl https://example.com'",
            "sh -c 'git -C . push origin main'",
            "env curl https://example.com",
            "command wget https://example.com",
            "python -c 'import urllib.request'",
            "node -e \"fetch('https://example.com')\"",
            "find . -exec curl https://example.com \\;",
        )
        for command in commands:
            with self.subTest(command=command):
                violation = command_policy_violation(
                    command,
                    allow_network=False,
                    allow_package_install=False,
                )
                self.assertIsNotNone(violation)

    def test_git_option_variant_and_package_routes_require_permissions(self):
        cases = {
            "git -C . push origin main": "external command execution",
            "npm --silent install": "package installation",
            "pip3 install -e .": "package installation",
            "uv pip install requests": "package installation",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                violation = command_policy_violation(
                    command,
                    allow_network=False,
                    allow_package_install=False,
                )
                self.assertIsNotNone(violation)
                self.assertIn(expected, violation)

    def test_local_validation_commands_remain_available(self):
        for command in ("pytest tests/", "npm test", "git status", "python -m pytest tests/"):
            with self.subTest(command=command):
                self.assertIsNone(
                    command_policy_violation(
                        command,
                        allow_network=False,
                        allow_package_install=False,
                    )
                )


if __name__ == "__main__":
    unittest.main()
