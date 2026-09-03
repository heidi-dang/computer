import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.memory.graph import MemoryGraphStore
from cptr.memory.jobs import MemoryJobStore
from cptr.memory.store import SqlMemoryStore
from cptr.models import Base, User, Workspace
from cptr.routers import mcp as mcp_router
from cptr.services.memory_fabric import MemoryFabricStore
from cptr.services.memory_observability import MemoryObservabilityService, build_memory_inventory
from cptr.utils import memory as managed_memory


class MemoryFabricStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(User(id="user-1", role="admin", settings={}, created_at=1))
            db.add(User(id="user-2", role="user", settings={}, created_at=1))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_event_journal_is_durable_owner_scoped_and_payload_bounded(self):
        store = MemoryFabricStore(session_factory=self.sessions)
        first = await store.record_event(
            user_id="user-1",
            workspace="/repo",
            event_type="recall",
            scope="workspace",
            path="facts.md",
            heading="Deploy",
            reason="query matched text",
            payload={"items": [{"path": "facts.md", "heading": "Deploy"}]},
            created_at_ms=1000,
        )
        await store.record_event(
            user_id="user-2",
            workspace="/other",
            event_type="write",
            payload={"operation_count": 1},
            created_at_ms=2000,
        )
        events = await store.list_events("user-1", limit=20)
        self.assertEqual([event["event_id"] for event in events], [first.id])
        self.assertEqual(events[0]["event_type"], "recall")
        self.assertEqual(events[0]["trust_level"], "managed_memory")
        self.assertEqual(events[0]["confidence"], 1.0)
        self.assertEqual(events[0]["payload"]["items"][0]["heading"], "Deploy")


class MemoryInventoryTests(unittest.TestCase):
    def test_inventory_builds_scope_nodes_sections_links_and_baseline_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            with patch.object(managed_memory, "DATA_DIR", data_dir):
                user_root = managed_memory.resolve_memory_roots("user-1")[0]
                user_root.root.mkdir(parents=True, exist_ok=True)
                user_root.baseline_path.write_text(
                    "- Prefer exact verification\n", encoding="utf-8"
                )
                project_root = managed_memory.resolve_memory_roots("user-1", str(workspace))[1]
                project_root.root.mkdir(parents=True, exist_ok=True)
                (project_root.root / "deploy.md").write_text(
                    "## Deploy\n<!-- mem: deploy-procedure -->\nRun tests first. Related: [[Rollback]]\n\n"
                    "## Rollback\nUse the last verified revision.\n",
                    encoding="utf-8",
                )
                inventory = build_memory_inventory(
                    "user-1",
                    [
                        {
                            "workspace_id": "workspace-1",
                            "workspace_name": "Repo",
                            "workspace_path": str(workspace),
                        }
                    ],
                    node_limit=100,
                )
        labels = {node["label"] for node in inventory["nodes"]}
        self.assertIn("Prefer exact verification", labels)
        self.assertIn("Deploy", labels)
        self.assertIn("Rollback", labels)
        self.assertTrue(any(edge["kind"] == "related" for edge in inventory["edges"]))
        self.assertGreaterEqual(inventory["metrics"]["memory_nodes"], 3)
        self.assertGreater(inventory["metrics"]["total_bytes"], 0)


class ManagedMemoryInstrumentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_write_emits_metadata_without_memory_content(self):
        recorder = AsyncMock()
        operations = [
            {
                "action": "add",
                "path": "facts.md",
                "heading": "Deploy",
                "content": "TOP SECRET MEMORY CONTENT",
            }
        ]
        with (
            patch.object(
                managed_memory, "get_memory_settings", AsyncMock(return_value={"enabled": True})
            ),
            patch.object(
                managed_memory,
                "write_memory",
                AsyncMock(
                    return_value={
                        "success": True,
                        "message": "Applied 1 operation(s).",
                        "path": "/memory/facts.md",
                    }
                ),
            ),
            patch.object(managed_memory, "_record_memory_fabric_event", recorder),
        ):
            result = await managed_memory.remember(
                SimpleNamespace(),
                user_id="user-1",
                workspace="/repo",
                scope="workspace",
                operations=operations,
            )
        self.assertTrue(result["success"])
        recorder.assert_awaited_once()
        event = recorder.await_args.kwargs
        self.assertEqual(event["event_type"], "write")
        self.assertEqual(event["payload"]["operation_count"], 1)
        self.assertEqual(event["payload"]["operations"][0]["action"], "add")
        self.assertEqual(event["payload"]["operations"][0]["path"], "facts.md")
        self.assertNotIn("content", event["payload"]["operations"][0])
        self.assertNotIn("TOP SECRET MEMORY CONTENT", str(event["payload"]))

    async def test_prompt_hydration_emits_recall_provenance_before_model_use(self):
        recorder = AsyncMock()
        settings = {
            "enabled": True,
            "tool_enabled": True,
            "background_review_enabled": True,
            "review_interval_turns": 10,
            "user_char_limit": 2000,
            "workspace_char_limit": 3000,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(managed_memory, "DATA_DIR", Path(tmp) / "data"),
                patch.object(
                    managed_memory, "get_memory_settings", AsyncMock(return_value=settings)
                ),
                patch.object(managed_memory, "_record_memory_fabric_event", recorder),
            ):
                root = managed_memory.resolve_memory_roots("user-1")[0]
                root.root.mkdir(parents=True, exist_ok=True)
                root.baseline_path.write_text("- Prefer exact verification\n", encoding="utf-8")
                rendered = await managed_memory.build_memory_prompt(
                    SimpleNamespace(),
                    "user-1",
                    "",
                    current_message="verify the deployment exactly",
                )
        self.assertIn("Prefer exact verification", rendered)
        recorder.assert_awaited_once()
        event = recorder.await_args.kwargs
        self.assertEqual(event["event_type"], "recall")
        self.assertEqual(event["reason"], "compiled for prompt")
        self.assertGreater(event["payload"]["context_chars"], 0)
        self.assertTrue(
            any(item["reason"] == "baseline memory" for item in event["payload"]["items"])
        )


class MemoryObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(User(id="user-1", role="admin", settings={}, created_at=1))
            db.add(User(id="user-2", role="user", settings={}, created_at=1))
            db.add(
                Workspace(
                    id="workspace-1",
                    user_id="user-1",
                    path="/repo-1",
                    name="Repo One",
                    data={},
                    created_at=1,
                    updated_at=1,
                )
            )
            db.add(
                Workspace(
                    id="workspace-2",
                    user_id="user-2",
                    path="/repo-2",
                    name="Other Repo",
                    data={},
                    created_at=1,
                    updated_at=1,
                )
            )
            await db.commit()
        self.store = MemoryFabricStore(session_factory=self.sessions)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_snapshot_combines_inventory_recall_traces_health_and_owner_scoping(self):
        now_ms = int(time.time() * 1000)
        await self.store.record_event(
            user_id="user-1",
            workspace="/repo-1",
            event_type="recall",
            scope="workspace",
            reason="compiled for prompt",
            payload={
                "items": [
                    {
                        "node_id": "mem-deploy",
                        "scope": "workspace",
                        "path": "deploy.md",
                        "heading": "Deploy",
                        "memory_id": "deploy-procedure",
                        "reason": "query matched text",
                    }
                ],
                "context_chars": 128,
            },
            created_at_ms=now_ms,
        )
        await self.store.record_event(
            user_id="user-2",
            workspace="/repo-2",
            event_type="write",
            payload={"operation_count": 1},
            created_at_ms=3000,
        )

        def inventory_builder(_user_id, workspaces, node_limit=400):
            self.assertEqual([item["workspace_id"] for item in workspaces], ["workspace-1"])
            return {
                "nodes": [
                    {
                        "id": "mem-deploy",
                        "label": "Deploy",
                        "kind": "memory",
                        "scope": "workspace",
                        "workspace_id": "workspace-1",
                        "workspace_name": "Repo One",
                        "path": "deploy.md",
                        "heading": "Deploy",
                        "memory_id": "deploy-procedure",
                        "preview": "Run tests first.",
                        "modified_at_ms": 1000,
                        "size": 16,
                        "trust_level": "managed_memory",
                        "confidence": 1.0,
                    }
                ],
                "edges": [],
                "metrics": {
                    "memory_nodes": 1,
                    "user_memory_nodes": 0,
                    "workspace_memory_nodes": 1,
                    "scope_nodes": 1,
                    "edge_count": 0,
                    "file_count": 1,
                    "total_bytes": 16,
                },
            }

        service = MemoryObservabilityService(
            session_factory=self.sessions,
            store=self.store,
            inventory_builder=inventory_builder,
            settings_loader=AsyncMock(
                return_value={
                    "enabled": True,
                    "tool_enabled": True,
                    "background_review_enabled": True,
                    "review_interval_turns": 10,
                    "user_char_limit": 2000,
                    "workspace_char_limit": 3000,
                }
            ),
        )
        snapshot = await service.snapshot(user_id="user-1")
        self.assertEqual([row["workspace_id"] for row in snapshot["workspaces"]], ["workspace-1"])
        self.assertEqual(len(snapshot["events"]), 1)
        self.assertEqual(snapshot["events"][0]["event_type"], "recall")
        self.assertEqual(snapshot["nodes"][0]["recall_count"], 1)
        self.assertEqual(snapshot["nodes"][0]["last_recalled_at_ms"], now_ms)
        self.assertEqual(snapshot["recall_traces"][0]["items"][0]["heading"], "Deploy")
        self.assertEqual(snapshot["metrics"]["recalls_24h"], 1)
        self.assertTrue(snapshot["health"]["enabled"])
        self.assertEqual(
            snapshot["health"]["canonical_store"],
            "managed_markdown_plus_versioned_memory_records",
        )
        self.assertEqual(len(snapshot["fingerprint"]), 64)

    async def test_snapshot_projects_canonical_temporal_graph_lifecycle_and_queue_state(self):
        core = SqlMemoryStore(session_factory=self.sessions)
        graph = MemoryGraphStore(session_factory=self.sessions)
        jobs = MemoryJobStore(session_factory=self.sessions)
        now_ms = int(time.time() * 1000)
        memory = await core.create_memory(
            user_id="user-1",
            workspace="/repo-1",
            scope="workspace",
            kind="procedure",
            canonical_text="Deploy [[Payments]] from heidi/repo only after verification.",
            trust_level="verified_system_fact",
            confidence_ppm=930_000,
            importance_ppm=880_000,
            verification_expires_at_ms=now_ms - 1,
        )
        await core.save_checkpoint(
            user_id="user-1",
            workspace="/repo-1",
            task_key="task-observatory",
            stage="tool_complete",
            state={"tool": "pytest"},
            memory_version=None,
        )
        saved_snapshot = await core.create_snapshot("user-1", "/repo-1", label="before release")
        await core.create_branch(
            "user-1",
            "/repo-1",
            name="experiment",
            from_snapshot_id=saved_snapshot.snapshot_id,
        )
        await graph.project_memory(
            user_id="user-1",
            workspace="/repo-1",
            memory_id=memory.memory_id,
            heading="Deploy",
            text="Deploy [[Payments]] from heidi/repo only after verification.",
        )
        await jobs.enqueue(
            user_id="user-1",
            workspace="/repo-1",
            job_type="consolidate",
            payload={"scope": "workspace", "text": "queued"},
        )
        service = MemoryObservabilityService(
            session_factory=self.sessions,
            store=self.store,
            core_store=core,
            graph_store=graph,
            job_store=jobs,
            inventory_builder=lambda *_args, **_kwargs: {
                "nodes": [],
                "edges": [],
                "metrics": {
                    "memory_nodes": 0,
                    "user_memory_nodes": 0,
                    "workspace_memory_nodes": 0,
                    "scope_nodes": 0,
                    "edge_count": 0,
                    "file_count": 0,
                    "total_bytes": 0,
                },
            },
            settings_loader=AsyncMock(
                return_value={
                    "enabled": True,
                    "required_for_execution": True,
                    "maintenance_enabled": True,
                }
            ),
        )
        snapshot = await service.snapshot(user_id="user-1", workspace_id="workspace-1")
        self.assertEqual(snapshot["version"], 3)
        canonical = next(node for node in snapshot["nodes"] if node["id"] == memory.memory_id)
        self.assertEqual(canonical["source_layer"], "canonical")
        self.assertEqual(canonical["heading"], "Procedure")
        self.assertTrue(canonical["verification_stale"])
        self.assertEqual(canonical["importance"], 0.88)
        self.assertTrue(any(node["kind"] == "entity" for node in snapshot["nodes"]))
        self.assertTrue(any(edge["kind"] == "related" for edge in snapshot["edges"]))
        self.assertEqual(snapshot["metrics"]["canonical_memory_nodes"], 1)
        self.assertGreaterEqual(snapshot["metrics"]["entity_nodes"], 2)
        self.assertEqual(snapshot["metrics"]["snapshot_count"], 1)
        self.assertEqual(snapshot["metrics"]["branch_count"], 1)
        self.assertEqual(snapshot["metrics"]["checkpoint_count"], 1)
        self.assertGreaterEqual(snapshot["metrics"]["memory_version"], 1)
        self.assertEqual(snapshot["metrics"]["pending_memory_jobs"], 1)
        self.assertEqual(snapshot["health"]["maintenance_queue"]["pending"], 1)
        self.assertEqual(snapshot["health"]["advanced_status"], "healthy")
        self.assertEqual(snapshot["health"]["lexical_index"]["backend"], "portable-sql-bm25")
        self.assertIn("backend", snapshot["health"]["vector_index"])
        self.assertEqual(snapshot["metrics"]["open_memory_conflicts"], 0)
        self.assertEqual(snapshot["metrics"]["retrieval_learning_observations"], 0)
        self.assertEqual(snapshot["lifecycle"]["snapshots"][0]["label"], "before release")
        self.assertEqual(snapshot["lifecycle"]["branches"][0]["name"], "experiment")
        self.assertEqual(snapshot["lifecycle"]["checkpoints"][0]["stage"], "tool_complete")

    async def test_explicit_workspace_cannot_cross_owner_boundary(self):
        service = MemoryObservabilityService(
            session_factory=self.sessions,
            store=self.store,
            inventory_builder=lambda *_args, **_kwargs: {"nodes": [], "edges": [], "metrics": {}},
            settings_loader=AsyncMock(return_value={"enabled": True}),
        )
        with self.assertRaises(KeyError):
            await service.snapshot(user_id="user-1", workspace_id="workspace-2")


