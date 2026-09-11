"""Tests for EnvironmentProfile and immutable EnvironmentProfileVersion models and validation."""

import tempfile
import time
import unittest
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.models.environment_profile import (
    EnvironmentProfileVersionImmutableError,
    compute_version_digest,
)
from cptr.models.users import User
from cptr.models.workspaces import Workspace
from cptr.services.environment_profile import (
    EnvironmentProfileService,
    EnvironmentProfileVersionNotFoundError,
    InvalidCredentialRefError,
    RawSecretStorageError,
    validate_credential_ref,
    validate_credential_refs,
    validate_environment_variables,
    validate_profile_version_spec,
)


class EnvironmentProfileValidationTests(unittest.TestCase):
    def test_valid_credential_refs_reuse_broker_patterns(self):
        # ai_connection source type
        ref1 = {
            "logical_name": "primary-ai",
            "source_type": "ai_connection",
            "source_ref": "conn-openai-prod",
            "consumers": ["provider.openai.responses", "worker:direct"],
            "target_env_var": "OPENAI_API_KEY",
        }
        val1 = validate_credential_ref(ref1)
        self.assertEqual(val1["logical_name"], "primary-ai")
        self.assertEqual(val1["source_type"], "ai_connection")
        self.assertEqual(val1["source_ref"], "conn-openai-prod")
        self.assertEqual(val1["consumers"], ["provider.openai.responses", "worker:direct"])
        self.assertEqual(val1["target_env_var"], "OPENAI_API_KEY")

        # CamelCase aliases commonly used in API JSON payloads
        ref1_camel = {
            "logicalName": "primary-ai-camel",
            "sourceType": "ai_connection",
            "sourceRef": "conn-openai-prod",
            "consumers": ["provider.openai.responses"],
            "targetEnvVar": "OPENAI_API_KEY",
        }
        val1_camel = validate_credential_ref(ref1_camel)
        self.assertEqual(val1_camel["logical_name"], "primary-ai-camel")
        self.assertEqual(val1_camel["target_env_var"], "OPENAI_API_KEY")

        # encrypted_config source type with allowed config key
        ref2 = {
            "logical_name": "voice-stt",
            "source_type": "encrypted_config",
            "source_ref": "audio.stt_api_key",
            "consumers": ["audio.transcription"],
        }
        val2 = validate_credential_ref(ref2)
        self.assertEqual(val2["logical_name"], "voice-stt")
        self.assertEqual(val2["source_type"], "encrypted_config")
        self.assertEqual(val2["source_ref"], "audio.stt_api_key")

        # vault / credential_broker source types
        ref3 = {
            "logical_name": "github-pat",
            "source_type": "credential_broker",
            "source_ref": "secrets/ci/github_pat",
            "consumers": ["git:push", "git:pull"],
        }
        val3 = validate_credential_ref(ref3)
        self.assertEqual(val3["logical_name"], "github-pat")

        # Multiple unique refs validated together
        multi = validate_credential_refs([ref1, ref2, ref3])
        self.assertEqual(len(multi), 3)

    def test_credential_ref_rejection_of_invalid_fields(self):
        # Non-dict input
        with self.assertRaisesRegex(InvalidCredentialRefError, "mapping"):
            validate_credential_ref("not-a-dict")

        # Blank logical name
        with self.assertRaisesRegex(InvalidCredentialRefError, "logical_name"):
            validate_credential_ref(
                {
                    "logical_name": "",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": ["worker"],
                }
            )

        # Blank or invalid source type
        with self.assertRaisesRegex(
            InvalidCredentialRefError, "Unsupported credential source_type"
        ):
            validate_credential_ref(
                {
                    "logical_name": "key",
                    "source_type": "insecure_plaintext",
                    "source_ref": "conn-1",
                    "consumers": ["worker"],
                }
            )

        # encrypted_config not in allowed broker keys
        with self.assertRaisesRegex(
            InvalidCredentialRefError, "not in allowed encrypted config keys"
        ):
            validate_credential_ref(
                {
                    "logical_name": "root-pwd",
                    "source_type": "encrypted_config",
                    "source_ref": "user.admin_password",
                    "consumers": ["worker"],
                }
            )

        # Empty consumers
        with self.assertRaisesRegex(InvalidCredentialRefError, "at least one exact consumer"):
            validate_credential_ref(
                {
                    "logical_name": "key",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": [],
                }
            )

        # Invalid target_env_var
        with self.assertRaisesRegex(InvalidCredentialRefError, "target_env_var"):
            validate_credential_ref(
                {
                    "logical_name": "key",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": ["worker"],
                    "target_env_var": "123-INVALID-VAR!",
                }
            )

        # Extra disallowed keys
        with self.assertRaisesRegex(InvalidCredentialRefError, "Unsupported keys"):
            validate_credential_ref(
                {
                    "logical_name": "key",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": ["worker"],
                    "random_extra_property": "not_allowed",
                }
            )

        # Duplicate logical names in list
        dup_ref = {
            "logical_name": "key",
            "source_type": "ai_connection",
            "source_ref": "conn-1",
            "consumers": ["worker"],
        }
        with self.assertRaisesRegex(InvalidCredentialRefError, "Duplicate credential reference"):
            validate_credential_refs([dup_ref, dup_ref])

    def test_raw_secret_storage_prevention_in_credential_refs(self):
        # Forbidden keys representing raw secrets
        forbidden_payloads = [
            {
                "logical_name": "a",
                "source_type": "ai_connection",
                "source_ref": "conn",
                "consumers": ["c"],
                "secret": "supersecret",
            },
            {
                "logical_name": "b",
                "source_type": "ai_connection",
                "source_ref": "conn",
                "consumers": ["c"],
                "api_key": "sk-1234567890",
            },
            {
                "logical_name": "c",
                "source_type": "ai_connection",
                "source_ref": "conn",
                "consumers": ["c"],
                "token": "ghp_1234567890",
            },
            {
                "logical_name": "d",
                "source_type": "ai_connection",
                "source_ref": "conn",
                "consumers": ["c"],
                "password": "pass",
            },
            {
                "logical_name": "e",
                "source_type": "ai_connection",
                "source_ref": "conn",
                "consumers": ["c"],
                "raw_key": "private",
            },
            {
                "logical_name": "f",
                "source_type": "ai_connection",
                "source_ref": "conn",
                "consumers": ["c"],
                "value": "secretvalue",
            },
            {
                "logical_name": "g",
                "source_type": "ai_connection",
                "source_ref": "conn",
                "consumers": ["c"],
                "credential": "secretvalue",
            },
        ]
        for bad in forbidden_payloads:
            with self.assertRaises(RawSecretStorageError):
                validate_credential_ref(bad)

        # Source ref formatted like raw token
        with self.assertRaisesRegex(RawSecretStorageError, "raw secret token format"):
            validate_credential_ref(
                {
                    "logical_name": "bad-ref",
                    "source_type": "ai_connection",
                    "source_ref": "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890",
                    "consumers": ["c"],
                }
            )

    def test_raw_secret_storage_prevention_in_environment_variables(self):
        # Valid non-sensitive environment variables
        valid_env = {
            "PYTHONUNBUFFERED": "1",
            "PORT": "8080",
            "ENVIRONMENT": "staging",
            "LOG_LEVEL": "DEBUG",
        }
        res = validate_environment_variables(valid_env)
        self.assertEqual(res["PYTHONUNBUFFERED"], "1")
        self.assertEqual(res["PORT"], "8080")

        # Prohibited sensitive variable names
        sensitive_keys = [
            "OPENAI_API_KEY",
            "API_KEY",
            "SECRET_TOKEN",
            "AUTH_TOKEN",
            "ACCESS_TOKEN",
            "USER_PASSWORD",
            "SSH_PRIVATE_KEY",
        ]
        for key in sensitive_keys:
            with self.assertRaisesRegex(RawSecretStorageError, "Sensitive variable"):
                validate_environment_variables({key: "non-secret-sounding-val"})

        # Prohibited secret-like values in non-sensitive keys
        secret_values = [
            ("CONFIG_VAL", "sk-proj-1234567890abcdefghijklmnopqrstuvwxyz"),
            ("REMOTE_URL", "ghp_123456789012345678901234567890123456"),
            (
                "TOKEN_LOOKALIKE",
                "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDcSemACt8x4iTMCda8Yhe3iZaWbvV5XKSTbuAn0M",
            ),
            ("CERT_DATA", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0..."),
        ]
        for k, v in secret_values:
            with self.assertRaisesRegex(RawSecretStorageError, r"matching a raw secret format"):
                validate_environment_variables({k: v})

    def test_profile_version_spec_validation(self):
        # Full spec validation
        valid_spec = {
            "runtime_profile": "cptr-vm",
            "environment_variables": {"FOO": "bar"},
            "packages": {"python": ["pytest"]},
            "settings": {"cpu": 2},
            "credential_refs": [
                {
                    "logical_name": "my-ref",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": ["worker"],
                }
            ],
        }
        validated = validate_profile_version_spec(valid_spec)
        self.assertEqual(validated["runtime_profile"], "cptr-vm")
        self.assertEqual(validated["environment_variables"], {"FOO": "bar"})
        self.assertEqual(len(validated["credential_refs"]), 1)

        # Non-dict spec raises ValueError
        with self.assertRaises(ValueError):
            validate_profile_version_spec("not-a-dict")

    def test_version_digest_determinism(self):
        spec = {
            "runtime_profile": "cptr-vm",
            "environment_variables": {"FOO": "bar", "BAZ": "qux"},
            "packages": {"python": ["pytest", "fastapi"]},
            "settings": {"cpu_limit": 2, "timeout": 300},
            "credential_refs": [
                {
                    "logical_name": "ai-conn",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": ["worker"],
                }
            ],
        }
        digest1 = compute_version_digest(
            runtime_profile=spec["runtime_profile"],
            environment_variables=spec["environment_variables"],
            packages=spec["packages"],
            settings=spec["settings"],
            credential_refs=spec["credential_refs"],
        )
        self.assertTrue(digest1.startswith("sha256:"))

        # Same spec with different dictionary order produces the identical digest
        digest2 = compute_version_digest(
            runtime_profile="cptr-vm",
            environment_variables={"BAZ": "qux", "FOO": "bar"},
            packages={"python": ["pytest", "fastapi"]},
            settings={"timeout": 300, "cpu_limit": 2},
            credential_refs=[
                {
                    "logical_name": "ai-conn",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": ["worker"],
                }
            ],
        )
        self.assertEqual(digest1, digest2)

        # Different spec produces a different digest
        digest3 = compute_version_digest(
            runtime_profile="cptr-vm",
            environment_variables={"FOO": "bar", "BAZ": "different"},
            packages={"python": ["pytest", "fastapi"]},
            settings={"cpu_limit": 2, "timeout": 300},
            credential_refs=spec["credential_refs"],
        )
        self.assertNotEqual(digest1, digest3)


class EnvironmentProfileModelDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.db_url = f"sqlite+aiosqlite:///{db_path}"
        self.engine = create_async_engine(self.db_url, echo=False)

        # Create all tables including users, workspaces, environment_profiles
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        # Seed a test user and workspace
        async with self.session_factory() as session:
            user = User(
                id="user-123",
                display_name="Test User",
                role="admin",
                settings={},
                created_at=int(time.time()),
            )
            workspace = Workspace(
                id="ws-123",
                user_id="user-123",
                path="/tmp/workspace",
                name="test-workspace",
                data={},
                created_at=int(time.time()),
            )
            session.add(user)
            session.add(workspace)
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_profile_creation_and_version_publishing(self):
        async with self.session_factory() as session:
            profile, version1 = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-123",
                workspace_id="ws-123",
                name="python-ml-sandbox",
                description="Profile for ML sandboxed runs",
                initial_spec={
                    "runtime_profile": "cptr-vm",
                    "environment_variables": {"OMP_NUM_THREADS": "4"},
                    "packages": {"python": ["torch", "numpy"]},
                    "settings": {"memory_mb": 4096},
                    "credential_refs": [
                        {
                            "logical_name": "llm-provider",
                            "source_type": "ai_connection",
                            "source_ref": "conn-openai-4",
                            "consumers": ["worker:direct"],
                            "target_env_var": "OPENAI_API_KEY",
                        }
                    ],
                },
            )
            await session.commit()

            self.assertIsNotNone(profile.id)
            self.assertEqual(profile.name, "python-ml-sandbox")
            self.assertEqual(profile.workspace_id, "ws-123")
            self.assertIsNotNone(version1)
            self.assertEqual(version1.version_number, 1)
            self.assertEqual(profile.active_version_id, version1.id)
            self.assertEqual(version1.runtime_profile, "cptr-vm")
            self.assertEqual(version1.environment_variables, {"OMP_NUM_THREADS": "4"})
            self.assertEqual(len(version1.credential_refs), 1)

            # Publish version 2
            version2 = await EnvironmentProfileService.create_version(
                session,
                profile_id=profile.id,
                spec={
                    "runtime_profile": "cptr-vm",
                    "environment_variables": {"OMP_NUM_THREADS": "8"},
                    "packages": {"python": ["torch", "numpy", "scipy"]},
                    "settings": {"memory_mb": 8192},
                    "credential_refs": [
                        {
                            "logical_name": "llm-provider",
                            "source_type": "ai_connection",
                            "source_ref": "conn-openai-4",
                            "consumers": ["worker:direct"],
                        },
                        {
                            "logical_name": "tts-key",
                            "source_type": "encrypted_config",
                            "source_ref": "audio.tts_api_key",
                            "consumers": ["audio:tts"],
                        },
                    ],
                },
                created_by="user-123",
                make_active=True,
            )
            await session.commit()

            self.assertEqual(version2.version_number, 2)
            self.assertEqual(version2.parent_version_id, version1.id)
            self.assertEqual(profile.active_version_id, version2.id)

            # Retrieve versions list
            versions = await EnvironmentProfileService.list_versions(session, profile.id)
            self.assertEqual(len(versions), 2)
            self.assertEqual([v.version_number for v in versions], [1, 2])

            # Roll back active version to version 1
            updated_profile = await EnvironmentProfileService.set_active_version(
                session, profile_id=profile.id, version_id=version1.id
            )
            await session.commit()
            self.assertEqual(updated_profile.active_version_id, version1.id)

            # List profiles by user and workspace
            profiles = await EnvironmentProfileService.list_profiles(
                session, user_id="user-123", workspace_id="ws-123"
            )
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0].id, profile.id)

            # Archive profile
            archived = await EnvironmentProfileService.archive_profile(session, profile.id)
            await session.commit()
            self.assertTrue(archived.is_archived)

            # Archived profiles excluded by default from list_profiles
            active_profiles = await EnvironmentProfileService.list_profiles(
                session, user_id="user-123"
            )
            self.assertEqual(len(active_profiles), 0)

            # Included when include_archived=True
            all_profiles = await EnvironmentProfileService.list_profiles(
                session, user_id="user-123", include_archived=True
            )
            self.assertEqual(len(all_profiles), 1)

    async def test_immutability_of_profile_version(self):
        async with self.session_factory() as session:
            profile, version = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-123",
                name="immutable-profile",
                initial_spec={
                    "runtime_profile": "default",
                    "environment_variables": {"APP": "test"},
                    "credential_refs": [],
                },
            )
            await session.commit()

            # Attempting to modify any field on an existing version and flushing must raise EnvironmentProfileVersionImmutableError
            version.runtime_profile = "modified-profile"
            with self.assertRaises(EnvironmentProfileVersionImmutableError):
                await session.flush()

    async def test_duplicate_profile_name_rejected_per_user(self):
        async with self.session_factory() as session:
            await EnvironmentProfileService.create_profile(
                session,
                user_id="user-123",
                name="duplicate-name",
            )
            await session.commit()

            # Creating another profile with the exact same user_id and name violates unique constraint
            with self.assertRaises(IntegrityError):
                await EnvironmentProfileService.create_profile(
                    session,
                    user_id="user-123",
                    name="duplicate-name",
                )
                await session.commit()

    async def test_invalid_profile_version_switching(self):
        async with self.session_factory() as session:
            profile1, _ = await EnvironmentProfileService.create_profile(
                session, user_id="user-123", name="prof-1"
            )
            profile2, version2 = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-123",
                name="prof-2",
                initial_spec={"runtime_profile": "default"},
            )
            await session.commit()

            # Non-existent version
            with self.assertRaises(EnvironmentProfileVersionNotFoundError):
                await EnvironmentProfileService.set_active_version(
                    session, profile_id=profile1.id, version_id="non-existent-ver"
                )

            # Version belonging to another profile
            with self.assertRaises(EnvironmentProfileVersionNotFoundError):
                await EnvironmentProfileService.set_active_version(
                    session, profile_id=profile1.id, version_id=version2.id
                )

    def test_raw_secret_bypass_vectors(self):
        """Verify that description, consumers, and settings cannot carry raw secrets."""
        # Token in description field
        with self.assertRaisesRegex(RawSecretStorageError, "description contains a value matching"):
            validate_credential_ref(
                {
                    "logical_name": "x",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": ["worker"],
                    "description": "sk-realkey12345678901234567890",
                }
            )

        # Token in consumers entry
        with self.assertRaisesRegex(RawSecretStorageError, "consumer scope entry contains"):
            validate_credential_ref(
                {
                    "logical_name": "x",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": ["sk-realkey12345678901234567890"],
                }
            )

        # Bearer token in consumers entry
        with self.assertRaisesRegex(RawSecretStorageError, "consumer scope entry contains"):
            validate_credential_ref(
                {
                    "logical_name": "x",
                    "source_type": "ai_connection",
                    "source_ref": "conn-1",
                    "consumers": ["Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIx"],
                }
            )

        # Raw token in settings
        with self.assertRaisesRegex(RawSecretStorageError, r"settings\["):
            validate_profile_version_spec(
                {
                    "runtime_profile": "default",
                    "settings": {"some_key": "sk-realkey12345678901234567890"},
                }
            )

        # Private key in settings
        with self.assertRaisesRegex(RawSecretStorageError, r"settings\["):
            validate_profile_version_spec(
                {
                    "runtime_profile": "default",
                    "settings": {"cert_data": "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."},
                }
            )

        # Legitimate description (non-secret) still accepted
        result = validate_credential_ref(
            {
                "logical_name": "my-key",
                "source_type": "ai_connection",
                "source_ref": "conn-openai-prod",
                "consumers": ["worker:direct"],
                "description": "OpenAI connection for direct coding worker",
            }
        )
        self.assertEqual(result["description"], "OpenAI connection for direct coding worker")

        # Legitimate consumers (non-secret scope identifiers) still accepted
        result2 = validate_credential_ref(
            {
                "logical_name": "tts",
                "source_type": "ai_connection",
                "source_ref": "conn-tts",
                "consumers": ["audio.tts", "worker:audio"],
            }
        )
        self.assertEqual(result2["consumers"], ["audio.tts", "worker:audio"])


if __name__ == "__main__":
    unittest.main()
