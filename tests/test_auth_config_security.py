import unittest
from unittest.mock import patch

from cptr.utils import config


class TrustedHeaderSecurityTests(unittest.TestCase):
    def test_trusted_header_without_sources_fails_closed_at_request_time(self):
        configured = {"auth": {"mode": "trusted_header", "trusted_sources": []}}
        with patch.object(config, "_config_cache", configured):
            result = config.check_access(
                client_host="203.0.113.10",
                jwt_token=None,
                remote_user_header="spoofed-user",
            )
        self.assertIsNone(result)

    def test_trusted_header_without_sources_is_rejected_by_startup_validation(self):
        with self.assertRaisesRegex(RuntimeError, "trusted_sources"):
            config.validate_auth_configuration(
                {"auth": {"mode": "trusted_header", "trusted_sources": []}}
            )

    def test_only_explicit_trusted_source_can_assert_remote_user(self):
        configured = {
            "auth": {
                "mode": "trusted_header",
                "trusted_sources": ["127.0.0.1", "10.0.0.5"],
            }
        }
        config.validate_auth_configuration(configured)
        with patch.object(config, "_config_cache", configured):
            trusted = config.check_access(
                client_host="10.0.0.5",
                jwt_token=None,
                remote_user_header="proxy-user",
            )
            untrusted = config.check_access(
                client_host="10.0.0.6",
                jwt_token=None,
                remote_user_header="spoofed-user",
            )
        self.assertEqual(trusted.username if trusted else None, "proxy-user")
        self.assertIsNone(untrusted)


if __name__ == "__main__":
    unittest.main()
