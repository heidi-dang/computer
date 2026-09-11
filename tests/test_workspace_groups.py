"""Tests for WorkspaceGroup and WorkspaceGroupMember domain and service."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models.users import User
from cptr.models.workspaces import (
    Workspace,
    WorkspaceGroup,
    WorkspaceGroupMember,
)
from cptr.services.workspace_groups import (
    WorkspaceGroupDuplicateMemberError,
    WorkspaceGroupNotFoundError,
    WorkspaceGroupService,
    WorkspaceGroupSlugConflictError,
    WorkspaceGroupValidationError,
    WorkspaceGroupWorkspaceNotFoundError,
)


class WorkspaceGroupDomainTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _alembic_config(db_path: Path) -> Config:
        cfg = Config()
        cfg.set_main_option(
            "script_location",
            str(Path(__file__).resolve().parents[1] / "cptr" / "migrations"),
        )
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        return cfg

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_groups.db"
        cfg = self._alembic_config(self.db_path)
        command.upgrade(cfg, "head")

        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        from sqlalchemy import event

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async def _mock_get_db():
            return self.factory()

        self.db_patcher1 = patch("cptr.services.workspace_groups.get_db", new=_mock_get_db)
        self.db_patcher2 = patch("cptr.models.workspaces.get_db", new=_mock_get_db)
        self.db_patcher1.start()
        self.db_patcher2.start()

        # Seed test users
        async with self.factory() as db:
            user1 = User(id="user-1", role="user", settings={}, created_at=1000)
            user2 = User(id="user-2", role="user", settings={}, created_at=1000)
            db.add_all([user1, user2])
            await db.commit()

    async def asyncTearDown(self) -> None:
        self.db_patcher2.stop()
        self.db_patcher1.stop()
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def _create_workspace(self, user_id: str, path: str, name: str) -> Workspace:
        ws = await Workspace.upsert(
            user_id=user_id,
            path=path,
            name=name,
            data={},
        )
        return ws

    async def test_migration_upgrade_and_downgrade(self) -> None:
        """Test migration 0038 applies schema and downgrades cleanly."""
        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            with engine.connect() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                }
                self.assertIn("workspace_groups", tables)
                self.assertIn("workspace_group_members", tables)
        finally:
            engine.dispose()

        cfg = self._alembic_config(self.db_path)
        command.downgrade(cfg, "0037")
        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            with engine.connect() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                }
                self.assertNotIn("workspace_groups", tables)
                self.assertNotIn("workspace_group_members", tables)
        finally:
            engine.dispose()

    async def test_create_group_auto_slug_and_custom_slug(self) -> None:
        """Test group creation with auto slug allocation and explicit slug."""
        g1 = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="Cross Repo Suite",
            description="Coordinates backend and frontend",
            group_type="cross-repo",
            config={"shared_env": "local"},
        )
        self.assertEqual(g1["name"], "Cross Repo Suite")
        self.assertEqual(g1["slug"], "cross-repo-suite")
        self.assertEqual(g1["group_type"], "cross-repo")
        self.assertEqual(g1["config"], {"shared_env": "local"})
        self.assertEqual(g1["member_count"], 0)

        # Same user creating another group with same name gets disambiguated slug
        g2 = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="Cross Repo Suite",
        )
        self.assertTrue(g2["slug"].startswith("cross-repo-suite-"))

        # Explicit custom slug
        g3 = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="My Custom Group",
            slug="my-custom-group",
        )
        self.assertEqual(g3["slug"], "my-custom-group")

        # Conflict on duplicate custom slug for same user
        with self.assertRaises(WorkspaceGroupSlugConflictError):
            await WorkspaceGroupService.create_group(
                user_id="user-1",
                name="Duplicate Slug Attempt",
                slug="my-custom-group",
            )

        # Different user can reuse the same slug (owner-scoped uniqueness)
        g_u2 = await WorkspaceGroupService.create_group(
            user_id="user-2",
            name="User 2 Custom Group",
            slug="my-custom-group",
        )
        self.assertEqual(g_u2["slug"], "my-custom-group")

    async def test_deterministic_membership_ordering_and_reordering(self) -> None:
        """Test deterministic membership ordering and reordering."""
        ws_backend = await self._create_workspace("user-1", "/code/backend", "Backend API")
        ws_frontend = await self._create_workspace("user-1", "/code/frontend", "Frontend Web")
        ws_plugin = await self._create_workspace("user-1", "/code/plugin", "Plugin Core")

        # Create group with initial ordered members
        group = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="Full Stack Ecosystem",
            members=[
                {
                    "workspace_id": ws_backend.id,
                    "role": "primary",
                    "primary": True,
                    "alias": "backend",
                },
                {"workspace_id": ws_frontend.id, "role": "member", "alias": "frontend"},
            ],
        )
        self.assertEqual(group["member_count"], 2)
        self.assertEqual(group["members"][0]["workspace_id"], ws_backend.id)
        self.assertEqual(group["members"][0]["sort_order"], 0)
        self.assertTrue(group["members"][0]["primary"])
        self.assertEqual(group["members"][0]["alias"], "backend")

        self.assertEqual(group["members"][1]["workspace_id"], ws_frontend.id)
        self.assertEqual(group["members"][1]["sort_order"], 1)
        self.assertFalse(group["members"][1]["primary"])

        # Add third member without sort_order -> gets sort_order = 2
        updated = await WorkspaceGroupService.add_member(
            user_id="user-1",
            group_ref=group["id"],
            workspace_ref=ws_plugin.id,
            role="companion",
            alias="plugin",
        )
        self.assertEqual(updated["member_count"], 3)
        self.assertEqual(
            [m["workspace_id"] for m in updated["members"]],
            [ws_backend.id, ws_frontend.id, ws_plugin.id],
        )
        self.assertEqual([m["sort_order"] for m in updated["members"]], [0, 1, 2])

        # Reorder members: plugin first, then frontend, then backend
        reordered = await WorkspaceGroupService.reorder_members(
            user_id="user-1",
            group_ref=group["slug"],
            ordered_workspace_refs=[ws_plugin.id, ws_frontend.id, ws_backend.id],
        )
        self.assertEqual(
            [m["workspace_id"] for m in reordered["members"]],
            [ws_plugin.id, ws_frontend.id, ws_backend.id],
        )
        self.assertEqual([m["sort_order"] for m in reordered["members"]], [0, 1, 2])

        # Fetching group verifies deterministic order is persisted
        fetched = await WorkspaceGroupService.get_group(user_id="user-1", group_ref=group["id"])
        self.assertEqual(
            [m["workspace_id"] for m in fetched["members"]],
            [ws_plugin.id, ws_frontend.id, ws_backend.id],
        )

    async def test_duplicate_membership_protection(self) -> None:
        """Test that duplicate memberships are rejected in initial creation and on add_member."""
        ws_a = await self._create_workspace("user-1", "/code/repo-a", "Repo A")

        # Duplicate in initial member list
        with self.assertRaises(WorkspaceGroupDuplicateMemberError):
            await WorkspaceGroupService.create_group(
                user_id="user-1",
                name="Duplicate Initial Group",
                members=[
                    {"workspace_id": ws_a.id},
                    {"workspace_id": ws_a.id},
                ],
            )

        # Create group with one member
        group = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="Single Member Group",
            members=[{"workspace_id": ws_a.id}],
        )

        # Adding same member again raises duplicate error
        with self.assertRaises(WorkspaceGroupDuplicateMemberError):
            await WorkspaceGroupService.add_member(
                user_id="user-1",
                group_ref=group["id"],
                workspace_ref=ws_a.id,
            )

        # Database constraint test: directly inserting duplicate raises error
        async with self.factory() as db:
            dup_member = WorkspaceGroupMember(
                id="manual-dup",
                group_id=group["id"],
                workspace_id=ws_a.id,
                sort_order=99,
                role="member",
                primary=False,
                enabled=True,
                config={},
                created_at=1000,
                updated_at=1000,
            )
            db.add(dup_member)
            with self.assertRaises(Exception):
                await db.commit()

    async def test_owner_scoping_and_isolation(self) -> None:
        """Test owner-scoped isolation: users cannot see, modify, or add other users' workspaces/groups."""
        ws1 = await self._create_workspace("user-1", "/code/user1/repo", "User 1 Repo")
        ws2 = await self._create_workspace("user-2", "/code/user2/repo", "User 2 Repo")

        g1 = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="User 1 Group",
            members=[{"workspace_id": ws1.id}],
        )

        # User 2 cannot get User 1's group
        with self.assertRaises(WorkspaceGroupNotFoundError):
            await WorkspaceGroupService.get_group(user_id="user-2", group_ref=g1["id"])

        # User 2 cannot update User 1's group
        with self.assertRaises(WorkspaceGroupNotFoundError):
            await WorkspaceGroupService.update_group(
                user_id="user-2", group_ref=g1["id"], name="Hacked Name"
            )

        # User 2 cannot delete User 1's group
        with self.assertRaises(WorkspaceGroupNotFoundError):
            await WorkspaceGroupService.delete_group(user_id="user-2", group_ref=g1["id"])

        # User 1 cannot add User 2's workspace into User 1's group
        with self.assertRaises(WorkspaceGroupWorkspaceNotFoundError):
            await WorkspaceGroupService.add_member(
                user_id="user-1",
                group_ref=g1["id"],
                workspace_ref=ws2.id,
            )

        # User 1 listing groups only sees user 1 groups
        g2 = await WorkspaceGroupService.create_group(
            user_id="user-2",
            name="User 2 Group",
            members=[{"workspace_id": ws2.id}],
        )
        u1_groups = await WorkspaceGroupService.list_groups(user_id="user-1")
        self.assertEqual(len(u1_groups), 1)
        self.assertEqual(u1_groups[0]["id"], g1["id"])

        u2_groups = await WorkspaceGroupService.list_groups(user_id="user-2")
        self.assertEqual(len(u2_groups), 1)
        self.assertEqual(u2_groups[0]["id"], g2["id"])

    async def test_primary_exclusivity_and_member_updates(self) -> None:
        """Test primary workspace flag exclusivity and member attribute updates."""
        ws_a = await self._create_workspace("user-1", "/code/repo-a", "Repo A")
        ws_b = await self._create_workspace("user-1", "/code/repo-b", "Repo B")

        group = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="Primary Test Group",
            members=[
                {"workspace_id": ws_a.id, "primary": True},
                {"workspace_id": ws_b.id, "primary": False},
            ],
        )
        self.assertTrue(group["members"][0]["primary"])
        self.assertFalse(group["members"][1]["primary"])

        # Updating member B to primary=True automatically sets member A to primary=False
        updated = await WorkspaceGroupService.update_member(
            user_id="user-1",
            group_ref=group["id"],
            workspace_ref=ws_b.id,
            primary=True,
            alias="main-repo",
            role="lead",
        )
        members_by_id = {m["workspace_id"]: m for m in updated["members"]}
        self.assertFalse(members_by_id[ws_a.id]["primary"])
        self.assertTrue(members_by_id[ws_b.id]["primary"])
        self.assertEqual(members_by_id[ws_b.id]["alias"], "main-repo")
        self.assertEqual(members_by_id[ws_b.id]["role"], "lead")

    async def test_cascade_deletion(self) -> None:
        """Test that deleting a group cascade-removes member records."""
        ws_a = await self._create_workspace("user-1", "/code/repo-a", "Repo A")
        group = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="To Be Deleted",
            members=[{"workspace_id": ws_a.id}],
        )
        group_id = group["id"]

        del_res = await WorkspaceGroupService.delete_group(user_id="user-1", group_ref=group_id)
        self.assertTrue(del_res["deleted"])

        async with self.factory() as db:
            remaining_group = await db.get(WorkspaceGroup, group_id)
            self.assertIsNone(remaining_group)

            remaining_members = list(
                (
                    await db.scalars(
                        select(WorkspaceGroupMember).where(
                            WorkspaceGroupMember.group_id == group_id
                        )
                    )
                ).all()
            )
            self.assertEqual(len(remaining_members), 0)

            # The underlying workspace itself must NOT be deleted!
            ws = await db.get(Workspace, ws_a.id)
            self.assertIsNotNone(ws)

    async def test_validation_errors(self) -> None:
        """Verify validation errors for blank names, duplicate primaries, and invalid reorders."""
        # Blank name
        with self.assertRaises(WorkspaceGroupValidationError):
            await WorkspaceGroupService.create_group(user_id="user-1", name="   ")

        # Blank group ref
        with self.assertRaises(WorkspaceGroupValidationError):
            await WorkspaceGroupService.get_group(user_id="user-1", group_ref="")

        # Multiple primaries in initial members
        ws_a = await self._create_workspace("user-1", "/code/val-a", "Val A")
        ws_b = await self._create_workspace("user-1", "/code/val-b", "Val B")
        with self.assertRaises(WorkspaceGroupValidationError):
            await WorkspaceGroupService.create_group(
                user_id="user-1",
                name="Multi Primary Error",
                members=[
                    {"workspace_id": ws_a.id, "primary": True},
                    {"workspace_id": ws_b.id, "primary": True},
                ],
            )

        group = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="Valid Group",
            members=[{"workspace_id": ws_a.id}],
        )

        # Empty reorder list
        with self.assertRaises(WorkspaceGroupValidationError):
            await WorkspaceGroupService.reorder_members(
                user_id="user-1", group_ref=group["id"], ordered_workspace_refs=[]
            )

        # Reorder with workspace not in group
        with self.assertRaises(WorkspaceGroupValidationError):
            await WorkspaceGroupService.reorder_members(
                user_id="user-1", group_ref=group["id"], ordered_workspace_refs=[ws_b.id]
            )

    async def test_group_identity_separated_from_task_aggregation(self) -> None:
        """Verify group identity is purely structural and decoupled from task execution state."""
        ws = await self._create_workspace("user-1", "/code/isolated", "Isolated Workspace")
        group = await WorkspaceGroupService.create_group(
            user_id="user-1",
            name="Structural Group Only",
            members=[{"workspace_id": ws.id}],
        )
        self.assertIn("id", group)
        self.assertIn("name", group)
        self.assertIn("slug", group)
        self.assertIn("group_type", group)
        self.assertIn("members", group)

        for forbidden in (
            "task_id",
            "tasks",
            "task_batch",
            "execution_status",
            "lease_id",
            "active_command",
        ):
            self.assertNotIn(forbidden, group)
            self.assertNotIn(forbidden, group["members"][0])


if __name__ == "__main__":
    unittest.main()
