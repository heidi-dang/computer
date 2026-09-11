import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import (
    Base,
    User,
    Workspace,
    WorkspaceAlias,
    WorkbenchSession,
)
from cptr.services.workbench_sessions import WorkbenchSessionStore
from cptr.routers.workbench import (
    CreateWorkbenchSessionRequest,
    BindWorkbenchSessionTargetRequest,
    AppendWorkbenchSessionEventRequest,
    create_workbench_session,
    bind_workbench_session,
    append_workbench_session_event,
)
from cptr.routers.control import _ensure_workbench_routing
from cptr.routers.coding import _validate_workbench_routing
from fastapi import HTTPException


class WorkbenchWorkspaceOsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test_wb_wsos.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.session_factory() as db:
            self.user = User(id="user_wsos", display_name="Test User", role="user", created_at=1)
            self.ws1 = Workspace(
                id="ws_canonical_1",
                user_id="user_wsos",
                name="Project Alpha",
                slug="project-alpha",
                path="/home/user/alpha",
                created_at=1,
            )
            self.ws2 = Workspace(
                id="ws_canonical_2",
                user_id="user_wsos",
                name="Project Beta",
                slug="project-beta",
                path="/home/user/beta",
                created_at=2,
            )
            self.alias1 = WorkspaceAlias(
                id="alias_1",
                workspace_id="ws_canonical_1",
                user_id="user_wsos",
                alias="alpha-alias",
                created_at=1,
            )
            db.add_all([self.user, self.ws1, self.ws2, self.alias1])
            await db.commit()

        self.store = WorkbenchSessionStore()
        self.patchers = [
            patch("cptr.utils.db.get_db", side_effect=self._get_db),
            patch("cptr.models.workspaces.get_db", side_effect=self._get_db),
            patch("cptr.services.workspace_refs.get_db", side_effect=self._get_db),
            patch("cptr.services.workbench_sessions.get_db", side_effect=self._get_db),
            patch("cptr.routers.workbench.get_db", side_effect=self._get_db),
            patch("cptr.routers.control.get_db", side_effect=self._get_db),
            patch("cptr.routers.coding.get_db", side_effect=self._get_db),
        ]
        for p in self.patchers:
            p.start()

    async def _get_db(self):
        return self.session_factory()

    async def asyncTearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_create_session_with_workspace_os_binding(self):
        session = await self.store.create(
            owner_id="user_wsos",
            name="Alpha Workbench",
            workspace_id="ws_canonical_1",
            environment_profile_id="envprof_prod",
            environment_profile_override={"CUDA_VISIBLE_DEVICES": "0"},
            admin_role="workspace_admin",
            role_context={"elevated": True, "scopes": ["root:run"]},
            last_context_snapshot_id="wcs_snapshot_123",
        )
        self.assertEqual(session["workspace_id"], "ws_canonical_1")
        self.assertEqual(session["environment_profile_id"], "envprof_prod")
        self.assertEqual(session["environment_profile_override"], {"CUDA_VISIBLE_DEVICES": "0"})
        self.assertEqual(session["admin_role"], "workspace_admin")
        self.assertEqual(session["role_context"], {"elevated": True, "scopes": ["root:run"]})
        self.assertEqual(session["last_context_snapshot_id"], "wcs_snapshot_123")
        self.assertIsNone(session["active_workspace_id"])
        self.assertIsNone(session["active_target_type"])

    async def test_sticky_workspace_id_preserved_across_target_lifecycle(self):
        # 1. Create with durable workspace_id
        session = await self.store.create(
            owner_id="user_wsos",
            name="Lifecycle Session",
            workspace_id="ws_canonical_1",
            last_context_snapshot_id="wcs_initial",
        )
        s_id = session["session_id"]
        self.assertEqual(session["workspace_id"], "ws_canonical_1")
        self.assertIsNone(session["active_workspace_id"])

        # 2. Bind a target with a different transient active workspace
        bound = await self.store.bind_target(
            owner_id="user_wsos",
            session_id=s_id,
            target_type="command",
            target_id="cmd_999",
            workspace_id="ws_canonical_2",
            last_context_snapshot_id="wcs_cmd_run",
        )
        self.assertEqual(bound["status"], "RUNNING")
        self.assertEqual(bound["workspace_id"], "ws_canonical_1")  # Sticky durable association
        self.assertEqual(
            bound["active_workspace_id"], "ws_canonical_2"
        )  # Transient target projection
        self.assertEqual(bound["active_target_type"], "command")
        self.assertEqual(bound["active_target_id"], "cmd_999")
        self.assertEqual(bound["last_context_snapshot_id"], "wcs_cmd_run")

        # 3. Terminal reconciliation of command clears active_workspace_id, leaves sticky workspace_id
        reconciled = await self.store.reconcile_command_terminal(
            owner_id="user_wsos",
            workspace_id="ws_canonical_2",
            command_id="cmd_999",
            status="COMPLETE",
            exit_code=0,
        )
        self.assertEqual(reconciled, 1)

        refreshed = await self.store.get(owner_id="user_wsos", session_id=s_id)
        self.assertEqual(refreshed["status"], "OPEN")
        self.assertIsNone(refreshed["active_workspace_id"])
        self.assertIsNone(refreshed["active_target_type"])
        self.assertIsNone(refreshed["active_target_id"])
        self.assertEqual(refreshed["workspace_id"], "ws_canonical_1")  # Still sticky!
        self.assertEqual(refreshed["last_context_snapshot_id"], "wcs_cmd_run")

    async def test_target_binding_without_explicit_workspace_inherits_default_routing(self):
        session = await self.store.create(
            owner_id="user_wsos",
            name="Inherited Routing Session",
            workspace_id="ws_canonical_1",
        )
        s_id = session["session_id"]

        # Target bound without workspace_id inherits session.workspace_id
        bound = await self.store.bind_target(
            owner_id="user_wsos",
            session_id=s_id,
            target_type="task",
            target_id="task_abc",
            workspace_id=None,
        )
        self.assertEqual(bound["active_workspace_id"], "ws_canonical_1")
        self.assertEqual(bound["workspace_id"], "ws_canonical_1")

    async def test_binding_initial_workspace_becomes_sticky(self):
        # Session created without workspace_id
        session = await self.store.create(
            owner_id="user_wsos",
            name="Unbound Session",
            workspace_id=None,
        )
        s_id = session["session_id"]
        self.assertIsNone(session["workspace_id"])

        # Target bound with workspace_id becomes sticky workspace_id
        bound = await self.store.bind_target(
            owner_id="user_wsos",
            session_id=s_id,
            target_type="command",
            target_id="cmd_first",
            workspace_id="ws_canonical_1",
        )
        self.assertEqual(bound["workspace_id"], "ws_canonical_1")
        self.assertEqual(bound["active_workspace_id"], "ws_canonical_1")

        # After unbind / completion, workspace_id remains sticky
        await self.store.reconcile_command_terminal(
            owner_id="user_wsos",
            workspace_id="ws_canonical_1",
            command_id="cmd_first",
            status="COMPLETE",
        )
        refreshed = await self.store.get(owner_id="user_wsos", session_id=s_id)
        self.assertEqual(refreshed["workspace_id"], "ws_canonical_1")
        self.assertIsNone(refreshed["active_workspace_id"])

    async def test_update_session_workspace_os_binding(self):
        session = await self.store.create(
            owner_id="user_wsos",
            name="Original Session",
        )
        s_id = session["session_id"]

        updated = await self.store.update(
            owner_id="user_wsos",
            session_id=s_id,
            name="Updated Session",
            workspace_id="ws_canonical_2",
            environment_profile_id="envprof_stage",
            environment_profile_override={"ENV": "staging"},
            admin_role="admin",
            role_context={"role": "admin"},
            last_context_snapshot_id="wcs_snap_update",
        )
        self.assertEqual(updated["name"], "Updated Session")
        self.assertEqual(updated["workspace_id"], "ws_canonical_2")
        self.assertEqual(updated["environment_profile_id"], "envprof_stage")
        self.assertEqual(updated["environment_profile_override"], {"ENV": "staging"})
        self.assertEqual(updated["admin_role"], "admin")
        self.assertEqual(updated["role_context"], {"role": "admin"})
        self.assertEqual(updated["last_context_snapshot_id"], "wcs_snap_update")

    async def test_restart_reconciliation_clears_transient_targets_and_preserves_os_binding(self):
        # Create session with rich OS binding and active command target
        session = await self.store.create(
            owner_id="user_wsos",
            name="Restart Session",
            workspace_id="ws_canonical_1",
            environment_profile_id="envprof_restart",
            environment_profile_override={"MODE": "daemon"},
            admin_role="workspace_admin",
            role_context={"perm": "full"},
            last_context_snapshot_id="wcs_pre_restart",
        )
        s_id = session["session_id"]

        # Bind active command
        await self.store.bind_target(
            owner_id="user_wsos",
            session_id=s_id,
            target_type="command",
            target_id="cmd_in_flight",
            workspace_id="ws_canonical_1",
        )

        # Reconcile restart
        reconciled_count = await self.store.reconcile_restart(now_ms=500_000)
        self.assertEqual(reconciled_count, 1)

        # Inspect session
        refreshed = await self.store.get(owner_id="user_wsos", session_id=s_id)
        self.assertEqual(refreshed["status"], "OPEN")
        self.assertIsNone(refreshed["active_target_type"])
        self.assertIsNone(refreshed["active_target_id"])
        self.assertIsNone(refreshed["active_workspace_id"])
        # All sticky bindings survive restart untouched:
        self.assertEqual(refreshed["workspace_id"], "ws_canonical_1")
        self.assertEqual(refreshed["environment_profile_id"], "envprof_restart")
        self.assertEqual(refreshed["environment_profile_override"], {"MODE": "daemon"})
        self.assertEqual(refreshed["admin_role"], "workspace_admin")
        self.assertEqual(refreshed["role_context"], {"perm": "full"})
        self.assertEqual(refreshed["last_context_snapshot_id"], "wcs_pre_restart")

        # Check recorded event
        events = await self.store.events(owner_id="user_wsos", session_id=s_id)
        self.assertTrue(any(e["event_type"] == "workbench.restart_reconciled" for e in events))

    async def test_no_mutable_memory_instruction_duplication(self):
        # Verify model columns: last_context_snapshot_id exists, but no mutable memory versions or instructions
        cols = {c.name for c in WorkbenchSession.__table__.columns}
        self.assertIn("last_context_snapshot_id", cols)
        self.assertIn("environment_profile_id", cols)
        self.assertIn("environment_profile_override", cols)
        self.assertIn("admin_role", cols)
        self.assertIn("role_context", cols)
        self.assertNotIn("memory_version", cols)
        self.assertNotIn("system_instructions", cols)
        self.assertNotIn("user_instructions", cols)
        self.assertNotIn("instruction_version", cols)

    async def test_router_create_and_bind_with_workspace_refs(self):
        request = SimpleNamespace()
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_wsos")),
            patch("cptr.routers.workbench._ensure_target_owner", new=AsyncMock(return_value=None)),
            patch("cptr.routers.workbench.trace_context_from_request", return_value=None),
        ):
            # Create using slug ref
            res = await create_workbench_session(
                request,
                CreateWorkbenchSessionRequest(
                    name="Slug Session",
                    workspace_ref="project-alpha",
                    environment_profile_ref="envprof_alpha",
                    admin_role_ref="workspace_admin",
                    last_context_snapshot_id="wcs_slug_snap",
                ),
            )
            self.assertEqual(res["workspace_id"], "ws_canonical_1")
            self.assertEqual(res["environment_profile_id"], "envprof_alpha")
            self.assertEqual(res["admin_role"], "workspace_admin")
            self.assertEqual(res["last_context_snapshot_id"], "wcs_slug_snap")
            session_id = res["session_id"]

            # Bind target using alias ref
            bound = await bind_workbench_session(
                request,
                session_id,
                BindWorkbenchSessionTargetRequest(
                    target_type="task",
                    target_id="task_slug",
                    workspace_ref="alpha-alias",
                    last_context_snapshot_id="wcs_bound_snap",
                ),
            )
            self.assertEqual(bound["active_workspace_id"], "ws_canonical_1")
            self.assertEqual(bound["last_context_snapshot_id"], "wcs_bound_snap")

            # Append event inheriting default routing
            event = await append_workbench_session_event(
                request,
                session_id,
                AppendWorkbenchSessionEventRequest(
                    summary="Inherited event",
                    event_type="test.event",
                ),
            )
            self.assertEqual(event["workspace_id"], "ws_canonical_1")

    async def test_inherited_routing_and_workspace_equivalence(self):
        # Test _ensure_workbench_routing and _validate_workbench_routing
        session = await self.store.create(
            owner_id="user_wsos",
            name="Route Session",
            workspace_id="ws_canonical_1",
        )
        s_id = session["session_id"]

        # Direct UUID match
        routed = await _ensure_workbench_routing(
            user_id="user_wsos",
            workspace_id="ws_canonical_1",
            session_id=s_id,
        )
        self.assertEqual(routed["session_id"], s_id)

        # Slug match (resolves to same canonical workspace)
        routed_slug = await _ensure_workbench_routing(
            user_id="user_wsos",
            workspace_id="project-alpha",
            session_id=s_id,
        )
        self.assertEqual(routed_slug["session_id"], s_id)

        # Path match (resolves to same canonical workspace)
        routed_path = await _validate_workbench_routing(
            user_id="user_wsos",
            workspace_id="/home/user/alpha",
            session_id=s_id,
        )
        self.assertEqual(routed_path["session_id"], s_id)

        # Mismatch should raise 404
        with self.assertRaises(HTTPException) as cm:
            await _ensure_workbench_routing(
                user_id="user_wsos",
                workspace_id="ws_canonical_2",
                session_id=s_id,
            )
        self.assertEqual(cm.exception.status_code, 404)

    async def test_restart_reconciliation_multi_session_isolation(self):
        # Create 3 sessions:
        # Session 1: active command on ws1
        # Session 2: active command on ws2
        # Session 3: open session with no active command
        s1 = await self.store.create(
            owner_id="user_wsos",
            name="Session 1",
            workspace_id="ws_canonical_1",
            environment_profile_id="env_1",
            admin_role="role_1",
            last_context_snapshot_id="snap_1",
        )
        s2 = await self.store.create(
            owner_id="user_wsos",
            name="Session 2",
            workspace_id="ws_canonical_2",
            environment_profile_id="env_2",
            admin_role="role_2",
            last_context_snapshot_id="snap_2",
        )
        s3 = await self.store.create(
            owner_id="user_wsos",
            name="Session 3",
            workspace_id="ws_canonical_1",
            environment_profile_id="env_3",
            admin_role="role_3",
            last_context_snapshot_id="snap_3",
        )

        await self.store.bind_target(
            owner_id="user_wsos",
            session_id=s1["session_id"],
            target_type="command",
            target_id="cmd_s1",
            workspace_id="ws_canonical_1",
        )
        await self.store.bind_target(
            owner_id="user_wsos",
            session_id=s2["session_id"],
            target_type="command",
            target_id="cmd_s2",
            workspace_id="ws_canonical_2",
        )

        reconciled = await self.store.reconcile_restart(now_ms=600_000)
        self.assertEqual(reconciled, 2)

        # Session 1 reconciled
        r1 = await self.store.get(owner_id="user_wsos", session_id=s1["session_id"])
        self.assertEqual(r1["status"], "OPEN")
        self.assertIsNone(r1["active_target_id"])
        self.assertIsNone(r1["active_workspace_id"])
        self.assertEqual(r1["workspace_id"], "ws_canonical_1")
        self.assertEqual(r1["environment_profile_id"], "env_1")
        self.assertEqual(r1["admin_role"], "role_1")
        self.assertEqual(r1["last_context_snapshot_id"], "snap_1")

        # Session 2 reconciled
        r2 = await self.store.get(owner_id="user_wsos", session_id=s2["session_id"])
        self.assertEqual(r2["status"], "OPEN")
        self.assertIsNone(r2["active_target_id"])
        self.assertIsNone(r2["active_workspace_id"])
        self.assertEqual(r2["workspace_id"], "ws_canonical_2")
        self.assertEqual(r2["environment_profile_id"], "env_2")
        self.assertEqual(r2["admin_role"], "role_2")
        self.assertEqual(r2["last_context_snapshot_id"], "snap_2")

        # Session 3 untouched
        r3 = await self.store.get(owner_id="user_wsos", session_id=s3["session_id"])
        self.assertEqual(r3["status"], "OPEN")
        self.assertIsNone(r3["active_target_id"])
        self.assertEqual(r3["workspace_id"], "ws_canonical_1")
        self.assertEqual(r3["environment_profile_id"], "env_3")
        self.assertEqual(r3["admin_role"], "role_3")
        self.assertEqual(r3["last_context_snapshot_id"], "snap_3")

    async def test_restart_recovery_preserves_bindings_across_subsequent_rebinding(self):
        # Create session with sticky bindings and active command
        session = await self.store.create(
            owner_id="user_wsos",
            name="Rebind After Restart",
            workspace_id="ws_canonical_1",
            environment_profile_id="envprof_sticky",
            environment_profile_override={"KEY": "val"},
            admin_role="admin_sticky",
            role_context={"ctx": "sticky"},
            last_context_snapshot_id="snap_initial",
        )
        s_id = session["session_id"]
        await self.store.bind_target(
            owner_id="user_wsos",
            session_id=s_id,
            target_type="command",
            target_id="cmd_crashed",
            workspace_id="ws_canonical_1",
        )

        # Restart occurs
        await self.store.reconcile_restart(now_ms=700_000)

        # Subsequent re-binding after restart:
        # User starts a new command without passing workspace_id -> inherits sticky workspace_id
        bound = await self.store.bind_target(
            owner_id="user_wsos",
            session_id=s_id,
            target_type="command",
            target_id="cmd_recovered",
            workspace_id=None,
            last_context_snapshot_id="snap_updated",
        )
        self.assertEqual(bound["status"], "RUNNING")
        self.assertEqual(bound["workspace_id"], "ws_canonical_1")
        self.assertEqual(bound["active_workspace_id"], "ws_canonical_1")
        self.assertEqual(bound["last_context_snapshot_id"], "snap_updated")
        self.assertEqual(bound["environment_profile_id"], "envprof_sticky")
        self.assertEqual(bound["environment_profile_override"], {"KEY": "val"})
        self.assertEqual(bound["admin_role"], "admin_sticky")
        self.assertEqual(bound["role_context"], {"ctx": "sticky"})

        # New command finishes
        await self.store.reconcile_command_terminal(
            owner_id="user_wsos",
            workspace_id="ws_canonical_1",
            command_id="cmd_recovered",
            status="COMPLETE",
            exit_code=0,
        )
        refreshed = await self.store.get(owner_id="user_wsos", session_id=s_id)
        self.assertEqual(refreshed["status"], "OPEN")
        self.assertIsNone(refreshed["active_workspace_id"])
        self.assertIsNone(refreshed["active_target_id"])
        # All sticky bindings still intact:
        self.assertEqual(refreshed["workspace_id"], "ws_canonical_1")
        self.assertEqual(refreshed["environment_profile_id"], "envprof_sticky")
        self.assertEqual(refreshed["last_context_snapshot_id"], "snap_updated")


if __name__ == "__main__":
    unittest.main()
