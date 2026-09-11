"""Tests for EnvironmentTarget resolution service contract.

Covers:
- Deterministic target registry (no ambiguity, explicit failure on unknown names)
- Ownership enforcement for restricted targets (aws, production)
- runtime_profile acceptance filtering
- TargetUnanchoredError on blank/None target names
- TargetRegistryIntegrityError on duplicate registry construction
- resolve_profile_target / set_profile_target service integration
- Migration: target_name column on EnvironmentProfile
"""

import tempfile
import time
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from cptr.routers.environment_profiles import (
    CreateProfileRequest,
    CreateVersionRequest,
    ResolveTargetRequest,
    SetActiveVersionRequest,
    SetTargetRequest,
    archive_profile,
    create_profile,
    create_version,
    get_profile,
    get_target,
    list_profiles,
    list_targets,
    list_versions,
    resolve_profile_target,
    set_active_version,
    set_profile_target,
)
from cptr.models import Base
from cptr.models.users import User
from cptr.models.workspaces import Workspace
from cptr.services.environment_profile import (
    EnvironmentProfileNotFoundError,
    EnvironmentProfileService,
    EnvironmentProfileVersionNotFoundError,
)
from cptr.services.environment_target import (
    EnvironmentTarget,
    EnvironmentTargetError,
    EnvironmentTargetRegistry,
    TargetNotFoundError,
    TargetOwnershipError,
    TargetRegistryIntegrityError,
    TargetUnanchoredError,
    resolve_target,
    target_registry,
)


class EnvironmentTargetDescriptorTests(unittest.TestCase):
    def test_valid_target_construction(self):
        t = EnvironmentTarget(
            name="local",
            display_name="Local Host",
            execution_class="local",
        )
        self.assertEqual(t.name, "local")
        self.assertEqual(t.execution_class, "local")
        self.assertTrue(t.accepts_runtime_profile("any-profile"))  # empty = unrestricted

    def test_invalid_name_rejected(self):
        with self.assertRaises(ValueError):
            EnvironmentTarget(name="INVALID-UPPER", display_name="x", execution_class="local")
        with self.assertRaises(ValueError):
            EnvironmentTarget(name="123starts-num", display_name="x", execution_class="local")
        with self.assertRaises(ValueError):
            EnvironmentTarget(name="", display_name="x", execution_class="local")

    def test_invalid_execution_class_rejected(self):
        with self.assertRaises(ValueError):
            EnvironmentTarget(name="x", display_name="x", execution_class="unknown")

    def test_runtime_profile_filtering(self):
        t = EnvironmentTarget(
            name="prod",
            display_name="Prod",
            execution_class="production",
            allowed_runtime_profiles=frozenset({"production", "cptr-vm-prod"}),
        )
        self.assertTrue(t.accepts_runtime_profile("production"))
        self.assertTrue(t.accepts_runtime_profile("cptr-vm-prod"))
        self.assertFalse(t.accepts_runtime_profile("default"))
        self.assertFalse(t.accepts_runtime_profile(""))

    def test_unrestricted_runtime_profile(self):
        t = EnvironmentTarget(
            name="aws",
            display_name="AWS",
            execution_class="cloud",
            allowed_runtime_profiles=frozenset(),  # empty = any
        )
        self.assertTrue(t.accepts_runtime_profile("default"))
        self.assertTrue(t.accepts_runtime_profile("cptr-vm"))
        self.assertTrue(t.accepts_runtime_profile("anything"))

    def test_to_dict_excludes_secrets(self):
        t = EnvironmentTarget(
            name="local",
            display_name="Local",
            execution_class="local",
            allowed_runtime_profiles=frozenset({"default"}),
            metadata={"note": "some-note"},  # metadata NOT in to_dict
        )
        d = t.to_dict()
        self.assertIn("name", d)
        self.assertIn("execution_class", d)
        self.assertIn("allowed_runtime_profiles", d)
        self.assertNotIn("metadata", d)  # opaque metadata excluded


