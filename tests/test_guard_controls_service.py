import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, User
from cptr.services.guard_controls import (
    GuardImmutableError,
    GuardNotFoundError,
    GuardVersionConflict,
    GuardPolicyService,
)


class GuardPolicyServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name) / 'guards.db'}"
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add_all(
                [
                    User(id="user-1", role="admin", settings={}, created_at=1),
                    User(id="user-2", role="user", settings={}, created_at=1),
                ]
            )
            await db.commit()
        self.service = GuardPolicyService()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def _db(self):
        return self.sessions()

    async def test_defaults_are_enabled_and_locked_invariants_are_immutable(self):
        with patch("cptr.services.guard_controls.get_db", new=self._db):
            catalog = await self.service.catalog("user-1")
            mutable = {item["id"]: item for item in catalog["guards"] if item["mutable"]}
            locked = {item["id"]: item for item in catalog["guards"] if not item["mutable"]}

            self.assertTrue(mutable["delegation_prompt_approval"]["enabled"])
            self.assertEqual(mutable["delegation_prompt_approval"]["version"], 0)
            self.assertTrue(locked["identity_authentication"]["enabled"])
            self.assertEqual(locked["identity_authentication"]["version"], 0)

            with self.assertRaises(GuardImmutableError):
                await self.service.set_guard(
                    "user-1",
                    "identity_authentication",
                    enabled=False,
                    expected_version=0,
                )

    async def test_set_guard_is_owner_scoped_versioned_and_persistent(self):
        with patch("cptr.services.guard_controls.get_db", new=self._db):
            changed = await self.service.set_guard(
                "user-1",
                "delegation_prompt_approval",
                enabled=False,
                expected_version=0,
            )
            self.assertFalse(changed["enabled"])
            self.assertEqual(changed["version"], 1)
            self.assertFalse(
                await self.service.is_enabled("user-1", "delegation_prompt_approval")
            )
            self.assertTrue(
                await self.service.is_enabled("user-2", "delegation_prompt_approval")
            )

            restarted = GuardPolicyService()
            self.assertFalse(
                await restarted.is_enabled("user-1", "delegation_prompt_approval")
            )

    async def test_stale_expected_version_fails_without_mutation(self):
        with patch("cptr.services.guard_controls.get_db", new=self._db):
            first = await self.service.set_guard(
                "user-1",
                "secret_write_prompt_approval",
                enabled=False,
                expected_version=0,
            )
            self.assertEqual(first["version"], 1)
            with self.assertRaises(GuardVersionConflict) as raised:
                await self.service.set_guard(
                    "user-1",
                    "secret_write_prompt_approval",
                    enabled=True,
                    expected_version=0,
                )
            self.assertEqual(raised.exception.current["version"], 1)
            self.assertFalse(
                await self.service.is_enabled("user-1", "secret_write_prompt_approval")
            )

    async def test_unknown_guard_fails_closed(self):
        with patch("cptr.services.guard_controls.get_db", new=self._db):
            with self.assertRaises(GuardNotFoundError):
                await self.service.is_enabled("user-1", "not-a-real-guard")
            with self.assertRaises(GuardNotFoundError):
                await self.service.set_guard(
                    "user-1", "not-a-real-guard", enabled=False, expected_version=0
                )

    async def test_mutation_appends_audit_event(self):
        from cptr.models.guard_controls import GuardSettingEvent

        with patch("cptr.services.guard_controls.get_db", new=self._db):
            await self.service.set_guard(
                "user-1",
                "browser_evaluate_approval",
                enabled=False,
                expected_version=0,
                source="mcp_ui",
                now_ms=1234,
            )
        async with self.sessions() as db:
            events = list((await db.execute(select(GuardSettingEvent))).scalars().all())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].user_id, "user-1")
        self.assertEqual(events[0].guard_id, "browser_evaluate_approval")
        self.assertTrue(events[0].previous_enabled)
        self.assertFalse(events[0].new_enabled)
        self.assertEqual(events[0].version, 1)
        self.assertEqual(events[0].source, "mcp_ui")
        self.assertEqual(events[0].created_at, 1234)

    async def test_reset_restores_defaults_and_preserves_monotonic_versions(self):
        with patch("cptr.services.guard_controls.get_db", new=self._db):
            disabled = await self.service.set_guard(
                "user-1",
                "network_operation_approval",
                enabled=False,
                expected_version=0,
            )
            result = await self.service.reset(
                "user-1",
                expected_versions={"network_operation_approval": disabled["version"]},
                now_ms=2000,
            )
            by_id = {item["id"]: item for item in result["guards"]}
            self.assertTrue(by_id["network_operation_approval"]["enabled"])
            self.assertEqual(by_id["network_operation_approval"]["version"], 2)

            with self.assertRaises(GuardVersionConflict):
                await self.service.reset(
                    "user-1",
                    expected_versions={"network_operation_approval": 1},
                    now_ms=3000,
                )


if __name__ == "__main__":
    unittest.main()
