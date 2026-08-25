import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cptr.flowdeck.generated_auth import (
    AuthProvider,
    GeneratedAuthError,
    GeneratedAuthService,
)


class GeneratedAuthServiceTests(unittest.TestCase):
    def test_local_session_lifecycle_and_opaque_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GeneratedAuthService(directory)
            user = service.signup("Person@Example.com", "correct horse battery")
            signed_in, token, csrf, expires_at = service.signin("person@example.com", "correct horse battery")
            self.assertEqual(user.id, signed_in.id)
            self.assertGreater(expires_at, 0)
            session = service.session(token, csrf)
            self.assertEqual(session.user.email, "person@example.com")
            self.assertNotIn(token, (Path(directory) / ".cptr" / "generated-auth.sqlite3").read_bytes().decode("latin1"))
            service.signout(token, csrf)
            with self.assertRaisesRegex(GeneratedAuthError, "authentication required"):
                service.session(token)

    def test_csrf_and_role_enforcement(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GeneratedAuthService(directory)
            service.signup("user@example.com", "correct horse battery")
            _, token, csrf, _ = service.signin("user@example.com", "correct horse battery")
            session = service.session(token)
            with self.assertRaisesRegex(GeneratedAuthError, "CSRF"):
                service.signout(token, "wrong")
            with self.assertRaisesRegex(GeneratedAuthError, "forbidden"):
                service.require_role(session, "admin")
            self.assertEqual(service.session(token, csrf).user.role, "user")

    def test_expiry_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GeneratedAuthService(directory)
            service.signup("user@example.com", "correct horse battery")
            _, token, csrf, expires_at = service.signin("user@example.com", "correct horse battery")
            with patch("cptr.flowdeck.generated_auth.time.time", return_value=expires_at + 1):
                with self.assertRaisesRegex(GeneratedAuthError, "session expired"):
                    service.session(token, csrf)
            with self.assertRaisesRegex(GeneratedAuthError, "authentication required"):
                service.session(token)

    def test_provider_detection_preserves_supported_existing_auth(self):
        cases = {
            "authjs": {"next-auth": "^5.0.0"},
            "clerk": {"@clerk/nextjs": "^6.0.0"},
            "supabase": {"@supabase/supabase-js": "^2.0.0"},
            "firebase": {"firebase": "^11.0.0"},
        }
        for expected, dependencies in cases.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                Path(directory, "package.json").write_text(
                    json.dumps({"dependencies": dependencies}), encoding="utf-8"
                )
                service = GeneratedAuthService(directory)
                self.assertEqual(service.provider, AuthProvider(expected))
                self.assertTrue(service.metadata()["preserved_existing_auth"])
                self.assertFalse(service.metadata()["verified"])
                with self.assertRaisesRegex(GeneratedAuthError, "server-owned verifier"):
                    service.signup("user@example.com", "correct horse battery")

    def test_ambiguous_and_unsafe_external_callbacks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "package.json").write_text(
                json.dumps({"dependencies": {"firebase": "1", "firebase-admin": "1"}}),
                encoding="utf-8",
            )
            Path(directory, ".cptr").mkdir()
            Path(directory, ".cptr", "generated-auth.json").write_text(
                json.dumps({"provider": "authjs"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(GeneratedAuthError, "multiple"):
                GeneratedAuthService(directory)

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".cptr").mkdir()
            Path(directory, ".cptr", "generated-auth.json").write_text(
                json.dumps(
                    {
                        "provider": "oauth_oidc",
                        "verifier": {
                            "issuer": "https://issuer.example",
                            "audience": "app",
                            "jwks_url": "https://issuer.example/jwks",
                            "redirect_uri": "https://app.example/callback",
                        },
                    }
                ),
                encoding="utf-8",
            )
            service = GeneratedAuthService(directory)
            with self.assertRaisesRegex(GeneratedAuthError, "unsafe"):
                service.verify_external_callback(
                    issuer="https://issuer.example",
                    audience="app",
                    redirect_uri="http://evil.example/callback",
                    state="same",
                    expected_state="same",
                    nonce="same",
                    expected_nonce="same",
                    code_verifier="a" * 43,
                )


if __name__ == "__main__":
    unittest.main()