class EnvironmentTargetRegistryTests(unittest.TestCase):
    def _make_target(self, name: str, execution_class: str = "local") -> EnvironmentTarget:
        return EnvironmentTarget(
            name=name, display_name=name.title(), execution_class=execution_class
        )

    def test_registry_construction_and_list(self):
        reg = EnvironmentTargetRegistry(
            [
                self._make_target("local"),
                self._make_target("aws", "cloud"),
            ]
        )
        names = [t.name for t in reg.list_targets()]
        self.assertEqual(names, sorted(names))  # stable order
        self.assertEqual(len(names), 2)

    def test_duplicate_name_raises_integrity_error(self):
        with self.assertRaises(TargetRegistryIntegrityError):
            EnvironmentTargetRegistry(
                [
                    self._make_target("local"),
                    self._make_target("local"),  # duplicate
                ]
            )

    def test_get_existing(self):
        reg = EnvironmentTargetRegistry([self._make_target("local")])
        t = reg.get("local")
        self.assertEqual(t.name, "local")

    def test_get_unknown_raises_target_not_found(self):
        reg = EnvironmentTargetRegistry([self._make_target("local")])
        with self.assertRaisesRegex(TargetNotFoundError, "not registered"):
            reg.get("nonexistent")

    def test_get_blank_raises_target_not_found(self):
        reg = EnvironmentTargetRegistry([self._make_target("local")])
        with self.assertRaisesRegex(TargetNotFoundError, "must not be blank"):
            reg.get("")
        with self.assertRaisesRegex(TargetNotFoundError, "must not be blank"):
            reg.get("   ")

    def test_resolve_local_no_ownership_required(self):
        t = EnvironmentTargetRegistry(
            [
                EnvironmentTarget(
                    name="local",
                    display_name="Local",
                    execution_class="local",
                    allowed_runtime_profiles=frozenset({"default"}),
                    requires_explicit_owner=False,
                )
            ]
        ).resolve(
            target_name="local",
            runtime_profile="default",
            owner_user_id=None,
            profile_user_id=None,
        )
        self.assertEqual(t.name, "local")

    def test_resolve_restricted_target_requires_owner(self):
        reg = EnvironmentTargetRegistry(
            [
                EnvironmentTarget(
                    name="production",
                    display_name="Production",
                    execution_class="production",
                    requires_explicit_owner=True,
                )
            ]
        )
        with self.assertRaisesRegex(TargetOwnershipError, "requires an explicit owner_user_id"):
            reg.resolve(
                target_name="production",
                runtime_profile="default",
                owner_user_id=None,
                profile_user_id="user-1",
            )

    def test_resolve_ownership_mismatch(self):
        reg = EnvironmentTargetRegistry(
            [
                EnvironmentTarget(
                    name="aws",
                    display_name="AWS",
                    execution_class="cloud",
                    requires_explicit_owner=True,
                )
            ]
        )
        with self.assertRaisesRegex(TargetOwnershipError, "does not match profile owner"):
            reg.resolve(
                target_name="aws",
                runtime_profile="default",
                owner_user_id="user-A",
                profile_user_id="user-B",
            )

    def test_resolve_runtime_profile_rejected(self):
        reg = EnvironmentTargetRegistry(
            [
                EnvironmentTarget(
                    name="production",
                    display_name="Production",
                    execution_class="production",
                    allowed_runtime_profiles=frozenset({"production"}),
                    requires_explicit_owner=True,
                )
            ]
        )
        with self.assertRaisesRegex(EnvironmentTargetError, "does not accept runtime_profile"):
            reg.resolve(
                target_name="production",
                runtime_profile="default",  # not in allowed set
                owner_user_id="user-1",
                profile_user_id="user-1",
            )


class BuiltinTargetRegistryTests(unittest.TestCase):
    """Tests against the module-level singleton to assert default target contracts."""

    def test_builtin_targets_present(self):
        names = {t.name for t in target_registry.list_targets()}
        self.assertIn("local", names)
        self.assertIn("aws", names)
        self.assertIn("production", names)

    def test_local_accepts_default_runtime(self):
        t = target_registry.get("local")
        self.assertTrue(t.accepts_runtime_profile("default"))

    def test_production_rejects_default_runtime(self):
        t = target_registry.get("production")
        self.assertFalse(t.accepts_runtime_profile("default"))
        self.assertTrue(t.accepts_runtime_profile("production"))

    def test_aws_accepts_any_runtime(self):
        t = target_registry.get("aws")
        self.assertTrue(t.accepts_runtime_profile("anything"))

    def test_production_requires_explicit_owner(self):
        self.assertTrue(target_registry.get("production").requires_explicit_owner)

    def test_aws_requires_explicit_owner(self):
        self.assertTrue(target_registry.get("aws").requires_explicit_owner)

    def test_local_does_not_require_explicit_owner(self):
        self.assertFalse(target_registry.get("local").requires_explicit_owner)


