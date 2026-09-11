from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import (
    AdminSessionGrant,
    AdminSessionGrantEvent,
    Base,
    LocalRootGrant,
    User,
    WorkbenchSession,
)
from cptr.services.privilege_broker import (
    AdminSessionGrantDenied,
    AdminSessionGrantStore,
    DEFAULT_ADMIN_TTL_SECONDS,
    MAX_ADMIN_TTL_SECONDS,
    PrivilegeBroker,
)


class AdminSessionGrantStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name) / 'privilege.db'}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add_all(
                [
                    User(id="u1", role="user", settings={}, created_at=1),
                    User(id="u2", role="user", settings={}, created_at=1),
                    WorkbenchSession(
                        id="wbs_u1",
                        user_id="u1",
                        name="Owned",
                        status="RUNNING",
                        event_count=0,
                        created_at=1,
                        updated_at=1,
                    ),
                ]
            )
            await db.commit()
        self.store = AdminSessionGrantStore()
        self.patches = [
            patch("cptr.services.privilege_broker.get_db", new=self._db),
            patch("cptr.services.workbench_sessions.get_db", new=self._db),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def _db(self):
        return self.sessions()

    async def test_default_grant_is_bounded_and_audited_without_changing_workbench_state(self):
        result = await self.store.grant(owner_id="u1", session_id="wbs_u1", now_ms=1_000)
        self.assertEqual(
            result["expires_at"],
            1_000 + DEFAULT_ADMIN_TTL_SECONDS * 1000,
        )
        self.assertTrue(
            await self.store.is_active(owner_id="u1", session_id="wbs_u1", now_ms=1_001)
        )

        async with self.sessions() as db:
            wb = await db.get(WorkbenchSession, "wbs_u1")
            self.assertEqual(wb.status, "RUNNING")
            self.assertEqual(wb.admin_role, "ADMIN")
            self.assertEqual(wb.role_context["authority"], "admin_session_grant")
            events = list((await db.scalars(select(AdminSessionGrantEvent))).all())
            roots = list((await db.scalars(select(LocalRootGrant))).all())
        self.assertEqual([event.action for event in events], ["granted"])
        self.assertEqual(roots, [])

    async def test_admin_metadata_alone_is_not_authority(self):
        async with self.sessions() as db:
            wb = await db.get(WorkbenchSession, "wbs_u1")
            wb.admin_role = "ADMIN"
            wb.role_context = {"claimed": True}
            await db.commit()

        self.assertFalse(await self.store.is_active(owner_id="u1", session_id="wbs_u1"))
        async with self.sessions() as db:
            wb = await db.get(WorkbenchSession, "wbs_u1")
            self.assertIsNone(wb.admin_role)
            self.assertIsNone(wb.role_context)

    async def test_expiry_revokes_grant_and_clears_projection(self):
        await self.store.grant(owner_id="u1", session_id="wbs_u1", ttl_seconds=1, now_ms=10_000)
        status = await self.store.status(owner_id="u1", session_id="wbs_u1", now_ms=11_001)
        self.assertFalse(status["active"])

        async with self.sessions() as db:
            wb = await db.get(WorkbenchSession, "wbs_u1")
            grant = await db.scalar(select(AdminSessionGrant))
            actions = [
                event.action
                for event in (
                    await db.scalars(
                        select(AdminSessionGrantEvent).order_by(AdminSessionGrantEvent.created_at)
                    )
                ).all()
            ]
        self.assertIsNotNone(grant.revoked_at)
        self.assertIsNone(wb.admin_role)
        self.assertEqual(actions, ["granted", "expired"])

    async def test_owner_isolation_and_bounded_ttl(self):
        with self.assertRaises(AdminSessionGrantDenied):
            await self.store.grant(owner_id="u2", session_id="wbs_u1")
        with self.assertRaises(ValueError):
            await self.store.grant(
                owner_id="u1",
                session_id="wbs_u1",
                ttl_seconds=MAX_ADMIN_TTL_SECONDS + 1,
            )

    async def test_archived_workbench_cannot_retain_admin_authority(self):
        await self.store.grant(owner_id="u1", session_id="wbs_u1", now_ms=1_000)
        async with self.sessions() as db:
            wb = await db.get(WorkbenchSession, "wbs_u1")
            wb.archived_at = 1_500
            await db.commit()
        status = await self.store.status(owner_id="u1", session_id="wbs_u1", now_ms=2_000)
        self.assertFalse(status["active"])
        async with self.sessions() as db:
            wb = await db.get(WorkbenchSession, "wbs_u1")
            grant = await db.scalar(
                select(AdminSessionGrant).where(AdminSessionGrant.workbench_session_id == "wbs_u1")
            )
        self.assertIsNone(wb.admin_role)
        self.assertIsNotNone(grant.revoked_at)

        # Stale ADMIN projection without a grant must also be durably cleared.
        async with self.sessions() as db:
            wb = await db.get(WorkbenchSession, "wbs_u1")
            wb.admin_role = "ADMIN"
            wb.role_context = {"claimed": True}
            await db.commit()
        await self.store.status(owner_id="u1", session_id="wbs_u1", now_ms=3_000)
        async with self.sessions() as db:
            wb = await db.get(WorkbenchSession, "wbs_u1")
        self.assertIsNone(wb.admin_role)
        self.assertIsNone(wb.role_context)


class PrivilegeHierarchyTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_is_separate_and_higher_than_admin(self):
        admin = SimpleNamespace(is_active=AsyncMock(return_value=True))
        root = SimpleNamespace(is_active=AsyncMock(return_value=True))
        broker = PrivilegeBroker(admin_store=admin, root_store=root)

        with patch("cptr.services.local_root_grants.local_root_grants_enabled", return_value=True):
            self.assertEqual(
                await broker.resolve_privilege(owner_id="u1", session_id="wbs_u1"),
                "ROOT",
            )
        with patch("cptr.services.local_root_grants.local_root_grants_enabled", return_value=False):
            self.assertEqual(
                await broker.resolve_privilege(owner_id="u1", session_id="wbs_u1"),
                "ADMIN",
            )

        admin.is_active.return_value = False
        with patch("cptr.services.local_root_grants.local_root_grants_enabled", return_value=False):
            self.assertEqual(
                await broker.resolve_privilege(owner_id="u1", session_id="wbs_u1"),
                "NORMAL",
            )


if __name__ == "__main__":
    unittest.main()
