import tempfile
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, User, WorkbenchSession
from cptr.services.local_root_grants import (
    LocalRootGrantDenied,
    LocalRootGrantStore,
    ROOT_GRANT_MARKER,
    ROOT_REVOKE_MARKER,
    parse_root_command_directive,
)


class LocalRootDirectiveTests(unittest.TestCase):
    def test_grant_marker_is_stripped_and_defaults_to_unlimited_session_grant(self):
        directive = parse_root_command_directive(
            f"{ROOT_GRANT_MARKER}\nprintf root-enabled"
        )
        self.assertTrue(directive.grant)
        self.assertFalse(directive.revoke)
        self.assertIsNone(directive.ttl_seconds)
        self.assertEqual(directive.command, "printf root-enabled")

    def test_explicit_ttl_is_only_accepted_directly_after_grant_marker(self):
        directive = parse_root_command_directive(
            f"{ROOT_GRANT_MARKER}\n# cptr-root-ttl-seconds: 90\nprintf ok"
        )
        self.assertTrue(directive.grant)
        self.assertEqual(directive.ttl_seconds, 90)
        self.assertEqual(directive.command, "printf ok")

    def test_revoke_marker_is_stripped(self):
        directive = parse_root_command_directive(
            f"{ROOT_REVOKE_MARKER}\nprintf user-mode"
        )
        self.assertFalse(directive.grant)
        self.assertTrue(directive.revoke)
        self.assertEqual(directive.command, "printf user-mode")

    def test_marker_inside_script_does_not_change_authority(self):
        command = f"printf '%s' '{ROOT_GRANT_MARKER}'"
        directive = parse_root_command_directive(command)
        self.assertFalse(directive.grant)
        self.assertFalse(directive.revoke)
        self.assertEqual(directive.command, command)

    def test_invalid_ttl_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_root_command_directive(
                f"{ROOT_GRANT_MARKER}\n# cptr-root-ttl-seconds: forever\nprintf ok"
            )


class LocalRootGrantStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name) / 'root-grants.db'}"
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.factory() as db:
            db.add_all(
                [
                    User(id="user-1", role="user", settings={}, created_at=1),
                    User(id="user-2", role="user", settings={}, created_at=1),
                ]
            )
            db.add(
                WorkbenchSession(
                    id="wbs_root_session_123456",
                    user_id="user-1",
                    name="Root work",
                    status="OPEN",
                    event_count=0,
                    created_at=1,
                    updated_at=1,
                )
            )
            await db.commit()
        self.store = LocalRootGrantStore()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def _db(self):
        return self.factory()

    async def test_default_grant_has_no_expiry_and_is_active(self):
        from unittest.mock import patch

        with patch("cptr.services.local_root_grants.get_db", new=self._db):
            grant = await self.store.grant(
                owner_id="user-1",
                session_id="wbs_root_session_123456",
                now_ms=1_000,
            )
            self.assertIsNone(grant["expires_at"])
            self.assertTrue(
                await self.store.is_active(
                    owner_id="user-1",
                    session_id="wbs_root_session_123456",
                    now_ms=9_999_999,
                )
            )

    async def test_explicit_ttl_expires_and_revokes_grant(self):
        from unittest.mock import patch

        with patch("cptr.services.local_root_grants.get_db", new=self._db):
            grant = await self.store.grant(
                owner_id="user-1",
                session_id="wbs_root_session_123456",
                ttl_seconds=2,
                now_ms=1_000,
            )
            self.assertEqual(grant["expires_at"], 3_000)
            self.assertTrue(
                await self.store.is_active(
                    owner_id="user-1",
                    session_id="wbs_root_session_123456",
                    now_ms=2_999,
                )
            )
            self.assertFalse(
                await self.store.is_active(
                    owner_id="user-1",
                    session_id="wbs_root_session_123456",
                    now_ms=3_000,
                )
            )

    async def test_revoke_disables_existing_grant(self):
        from unittest.mock import patch

        with patch("cptr.services.local_root_grants.get_db", new=self._db):
            await self.store.grant(
                owner_id="user-1",
                session_id="wbs_root_session_123456",
                now_ms=1_000,
            )
            self.assertEqual(
                await self.store.revoke(
                    owner_id="user-1",
                    session_id="wbs_root_session_123456",
                    now_ms=2_000,
                ),
                1,
            )
            self.assertFalse(
                await self.store.is_active(
                    owner_id="user-1",
                    session_id="wbs_root_session_123456",
                    now_ms=2_001,
                )
            )

    async def test_grant_is_owner_scoped(self):
        from unittest.mock import patch

        with patch("cptr.services.local_root_grants.get_db", new=self._db):
            await self.store.grant(
                owner_id="user-1",
                session_id="wbs_root_session_123456",
                now_ms=1_000,
            )
            self.assertFalse(
                await self.store.is_active(
                    owner_id="user-2",
                    session_id="wbs_root_session_123456",
                    now_ms=1_001,
                )
            )
            with self.assertRaises(LocalRootGrantDenied):
                await self.store.grant(
                    owner_id="user-2",
                    session_id="wbs_root_session_123456",
                    now_ms=1_002,
                )

    async def test_archived_workbench_makes_grant_inactive(self):
        from unittest.mock import patch

        with patch("cptr.services.local_root_grants.get_db", new=self._db):
            await self.store.grant(
                owner_id="user-1",
                session_id="wbs_root_session_123456",
                now_ms=1_000,
            )
            async with self.factory() as db:
                session = await db.get(WorkbenchSession, "wbs_root_session_123456")
                session.archived_at = 2_000
                session.status = "ARCHIVED"
                await db.commit()
            self.assertFalse(
                await self.store.is_active(
                    owner_id="user-1",
                    session_id="wbs_root_session_123456",
                    now_ms=2_001,
                )
            )


if __name__ == "__main__":
    unittest.main()