class MemoryObservabilityApiTests(unittest.IsolatedAsyncioTestCase):
    def _request(self):
        return SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    async def test_memory_tool_surface_binds_authenticated_identity_and_rejects_identity_injection(
        self,
    ):
        request = self._request()
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        observability = SimpleNamespace(resolve_workspace_path=AsyncMock(return_value="/repo"))
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "memory_observability", observability),
        ):
            listed = await mcp_router.list_memory_core_tools(request, workspace_id="workspace-1")
            with self.assertRaises(HTTPException) as caught:
                await mcp_router.invoke_memory_core_tool(
                    request,
                    "memory.search",
                    mcp_router.MemoryToolInvokeRequest(
                        arguments={"query": "database", "user_id": "attacker"}
                    ),
                    workspace_id="workspace-1",
                )
        self.assertEqual(listed["workspace_id"], "workspace-1")
        self.assertIn("memory.forget", {tool["name"] for tool in listed["tools"]})
        self.assertIn("memory.rebuild", {tool["name"] for tool in listed["tools"]})
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(observability.resolve_workspace_path.await_count, 2)

    async def test_managed_source_forgetter_removes_markdown_section_and_is_idempotent(self):
        request = self._request()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            with patch.object(managed_memory, "DATA_DIR", data_dir):
                result = await managed_memory.write_memory(
                    request,
                    "user-1",
                    str(workspace),
                    "workspace",
                    [
                        {
                            "action": "add",
                            "path": "facts.md",
                            "heading": "Deploy",
                            "memory_id": "managed-deploy",
                            "content": "Run exact verification before deployment.",
                        }
                    ],
                )
                self.assertTrue(result["success"])
                row = {
                    "workspace": str(workspace),
                    "scope": "workspace",
                    "canonical_text": "Run exact verification before deployment.",
                    "structured_value": {
                        "managed_source": {
                            "scope": "workspace",
                            "path": "facts.md",
                            "heading": "Deploy",
                            "memory_id": "managed-deploy",
                        }
                    },
                }
                forgetter = mcp_router._memory_source_forgetter(request, "user-1")
                await forgetter(row)
                await forgetter(row)
                source = managed_memory.resolve_memory_roots("user-1", str(workspace))[1]
                text = (source.root / "facts.md").read_text(errors="replace")
        self.assertNotIn("Run exact verification before deployment.", text)
        self.assertNotIn("managed-deploy", text)

    async def test_snapshot_and_stream_use_admin_identity(self):
        request = self._request()
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        snapshot = {
            "version": 1,
            "workspaces": [],
            "selected_workspace_id": None,
            "nodes": [],
            "edges": [],
            "events": [],
            "recall_traces": [],
            "metrics": {},
            "health": {},
            "fingerprint": "c" * 64,
            "generated_at_ms": 1,
        }
        service = SimpleNamespace(snapshot=AsyncMock(return_value=snapshot))
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "memory_observability", service),
        ):
            result = await mcp_router.get_memory_observability_snapshot(
                request, workspace_id=None, node_limit=400, event_limit=120
            )
            response = await mcp_router.stream_memory_observability(
                request, workspace_id=None, node_limit=400, event_limit=120
            )
            iterator = response.body_iterator.__aiter__()
            retry = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            event = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            await iterator.aclose()
        self.assertEqual(result["fingerprint"], "c" * 64)
        self.assertEqual(retry, "retry: 1500\n\n")
        self.assertIn("event: snapshot", event)
        self.assertEqual(admin.call_count, 2)
        self.assertEqual(service.snapshot.await_count, 2)


if __name__ == "__main__":
    unittest.main()
