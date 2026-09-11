"""Focused tests for stable workspace UUID memory namespace compatibility,

legacy aliases, and compact-summary/checkpoint extensions.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import cptr.routers.control as control
from cptr.memory.domain import (
    CheckpointState,
    ConsolidationInput,
    ManagedContext,
    MemoryQuery,
)
from cptr.memory.mcp_adapter import MemoryMcpAdapter
from cptr.memory.service import EmbeddedMemoryService
from cptr.memory.store import SqlMemoryStore
from cptr.memory.workspace import (
    WorkspaceNamespace,
    clear_workspace_namespace_cache,
    resolve_workspace_namespace,
)
from cptr.models import Base, User, Workspace
from cptr.services.memory_fabric import MemoryFabricStore


class WorkspaceMemoryNamespaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_workspace_namespace_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(User(id="user-1", role="admin", settings={}, created_at=1))
            db.add(
                Workspace(
                    id="ws-uuid-1234",
                    user_id="user-1",
                    path="/home/user/projects/cptr-app",
                    name="CPTR App",
                    data={},
                    created_at=1,
                    updated_at=1,
                )
            )
            await db.commit()

        self.store = SqlMemoryStore(session_factory=self.sessions)
        self.events = MemoryFabricStore(session_factory=self.sessions)
        self.settings = {
            "enabled": True,
            "required_for_execution": True,
            "context_char_limit": 9000,
            "canonical_char_limit": 3000,
            "verification_ttl_seconds": 86400,
        }

    async def asyncTearDown(self):
        clear_workspace_namespace_cache()
        await self.engine.dispose()

    def service(self) -> EmbeddedMemoryService:
        return EmbeddedMemoryService(
            store=self.store,
            event_store=self.events,
            managed_context_loader=AsyncMock(return_value=ManagedContext(rendered="", items=[])),
            settings_loader=AsyncMock(return_value=self.settings),
        )

    async def test_workspace_namespace_resolution_by_uuid_and_path(self):
        # Empty workspace resolves to user scope
        user_ns = await resolve_workspace_namespace("user-1", "", session_factory=self.sessions)
        self.assertTrue(user_ns.is_user_scope)
        self.assertEqual(user_ns.workspace_id, "")
        self.assertTrue(user_ns.matches(""))
        self.assertFalse(user_ns.matches("ws-uuid-1234"))

        # Resolution by stable UUID
        by_uuid = await resolve_workspace_namespace(
            "user-1", "ws-uuid-1234", session_factory=self.sessions
        )
        self.assertFalse(by_uuid.is_user_scope)
        self.assertEqual(by_uuid.workspace_id, "ws-uuid-1234")
        self.assertEqual(by_uuid.workspace_path, "/home/user/projects/cptr-app")
        self.assertEqual(by_uuid.name, "CPTR App")
        self.assertIn("ws-uuid-1234", by_uuid.aliases)
        self.assertIn("/home/user/projects/cptr-app", by_uuid.aliases)
        self.assertTrue(by_uuid.matches("/home/user/projects/cptr-app"))
        self.assertTrue(by_uuid.matches("ws-uuid-1234"))

        # Resolution by legacy path
        by_path = await resolve_workspace_namespace(
            "user-1", "/home/user/projects/cptr-app", session_factory=self.sessions
        )
        self.assertEqual(by_path.workspace_id, "ws-uuid-1234")
        self.assertEqual(by_path.workspace_path, "/home/user/projects/cptr-app")
        self.assertEqual(by_path.aliases, by_uuid.aliases)

        # Ad-hoc / unknown workspace string resolves gracefully without error
        adhoc = await resolve_workspace_namespace(
            "user-1", "/unknown/repo", session_factory=self.sessions
        )
        self.assertEqual(adhoc.workspace_id, "/unknown/repo")
        self.assertEqual(adhoc.workspace_path, "/unknown/repo")
        self.assertIn("/unknown/repo", adhoc.aliases)

    async def test_memory_search_and_deduplication_across_aliases(self):
        service = self.service()

        # Consolidate a memory referencing legacy workspace path
        ref1 = await service.consolidate(
            ConsolidationInput(
                user_id="user-1",
                workspace="/home/user/projects/cptr-app",
                scope="workspace",
                heading="Deployment",
                kind="procedure",
                text="Always run pytest before merging pull requests.",
                trust_level="verified_system_fact",
                confidence=0.95,
                importance=0.8,
            )
        )

        # Querying by stable workspace UUID finds the memory stored under legacy path
        results_by_uuid = await service.search(
            MemoryQuery(
                user_id="user-1",
                workspace="ws-uuid-1234",
                query="pytest pull requests",
                limit=5,
            )
        )
        self.assertGreaterEqual(len(results_by_uuid), 1)
        self.assertEqual(results_by_uuid[0].memory_id, ref1.memory_id)

        # Inspecting by stable UUID also resolves and matches
        inspected = await service.inspect(ref1.memory_id, user_id="user-1", workspace="ws-uuid-1234")
        self.assertEqual(inspected["memory_id"], ref1.memory_id)

        # Deduplication: consolidating identical text with stable UUID reuses existing record
        ref2 = await service.consolidate(
            ConsolidationInput(
                user_id="user-1",
                workspace="ws-uuid-1234",
                scope="workspace",
                heading="Deployment",
                kind="procedure",
                text="Always run pytest before merging pull requests.",
            )
        )
        self.assertEqual(ref2.memory_id, ref1.memory_id)

    async def test_checkpoints_and_namespace_version_across_aliases(self):
        service = self.service()

        # Save first checkpoint with legacy path
        cp1 = await service.checkpoint(
            CheckpointState(
                user_id="user-1",
                workspace="/home/user/projects/cptr-app",
                task_key="task-build",
                stage="compile",
                state={"phase": 1},
            )
        )
        self.assertEqual(cp1.version, 1)

        # Recover latest checkpoint using stable workspace UUID
        latest = await self.store.latest_checkpoint("user-1", "ws-uuid-1234", task_key="task-build")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["checkpoint_id"], cp1.checkpoint_id)
        self.assertEqual(latest["version"], 1)

        # Save second checkpoint with stable UUID
        cp2 = await service.checkpoint(
            CheckpointState(
                user_id="user-1",
                workspace="ws-uuid-1234",
                task_key="task-build",
                stage="verify",
                state={"phase": 2},
            )
        )
        self.assertEqual(cp2.version, 2)

        # Recover latest checkpoint using legacy path
        latest_by_path = await self.store.latest_checkpoint(
            "user-1", "/home/user/projects/cptr-app", task_key="task-build"
        )
        self.assertIsNotNone(latest_by_path)
        self.assertEqual(latest_by_path["checkpoint_id"], cp2.checkpoint_id)
        self.assertEqual(latest_by_path["version"], 2)

        # Recover latest workspace checkpoint without specifying task_key
        latest_any_task = await self.store.latest_checkpoint("user-1", "ws-uuid-1234")
        self.assertIsNotNone(latest_any_task)
        self.assertEqual(latest_any_task["checkpoint_id"], cp2.checkpoint_id)

        # Namespace version is unified
        v_uuid = await self.store.namespace_version("user-1", "ws-uuid-1234")
        v_path = await self.store.namespace_version("user-1", "/home/user/projects/cptr-app")
        self.assertEqual(v_uuid, v_path)

    async def test_compact_summary_reuses_existing_memory_core(self):
        service = self.service()

        # Add records
        await service.consolidate(
            ConsolidationInput(
                user_id="user-1",
                workspace="ws-uuid-1234",
                scope="workspace",
                heading="Architecture",
                kind="semantic",
                text="PostgreSQL 16 is the primary storage backend.",
                importance=0.9,
            )
        )
        await service.consolidate(
            ConsolidationInput(
                user_id="user-1",
                workspace="/home/user/projects/cptr-app",
                scope="workspace",
                heading="Tests",
                kind="procedure",
                text="Run pytest with PYTHONPATH=. for all test suites.",
                importance=0.85,
            )
        )
        await service.checkpoint(
            CheckpointState(
                user_id="user-1",
                workspace="ws-uuid-1234",
                task_key="deploy-task",
                stage="tested",
                state={"coverage": 95},
            )
        )

        # Fetch compact summary via UUID
        summary = await service.compact_summary("user-1", "ws-uuid-1234", limit=5)
        self.assertEqual(summary["workspace_id"], "ws-uuid-1234")
        self.assertEqual(summary["workspace_path"], "/home/user/projects/cptr-app")
        self.assertIn("ws-uuid-1234", summary["aliases"])
        self.assertIn("/home/user/projects/cptr-app", summary["aliases"])
        self.assertEqual(summary["total_records"], 2)
        self.assertEqual(summary["counts_by_kind"]["semantic"], 1)
        self.assertEqual(summary["counts_by_kind"]["procedure"], 1)
        self.assertEqual(len(summary["records"]), 2)
        self.assertIsNotNone(summary["latest_checkpoint"])
        self.assertEqual(summary["latest_checkpoint"]["stage"], "tested")
        self.assertEqual(summary["health"]["enabled"], True)

    async def test_mcp_adapter_compact_summary_and_checkpoint_tools(self):
        service = self.service()
        adapter_ro = MemoryMcpAdapter(
            service=service,
            user_id="user-1",
            workspace="ws-uuid-1234",
            allow_mutations=False,
        )

        # Save checkpoint via store directly
        await self.store.save_checkpoint(
            user_id="user-1",
            workspace="ws-uuid-1234",
            task_key="mcp-task",
            stage="initial",
            state={"ok": True},
            memory_version=1,
        )

        # Read-only checkpoint query
        cp_info = await adapter_ro.call_tool("memory.checkpoint", {"task_key": "mcp-task"})
        self.assertEqual(cp_info["stage"], "initial")

        # Read-only compact summary
        summary = await adapter_ro.call_tool("memory.compact_summary", {"limit": 5})
        self.assertEqual(summary["workspace_id"], "ws-uuid-1234")
        self.assertEqual(summary["latest_checkpoint"]["stage"], "initial")

        # Writing checkpoint with allow_mutations=False raises PermissionError
        with self.assertRaises(PermissionError):
            await adapter_ro.call_tool(
                "memory.checkpoint",
                {"task_key": "mcp-task", "stage": "next", "state": {}},
            )

        # Mutation-enabled adapter can save checkpoint
        adapter_rw = MemoryMcpAdapter(
            service=service,
            user_id="user-1",
            workspace="ws-uuid-1234",
            allow_mutations=True,
        )
        saved = await adapter_rw.call_tool(
            "memory.checkpoint",
            {"task_key": "mcp-task", "stage": "next", "state": {"ok": 2}},
        )
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["stage"], "next")

    async def test_control_router_compact_summary_and_checkpoint_actions(self):
        endpoint = getattr(control, "read_memory", None)
        request_model = getattr(control, "MemoryReadRequest", None)
        self.assertTrue(callable(endpoint))

        # Checkpoint action
        body_cp = request_model(
            action="checkpoint",
            workspace_id="ws-uuid-1234",
            task_key="task-gate",
        )
        adapter = SimpleNamespace(
            call_tool=AsyncMock(return_value={"checkpoint_id": "cp-1", "stage": "verified"}),
        )
        workspace = SimpleNamespace(path="/home/user/projects/cptr-app")

        request = SimpleNamespace(state=SimpleNamespace())
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")) as require_user,
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.control.MemoryMcpAdapter", return_value=adapter),
        ):
            res_cp = await endpoint(request, body_cp)

        require_user.assert_awaited_once_with(request, "memory:read")
        adapter.call_tool.assert_awaited_once_with("memory.checkpoint", {"task_key": "task-gate"})
        self.assertEqual(res_cp["action"], "checkpoint")
        self.assertEqual(res_cp["result"]["stage"], "verified")

        # Compact summary action
        body_sum = request_model(
            action="compact_summary",
            workspace_id="ws-uuid-1234",
            limit=10,
        )
        adapter.call_tool = AsyncMock(
            return_value={"workspace_id": "ws-uuid-1234", "total_records": 5}
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")) as require_user,
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.control.MemoryMcpAdapter", return_value=adapter),
        ):
            res_sum = await endpoint(request, body_sum)

        require_user.assert_awaited_once_with(request, "memory:read")
        adapter.call_tool.assert_awaited_once_with("memory.compact_summary", {"limit": 10})
        self.assertEqual(res_sum["action"], "compact_summary")
        self.assertEqual(res_sum["result"]["total_records"], 5)


if __name__ == "__main__":
    unittest.main()