class ResolveTargetFunctionTests(unittest.TestCase):
    def test_blank_target_name_raises_unanchored(self):
        with self.assertRaises(TargetUnanchoredError):
            resolve_target(None)
        with self.assertRaises(TargetUnanchoredError):
            resolve_target("")
        with self.assertRaises(TargetUnanchoredError):
            resolve_target("   ")

    def test_unknown_target_name_raises_not_found(self):
        with self.assertRaisesRegex(TargetNotFoundError, "not registered"):
            resolve_target("nonexistent-target")

    def test_local_resolve_succeeds(self):
        t = resolve_target("local", runtime_profile="default")
        self.assertEqual(t.name, "local")

    def test_production_resolve_without_owner_fails(self):
        with self.assertRaisesRegex(TargetOwnershipError, "requires an explicit owner_user_id"):
            resolve_target(
                "production",
                runtime_profile="production",
                owner_user_id=None,
            )

    def test_production_resolve_with_matching_owner(self):
        t = resolve_target(
            "production",
            runtime_profile="production",
            owner_user_id="user-1",
            profile_user_id="user-1",
        )
        self.assertEqual(t.name, "production")

    def test_production_resolve_with_wrong_runtime_profile_fails(self):
        with self.assertRaisesRegex(EnvironmentTargetError, "does not accept runtime_profile"):
            resolve_target(
                "production",
                runtime_profile="cptr-vm",  # not production-approved
                owner_user_id="user-1",
                profile_user_id="user-1",
            )


class EnvironmentTargetServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Integration tests exercising set_profile_target and resolve_profile_target
    through the EnvironmentProfileService against a real in-memory SQLite DB."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.db_url = f"sqlite+aiosqlite:///{db_path}"
        self.engine = create_async_engine(self.db_url, echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            user = User(
                id="user-env-target",
                display_name="Target Test User",
                role="admin",
                settings={},
                created_at=int(time.time()),
            )
            workspace = Workspace(
                id="ws-env-target",
                user_id="user-env-target",
                path="/tmp/env-target-ws",
                name="env-target-workspace",
                data={},
                created_at=int(time.time()),
            )
            session.add(user)
            session.add(workspace)
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def _create_profile_with_spec(self, session, name: str, runtime: str = "default"):
        profile, version = await EnvironmentProfileService.create_profile(
            session,
            user_id="user-env-target",
            workspace_id="ws-env-target",
            name=name,
            initial_spec={
                "runtime_profile": runtime,
                "environment_variables": {"APP_ENV": "test"},
                "credential_refs": [],
            },
        )
        await session.commit()
        return profile, version

    async def test_set_and_get_target_name_local(self):
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(session, "target-local-test")
            self.assertIsNone(getattr(profile, "target_name", None))

            updated = await EnvironmentProfileService.set_profile_target(
                session,
                profile_id=profile.id,
                target_name="local",
                user_id="user-env-target",
            )
            await session.commit()
            self.assertEqual(updated.target_name, "local")

    async def test_set_unknown_target_raises_not_found(self):
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(session, "target-unknown-test")
            with self.assertRaises(TargetNotFoundError):
                await EnvironmentProfileService.set_profile_target(
                    session,
                    profile_id=profile.id,
                    target_name="does-not-exist",
                    user_id="user-env-target",
                )

    async def test_set_target_to_none_unanchors_profile(self):
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(session, "target-unanchor-test")
            # First set
            await EnvironmentProfileService.set_profile_target(
                session,
                profile_id=profile.id,
                target_name="local",
                user_id="user-env-target",
            )
            await session.commit()
            # Then unanchor
            updated = await EnvironmentProfileService.set_profile_target(
                session,
                profile_id=profile.id,
                target_name=None,
                user_id="user-env-target",
            )
            await session.commit()
            self.assertIsNone(updated.target_name)

    async def test_resolve_profile_target_local(self):
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(
                session, "resolve-local-test", runtime="default"
            )
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=profile.id, target_name="local", user_id="user-env-target"
            )
            await session.commit()

            target = await EnvironmentProfileService.resolve_profile_target(
                session,
                profile_id=profile.id,
                caller_user_id="user-env-target",
            )
            self.assertEqual(target.name, "local")
            self.assertEqual(target.execution_class, "local")

    async def test_resolve_profile_target_unanchored_raises(self):
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(session, "resolve-unanchored-test")
            # No target_name set
            with self.assertRaises(TargetUnanchoredError):
                await EnvironmentProfileService.resolve_profile_target(
                    session,
                    profile_id=profile.id,
                    caller_user_id="user-env-target",
                )

    async def test_resolve_profile_target_override(self):
        """Override lets callers pass a target without storing it on the profile."""
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(session, "resolve-override-test")
            # Profile is unanchored, but override resolves it
            target = await EnvironmentProfileService.resolve_profile_target(
                session,
                profile_id=profile.id,
                caller_user_id="user-env-target",
                target_name_override="local",
            )
            self.assertEqual(target.name, "local")

    async def test_resolve_profile_no_active_version_raises(self):
        async with self.session_factory() as session:
            profile, _ = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-env-target",
                name="no-version-resolve-test",
                # No initial_spec → no version
            )
            await session.commit()
            # Target is set but no active version
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=profile.id, target_name="local", user_id="user-env-target"
            )
            await session.commit()
            with self.assertRaises(EnvironmentProfileVersionNotFoundError):
                await EnvironmentProfileService.resolve_profile_target(
                    session,
                    profile_id=profile.id,
                    caller_user_id="user-env-target",
                )

    async def test_resolve_profile_production_target_runtime_mismatch(self):
        """Profile with runtime_profile='default' cannot resolve to production target."""
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(
                session, "prod-runtime-mismatch-test", runtime="default"
            )
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=profile.id, target_name="production", user_id="user-env-target"
            )
            await session.commit()
            with self.assertRaises(EnvironmentTargetError):
                await EnvironmentProfileService.resolve_profile_target(
                    session,
                    profile_id=profile.id,
                    caller_user_id="user-env-target",
                )

    async def test_resolve_profile_production_target_correct_runtime(self):
        """Profile with runtime_profile='production' resolves correctly with owner."""
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(
                session, "prod-runtime-ok-test", runtime="production"
            )
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=profile.id, target_name="production", user_id="user-env-target"
            )
            await session.commit()
            target = await EnvironmentProfileService.resolve_profile_target(
                session,
                profile_id=profile.id,
                caller_user_id="user-env-target",
            )
            self.assertEqual(target.name, "production")
            self.assertEqual(target.execution_class, "production")

    async def test_set_profile_target_not_found_raises(self):
        async with self.session_factory() as session:
            with self.assertRaises(EnvironmentProfileNotFoundError):
                await EnvironmentProfileService.set_profile_target(
                    session,
                    profile_id="nonexistent-profile-id",
                    target_name="local",
                    user_id="user-env-target",
                )

    async def test_target_name_persists_across_sessions(self):
        """Verify target_name is written to DB and retrievable in a fresh session."""
        profile_id = None
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(session, "persist-target-test")
            profile_id = profile.id
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=profile_id, target_name="local", user_id="user-env-target"
            )
            await session.commit()

        # Fresh session
        async with self.session_factory() as session:
            reloaded = await EnvironmentProfileService.get_profile(session, profile_id)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.target_name, "local")

    async def test_set_profile_target_wrong_owner_raises(self):
        """Caller cannot set target on a profile owned by another user."""
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(session, "set-target-wrong-owner")
            with self.assertRaises(TargetOwnershipError):
                await EnvironmentProfileService.set_profile_target(
                    session,
                    profile_id=profile.id,
                    target_name="local",
                    user_id="other-user",
                )

    async def test_resolve_profile_target_wrong_caller_raises(self):
        """Caller cannot resolve target for a profile owned by another user."""
        async with self.session_factory() as session:
            profile, _ = await self._create_profile_with_spec(
                session, "resolve-target-wrong-caller"
            )
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=profile.id, target_name="local", user_id="user-env-target"
            )
            await session.commit()
            with self.assertRaises(TargetOwnershipError):
                await EnvironmentProfileService.resolve_profile_target(
                    session,
                    profile_id=profile.id,
                    caller_user_id="other-user",
                )


class SourceWorkspaceTargetSeparationTests(unittest.IsolatedAsyncioTestCase):
    """Verifies strict separation between authoring source workspaces and runtime deployment targets.

    Asserts:
    - Profile workspace_id identifies the authoring workspace context, not the execution target.
    - Profiles created in distinct workspaces can independently target local, aws, or production.
    - Queries filtered by workspace_id partition profiles cleanly without cross-workspace leakage.
    - Execution classes (local, cloud, production) are properties of the target environment,
      never conflated with workspace identifiers or paths.
    - Arbitrary paths or workspace IDs are rejected as target names.
    """

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test_sep.db"
        self.db_url = f"sqlite+aiosqlite:///{db_path}"
        self.engine = create_async_engine(self.db_url, echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            user = User(
                id="user-sep",
                display_name="Separation User",
                role="admin",
                settings={},
                created_at=int(time.time()),
            )
            ws_dev = Workspace(
                id="ws-dev",
                user_id="user-sep",
                path="/home/shacker/repos/dev",
                name="dev-repo",
                data={},
                created_at=int(time.time()),
            )
            ws_staging = Workspace(
                id="ws-staging",
                user_id="user-sep",
                path="/home/shacker/repos/staging",
                name="staging-repo",
                data={},
                created_at=int(time.time()),
            )
            ws_prod = Workspace(
                id="ws-prod",
                user_id="user-sep",
                path="/home/shacker/repos/prod",
                name="prod-repo",
                data={},
                created_at=int(time.time()),
            )
            session.add_all([user, ws_dev, ws_staging, ws_prod])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_workspace_isolation_and_distinct_target_assignment(self):
        async with self.session_factory() as session:
            # 1. Dev workspace profile targets 'local'
            prof_dev, _ = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-sep",
                workspace_id="ws-dev",
                name="dev-profile",
                initial_spec={
                    "runtime_profile": "default",
                    "environment_variables": {"STAGE": "dev"},
                },
            )
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=prof_dev.id, target_name="local", user_id="user-sep"
            )

            # 2. Staging workspace profile targets 'aws' (cloud)
            prof_staging, _ = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-sep",
                workspace_id="ws-staging",
                name="staging-profile",
                initial_spec={
                    "runtime_profile": "default",
                    "environment_variables": {"STAGE": "staging"},
                },
            )
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=prof_staging.id, target_name="aws", user_id="user-sep"
            )

            # 3. Prod workspace profile targets 'production'
            prof_prod, _ = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-sep",
                workspace_id="ws-prod",
                name="prod-profile",
                initial_spec={
                    "runtime_profile": "production",
                    "environment_variables": {"STAGE": "prod"},
                },
            )
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=prof_prod.id, target_name="production", user_id="user-sep"
            )

            # 4. Global un-scoped profile (workspace_id=None) targets 'local'
            prof_global, _ = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-sep",
                workspace_id=None,
                name="global-profile",
                initial_spec={
                    "runtime_profile": "default",
                    "environment_variables": {"STAGE": "global"},
                },
            )
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=prof_global.id, target_name="local", user_id="user-sep"
            )
            await session.commit()

            # Verify workspace-scoped querying partitions profiles cleanly
            dev_profiles = await EnvironmentProfileService.list_profiles(
                session, user_id="user-sep", workspace_id="ws-dev"
            )
            self.assertEqual(len(dev_profiles), 1)
            self.assertEqual(dev_profiles[0].id, prof_dev.id)

            staging_profiles = await EnvironmentProfileService.list_profiles(
                session, user_id="user-sep", workspace_id="ws-staging"
            )
            self.assertEqual(len(staging_profiles), 1)
            self.assertEqual(staging_profiles[0].id, prof_staging.id)

            prod_profiles = await EnvironmentProfileService.list_profiles(
                session, user_id="user-sep", workspace_id="ws-prod"
            )
            self.assertEqual(len(prod_profiles), 1)
            self.assertEqual(prod_profiles[0].id, prof_prod.id)

            # All profiles for user
            all_profiles = await EnvironmentProfileService.list_profiles(
                session, user_id="user-sep"
            )
            self.assertEqual(len(all_profiles), 4)

            # Resolve targets independently and assert execution classes
            target_dev = await EnvironmentProfileService.resolve_profile_target(
                session, profile_id=prof_dev.id, caller_user_id="user-sep"
            )
            self.assertEqual(target_dev.name, "local")
            self.assertEqual(target_dev.execution_class, "local")

            target_staging = await EnvironmentProfileService.resolve_profile_target(
                session, profile_id=prof_staging.id, caller_user_id="user-sep"
            )
            self.assertEqual(target_staging.name, "aws")
            self.assertEqual(target_staging.execution_class, "cloud")

            target_prod = await EnvironmentProfileService.resolve_profile_target(
                session, profile_id=prof_prod.id, caller_user_id="user-sep"
            )
            self.assertEqual(target_prod.name, "production")
            self.assertEqual(target_prod.execution_class, "production")

    async def test_workspace_path_or_id_rejected_as_target_name(self):
        async with self.session_factory() as session:
            prof, _ = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-sep",
                workspace_id="ws-dev",
                name="test-reject-target",
                initial_spec={"runtime_profile": "default"},
            )
            # Cannot set a workspace ID or filesystem path as target name
            with self.assertRaises(TargetNotFoundError):
                await EnvironmentProfileService.set_profile_target(
                    session, profile_id=prof.id, target_name="ws-dev", user_id="user-sep"
                )
            with self.assertRaises(ValueError):
                # Slashes violate target identifier regex ^[a-z][a-z0-9_-]{0,63}$
                EnvironmentTarget(
                    name="/home/shacker/repos/dev", display_name="dev", execution_class="local"
                )


class EnvironmentProfilesRouterApiTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end unit tests for the environment profiles REST router."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test_api.db"
        self.db_url = f"sqlite+aiosqlite:///{db_path}"
        self.engine = create_async_engine(self.db_url, echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            user1 = User(
                id="user-api-1", display_name="User 1", role="user", created_at=int(time.time())
            )
            user2 = User(
                id="user-api-2", display_name="User 2", role="user", created_at=int(time.time())
            )
            ws1 = Workspace(
                id="ws-api-1",
                user_id="user-api-1",
                path="/tmp/ws1",
                name="ws1",
                created_at=int(time.time()),
            )
            session.add_all([user1, user2, ws1])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    def _request(self, user_id: str | None = "user-api-1"):
        auth = SimpleNamespace(user_id=user_id, role="user") if user_id else None
        return SimpleNamespace(state=SimpleNamespace(auth=auth))

    async def _mock_get_db(self):
        return self.session_factory()

    async def test_list_and_get_targets(self):
        targets_res = await list_targets()
        target_names = [t["name"] for t in targets_res["targets"]]
        self.assertIn("local", target_names)
        self.assertIn("aws", target_names)
        self.assertIn("production", target_names)

        local_t = await get_target("local")
        self.assertEqual(local_t["name"], "local")
        self.assertEqual(local_t["execution_class"], "local")

        with self.assertRaises(HTTPException) as ctx:
            await get_target("nonexistent-target")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_auth_required_for_profiles(self):
        req = self._request(None)
        with self.assertRaises(HTTPException) as ctx:
            await list_profiles(req)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_profile_and_version_lifecycle_endpoints(self):
        req = self._request("user-api-1")

        with patch("cptr.routers.environment_profiles.get_db", side_effect=self._mock_get_db):
            # 1. Create profile with initial spec and target
            create_req = CreateProfileRequest(
                name="api-profile",
                description="Profile created via API",
                workspace_id="ws-api-1",
                target_name="local",
                initial_spec={
                    "runtime_profile": "default",
                    "environment_variables": {"API_MODE": "test"},
                    "packages": ["fastapi"],
                },
            )
            created = await create_profile(req, create_req)
            self.assertEqual(created["profile"]["name"], "api-profile")
            self.assertEqual(created["profile"]["target_name"], "local")
            self.assertIsNotNone(created["version"])
            profile_id = created["profile"]["id"]
            ver1_id = created["version"]["id"]

            # 2. Query profile by ID
            fetched = await get_profile(req, profile_id)
            self.assertEqual(fetched["id"], profile_id)

            # 3. Create second version
            ver_req = CreateVersionRequest(
                spec={
                    "runtime_profile": "default",
                    "environment_variables": {"API_MODE": "v2"},
                    "packages": ["fastapi", "uvicorn"],
                },
                make_active=True,
            )
            ver2 = await create_version(req, profile_id, ver_req)
            self.assertEqual(ver2["version_number"], 2)

            # 4. List versions
            ver_list = await list_versions(req, profile_id)
            self.assertEqual(len(ver_list["versions"]), 2)

            # 5. Switch active version back to v1
            switched = await set_active_version(
                req, profile_id, SetActiveVersionRequest(version_id=ver1_id)
            )
            self.assertEqual(switched["active_version_id"], ver1_id)

            # 6. Resolve target
            resolved = await resolve_profile_target(req, profile_id, ResolveTargetRequest())
            self.assertEqual(resolved["resolved_target"]["name"], "local")

            # 7. Update target to aws
            updated_tgt = await set_profile_target(
                req, profile_id, SetTargetRequest(target_name="aws")
            )
            self.assertEqual(updated_tgt["target_name"], "aws")

            # 8. Resolve target for aws
            resolved_aws = await resolve_profile_target(req, profile_id, ResolveTargetRequest())
            self.assertEqual(resolved_aws["resolved_target"]["name"], "aws")
            self.assertEqual(resolved_aws["resolved_target"]["execution_class"], "cloud")

            # 9. Unanchor target by setting to null
            unanchored = await set_profile_target(
                req, profile_id, SetTargetRequest(target_name=None)
            )
            self.assertIsNone(unanchored["target_name"])

            # 10. Resolving unanchored profile raises 409 TARGET_UNANCHORED
            with self.assertRaises(HTTPException) as ctx:
                await resolve_profile_target(req, profile_id, ResolveTargetRequest())
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.detail["code"], "TARGET_UNANCHORED")

            # 11. Resolving with override succeeds
            resolved_override = await resolve_profile_target(
                req, profile_id, ResolveTargetRequest(target_name_override="local")
            )
            self.assertEqual(resolved_override["resolved_target"]["name"], "local")

            # 12. Archive profile
            archived = await archive_profile(req, profile_id)
            self.assertTrue(archived["is_archived"])

    async def test_owner_scoping_enforced_across_users(self):
        req1 = self._request("user-api-1")
        req2 = self._request("user-api-2")

        with patch("cptr.routers.environment_profiles.get_db", side_effect=self._mock_get_db):
            # User 1 creates a profile
            created = await create_profile(
                req1,
                CreateProfileRequest(
                    name="user1-exclusive",
                    target_name="local",
                    initial_spec={"runtime_profile": "default"},
                ),
            )
            profile_id = created["profile"]["id"]

            # User 2 attempts to get User 1's profile -> 404
            with self.assertRaises(HTTPException) as ctx:
                await get_profile(req2, profile_id)
            self.assertEqual(ctx.exception.status_code, 404)

            # User 2 attempts to set target on User 1's profile -> 404
            with self.assertRaises(HTTPException) as ctx:
                await set_profile_target(req2, profile_id, SetTargetRequest(target_name="local"))
            self.assertEqual(ctx.exception.status_code, 404)

            # User 2 attempts to resolve target on User 1's profile -> 404
            with self.assertRaises(HTTPException) as ctx:
                await resolve_profile_target(req2, profile_id, ResolveTargetRequest())
            self.assertEqual(ctx.exception.status_code, 404)


class EnvironmentTargetRestartMigrationCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """Verifies Alembic migration 0038 compatibility, restart idempotence, and persistence
    of versioned Environment Profiles, target resolution, owner scoping, and credential refs.
    """

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "restart_compat.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"

        # Run Alembic migrations to head (0038)
        from alembic import command
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", "cptr/migrations")
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")
        command.upgrade(cfg, "head")

        self.engine = create_async_engine(self.db_url, echo=False)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            user1 = User(
                id="user-restart-1", display_name="User 1", role="user", created_at=int(time.time())
            )
            user2 = User(
                id="user-restart-2", display_name="User 2", role="user", created_at=int(time.time())
            )
            ws1 = Workspace(
                id="ws-restart-1",
                user_id="user-restart-1",
                path="/tmp/ws-restart-1",
                name="ws-restart-1",
                created_at=int(time.time()),
            )
            session.add_all([user1, user2, ws1])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_migration_and_restart_preserves_profiles_and_targets(self):
        # 1. Create profile with version and credential references
        async with self.session_factory() as session:
            prof, ver = await EnvironmentProfileService.create_profile(
                session,
                user_id="user-restart-1",
                workspace_id="ws-restart-1",
                name="restart-profile",
                description="Profile for restart verification",
                initial_spec={
                    "runtime_profile": "cptr-vm",
                    "environment_variables": {"STAGE": "pre-restart", "DEBUG": "0"},
                    "packages": {"python": ["pytest", "fastapi"]},
                    "settings": {"cpu": 4, "memory_mb": 8192},
                    "credential_refs": [
                        {
                            "logical_name": "db-vault-ref",
                            "source_type": "credential_broker",
                            "source_ref": "secrets/db/conn",
                            "consumers": ["worker:direct", "service:backend"],
                        },
                        {
                            "logical_name": "ai-key-ref",
                            "source_type": "ai_connection",
                            "source_ref": "conn-anthropic",
                            "consumers": ["provider:anthropic"],
                            "target_env_var": "ANTHROPIC_API_KEY",
                            "description": "Anthropic API connection",
                        },
                    ],
                },
            )
            await EnvironmentProfileService.set_profile_target(
                session, profile_id=prof.id, target_name="local", user_id="user-restart-1"
            )
            await session.commit()
            profile_id = prof.id
            ver_id = ver.id
            digest = ver.digest

        # 2. Simulate server restart: re-run Alembic migration upgrade on existing DB
        from alembic import command
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", "cptr/migrations")
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")
        command.upgrade(cfg, "head")

        # Dispose and re-create engine / session factory
        await self.engine.dispose()
        self.engine = create_async_engine(self.db_url, echo=False)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        # 3. Verify state after restart
        async with self.session_factory() as session:
            reloaded_prof = await EnvironmentProfileService.get_profile(session, profile_id)
            self.assertIsNotNone(reloaded_prof)
            self.assertEqual(reloaded_prof.name, "restart-profile")
            self.assertEqual(reloaded_prof.target_name, "local")
            self.assertEqual(reloaded_prof.active_version_id, ver_id)

            reloaded_ver = await EnvironmentProfileService.get_version(session, ver_id)
            self.assertIsNotNone(reloaded_ver)
            self.assertEqual(reloaded_ver.digest, digest)
            self.assertEqual(reloaded_ver.runtime_profile, "cptr-vm")
            self.assertEqual(len(reloaded_ver.credential_refs), 2)
            self.assertEqual(reloaded_ver.credential_refs[0]["logical_name"], "db-vault-ref")
            self.assertEqual(reloaded_ver.credential_refs[1]["logical_name"], "ai-key-ref")

            # Verify target resolution survives restart
            target = await EnvironmentProfileService.resolve_profile_target(
                session,
                profile_id=profile_id,
                caller_user_id="user-restart-1",
            )
            self.assertEqual(target.name, "local")
            self.assertEqual(target.execution_class, "local")

            # Verify owner scoping remains enforced after restart
            with self.assertRaises(TargetOwnershipError):
                await EnvironmentProfileService.resolve_profile_target(
                    session,
                    profile_id=profile_id,
                    caller_user_id="user-restart-2",
                )

            with self.assertRaises(TargetOwnershipError):
                await EnvironmentProfileService.set_profile_target(
                    session,
                    profile_id=profile_id,
                    target_name="aws",
                    user_id="user-restart-2",
                )

            # Verify immutability hook is still active after restart
            reloaded_ver.runtime_profile = "hacked"
            from cptr.models.environment_profile import EnvironmentProfileVersionImmutableError

            with self.assertRaises(EnvironmentProfileVersionImmutableError):
                await session.flush()


if __name__ == "__main__":
    unittest.main()
