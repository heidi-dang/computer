import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.events import EVENTS
from cptr.models.users import User
from cptr.models.workspaces import (
    Repository,
    RepositoryCheckout,
    Workspace,
    WorkspaceRepository,
)
from cptr.models.workspace_context import (
    CheckpointDivergence,
    EnvironmentEvidence,
    InstructionContext,
    MemoryEvidence,
    RepoEvidence,
    WorkspaceContextSnapshot,
)
from cptr.routers import control_stream
from cptr.services.live_events import (
    LiveEventHub,
    LiveEventStore,
    workspace_target_key,
)
from cptr.services.workspace_observability import (
    ContextInvalidationReason,
    ContextValidationTokens,
    WorkspaceContextCache,
    WorkspaceLiveProjectionService,
    WorkspaceObservabilityMetrics,
)
from cptr.utils.db import get_db, init_db


def _make_snapshot(
    workspace_id: str = "ws-1",
    revision: str = "rev-100",
    memory_version: int = 1,
    checkpoint_id: str = "chk-1",
    instructions: str = "Do this task",
    hostname: str = "host-1",
) -> WorkspaceContextSnapshot:
    return WorkspaceContextSnapshot(
        workspace_id=workspace_id,
        workspace_root=f"/workspaces/{workspace_id}",
        user_id="user-1",
        repo=RepoEvidence(revision=revision, is_dirty=False, branch="main"),
        memory=MemoryEvidence(memory_version=memory_version, canonical_memories=["rule 1"]),
        divergence=CheckpointDivergence(checkpoint_id=checkpoint_id, is_diverged=False),
        instructions=InstructionContext(system_instructions=instructions),
        environment=EnvironmentEvidence(hostname=hostname),
    )


class WorkspaceObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with await get_db() as db:
            for uid in ("user-1", "user-stream-test"):
                u = await db.get(User, uid)
                if not u:
                    db.add(
                        User(
                            id=uid,
                            display_name=uid,
                            role="admin",
                            settings={},
                            created_at=int(time.time()),
                        )
                    )
            await db.commit()
        self.hub = LiveEventHub(store=LiveEventStore(persistent=False))
        self.metrics = WorkspaceObservabilityMetrics()
        self.cache = WorkspaceContextCache(
            max_entries=4,
            default_ttl_seconds=60.0,
            metrics=self.metrics,
            hub=self.hub,
        )
        self.projection_service = WorkspaceLiveProjectionService(
            cache=self.cache,
            metrics=self.metrics,
            hub=self.hub,
        )

    # -------------------------------------------------------------------------
    # Invalidation across all 7 axes
    # -------------------------------------------------------------------------

    async def test_invalidation_on_instructions_change(self):
        snap = _make_snapshot(instructions="initial instructions")
        await self.cache.put("ws-1", snap)
        self.assertTrue(self.cache.contains("ws-1"))

        # Access with differing instructions token
        changed_tokens = ContextValidationTokens.from_snapshot(
            _make_snapshot(instructions="updated instructions")
        )
        result = await self.cache.get("ws-1", current_tokens=changed_tokens)
        self.assertIsNone(result)
        self.assertFalse(self.cache.contains("ws-1"))
        self.assertEqual(self.metrics.invalidations_by_reason["instructions"], 1)

        # Explicit method
        await self.cache.put("ws-1", snap)
        inv = await self.cache.invalidate_on_instructions_change("ws-1")
        self.assertTrue(inv)
        self.assertFalse(self.cache.contains("ws-1"))
        self.assertEqual(self.metrics.invalidations_by_reason["instructions"], 2)

    async def test_invalidation_on_environment_change(self):
        snap = _make_snapshot(hostname="worker-box-1")
        await self.cache.put("ws-1", snap)

        changed_tokens = ContextValidationTokens.from_snapshot(
            _make_snapshot(hostname="worker-box-2")
        )
        result = await self.cache.get("ws-1", current_tokens=changed_tokens)
        self.assertIsNone(result)
        self.assertEqual(self.metrics.invalidations_by_reason["environment"], 1)

        # Explicit method
        await self.cache.put("ws-1", snap)
        inv = await self.cache.invalidate_on_environment_change("ws-1")
        self.assertTrue(inv)
        self.assertEqual(self.metrics.invalidations_by_reason["environment"], 2)

    async def test_invalidation_on_memory_change(self):
        snap = _make_snapshot(memory_version=1)
        await self.cache.put("ws-1", snap)

        changed_tokens = ContextValidationTokens.from_snapshot(_make_snapshot(memory_version=2))
        result = await self.cache.get("ws-1", current_tokens=changed_tokens)
        self.assertIsNone(result)
        self.assertEqual(self.metrics.invalidations_by_reason["memory"], 1)

        # Explicit method
        await self.cache.put("ws-1", snap)
        inv = await self.cache.invalidate_on_memory_change("ws-1")
        self.assertTrue(inv)
        self.assertEqual(self.metrics.invalidations_by_reason["memory"], 2)

    async def test_invalidation_on_checkpoint_change(self):
        snap = _make_snapshot(checkpoint_id="chk-v1")
        await self.cache.put("ws-1", snap)

        changed_tokens = ContextValidationTokens.from_snapshot(
            _make_snapshot(checkpoint_id="chk-v2")
        )
        result = await self.cache.get("ws-1", current_tokens=changed_tokens)
        self.assertIsNone(result)
        self.assertEqual(self.metrics.invalidations_by_reason["checkpoint"], 1)

        # Explicit method
        await self.cache.put("ws-1", snap)
        inv = await self.cache.invalidate_on_checkpoint_change("ws-1")
        self.assertTrue(inv)
        self.assertEqual(self.metrics.invalidations_by_reason["checkpoint"], 2)

    async def test_invalidation_on_revision_change(self):
        snap = _make_snapshot(revision="commit-aaa")
        await self.cache.put("ws-1", snap)

        changed_tokens = ContextValidationTokens.from_snapshot(
            _make_snapshot(revision="commit-bbb")
        )
        result = await self.cache.get("ws-1", current_tokens=changed_tokens)
        self.assertIsNone(result)
        self.assertEqual(self.metrics.invalidations_by_reason["revision"], 1)

        # Explicit method
        await self.cache.put("ws-1", snap)
        inv = await self.cache.invalidate_on_revision_change("ws-1")
        self.assertTrue(inv)
        self.assertEqual(self.metrics.invalidations_by_reason["revision"], 2)

    async def test_invalidation_on_role_change(self):
        snap = _make_snapshot()
        tokens = ContextValidationTokens.from_snapshot(snap, role="primary")
        await self.cache.put("ws-1", snap, tokens=tokens)

        # Token mismatch when role becomes "dependency"
        changed_tokens = ContextValidationTokens.from_snapshot(snap, role="dependency")
        result = await self.cache.get("ws-1", current_tokens=changed_tokens)
        self.assertIsNone(result)
        self.assertEqual(self.metrics.invalidations_by_reason["role"], 1)

        # Explicit method
        await self.cache.put("ws-1", snap)
        inv = await self.cache.invalidate_on_role_change("ws-1")
        self.assertTrue(inv)
        self.assertEqual(self.metrics.invalidations_by_reason["role"], 2)

    async def test_invalidation_on_canonical_checkout_change(self):
        snap = _make_snapshot()
        tokens = ContextValidationTokens.from_snapshot(snap, canonical_checkout="checkout-1")
        await self.cache.put("ws-1", snap, tokens=tokens)

        # Token mismatch when canonical checkout switches to checkout-2
        changed_tokens = ContextValidationTokens.from_snapshot(
            snap, canonical_checkout="checkout-2"
        )
        result = await self.cache.get("ws-1", current_tokens=changed_tokens)
        self.assertIsNone(result)
        self.assertEqual(self.metrics.invalidations_by_reason["canonical_checkout"], 1)

        # Explicit method
        await self.cache.put("ws-1", snap)
        inv = await self.cache.invalidate_on_canonical_checkout_change("ws-1")
        self.assertTrue(inv)
        self.assertEqual(self.metrics.invalidations_by_reason["canonical_checkout"], 2)

    # -------------------------------------------------------------------------
    # Bounded Cache Behavior & LRU Eviction
    # -------------------------------------------------------------------------

    async def test_bounded_capacity_and_lru_eviction(self):
        cache = WorkspaceContextCache(
            max_entries=2,
            metrics=self.metrics,
            hub=self.hub,
        )
        snap1 = _make_snapshot("ws-1")
        snap2 = _make_snapshot("ws-2")
        snap3 = _make_snapshot("ws-3")

        await cache.put("ws-1", snap1)
        await cache.put("ws-2", snap2)
        self.assertEqual(cache.size, 2)

        # Access ws-1 so ws-2 is the LRU
        hit = await cache.get("ws-1")
        self.assertIsNotNone(hit)

        # Put ws-3: should evict ws-2
        await cache.put("ws-3", snap3)
        self.assertEqual(cache.size, 2)
        self.assertTrue(cache.contains("ws-1"))
        self.assertFalse(cache.contains("ws-2"))
        self.assertTrue(cache.contains("ws-3"))

        self.assertEqual(self.metrics.evictions, 1)
        self.assertEqual(self.metrics.invalidations_by_reason["capacity_eviction"], 1)

    async def test_ttl_expiration(self):
        cache = WorkspaceContextCache(
            max_entries=4,
            default_ttl_seconds=0.05,
            metrics=self.metrics,
            hub=self.hub,
        )
        snap = _make_snapshot("ws-short")
        await cache.put("ws-short", snap)
        self.assertTrue(cache.contains("ws-short"))

        await asyncio.sleep(0.06)
        # Should detect expiration on get
        result = await cache.get("ws-short")
        self.assertIsNone(result)
        self.assertEqual(self.metrics.invalidations_by_reason["ttl_expired"], 1)

    async def test_cache_metrics_and_hit_ratio(self):
        snap = _make_snapshot("ws-1")
        await self.cache.put("ws-1", snap)

        # 2 hits
        await self.cache.get("ws-1")
        await self.cache.get("ws-1")
        # 1 miss
        await self.cache.get("ws-nonexistent")

        stats = self.metrics.snapshot()
        self.assertEqual(stats["hits"], 2)
        self.assertEqual(stats["misses"], 1)
        self.assertAlmostEqual(stats["hit_ratio"], 2 / 3, places=2)

    # -------------------------------------------------------------------------
    # Events & LiveEventHub Integration (No duplicate bus)
    # -------------------------------------------------------------------------

    async def test_live_event_hub_event_publishing(self):
        sub = self.hub.subscribe(workspace_target_key("ws-events")).__aiter__()

        snap = _make_snapshot("ws-events")
        await self.cache.put("ws-events", snap)

        evt_updated = await sub.__anext__()
        self.assertEqual(evt_updated.event_type, EVENTS.WORKSPACE_CONTEXT_UPDATED.name)
        self.assertEqual(evt_updated.target_key, "workspace:ws-events")

        await self.cache.invalidate("ws-events", reason=ContextInvalidationReason.REVISION)
        evt_inval = await sub.__anext__()
        self.assertEqual(evt_inval.event_type, EVENTS.WORKSPACE_CONTEXT_INVALIDATED.name)
        self.assertEqual(evt_inval.payload["reason"], "revision")

    # -------------------------------------------------------------------------
    # Live Projections
    # -------------------------------------------------------------------------

    async def test_live_projection_building(self):
        # Seed test workspace and repository in DB
        now = int(time.time())
        import uuid

        test_uid = uuid.uuid4().hex[:8]
        ws = await Workspace.upsert(
            user_id="user-1",
            path=f"/tmp/ws-proj-test-{test_uid}",
            name="Project Test Workspace",
            data={},
        )
        ws_id = str(ws.id)

        now = int(time.time())
        rid = f"repo-proj-{int(time.time() * 1000)}"
        async with await get_db() as db:
            repo = Repository(
                id=rid,
                user_id="user-1",
                name="my-repo",
                canonical_remote_identity=f"github.com/org/{rid}",
                default_branch="main",
                created_at=now,
                updated_at=now,
            )
            db.add(repo)
            ws_repo = WorkspaceRepository(
                workspace_id=ws_id,
                repository_id=rid,
                role="primary",
                primary=True,
                sort_order=0,
                enabled=True,
            )
            db.add(ws_repo)
            checkout = RepositoryCheckout(
                id=f"chk-{rid}",
                repository_id=rid,
                path="/tmp/ws-proj-test",
                canonical=True,
                branch="main",
                last_seen_revision="abc1234",
                available=True,
                created_at=now,
                last_seen_at=now,
            )
            db.add(checkout)
            await db.commit()

        # Cache a context snapshot for ws
        snap = _make_snapshot(ws_id)
        await self.cache.put(ws_id, snap)

        proj = await self.projection_service.get_projection(ws_id, user_id="user-1")
        self.assertEqual(proj["workspace_id"], ws_id)
        self.assertEqual(proj["name"], "Project Test Workspace")
        self.assertEqual(proj["status"], "active")
        self.assertEqual(len(proj["repositories"]), 1)
        self.assertEqual(proj["repositories"][0]["name"], "my-repo")
        self.assertEqual(proj["repositories"][0]["role"], "primary")
        self.assertTrue(proj["repositories"][0]["primary"])
        self.assertEqual(proj["repositories"][0]["checkout"]["branch"], "main")
        self.assertTrue(proj["context_cache"]["is_cached"])
        self.assertTrue(bool(proj["fingerprint"]))

        # Invalidate for repository
        inv_count = await self.cache.invalidate_for_repository(rid, reason="revision")
        self.assertEqual(inv_count, 1)
        self.assertFalse(self.cache.contains(ws_id))

    # -------------------------------------------------------------------------
    # Control Stream SSE Endpoints
    # -------------------------------------------------------------------------

    async def test_workspace_stream_and_endpoints(self):
        ws = await Workspace.upsert(
            user_id="user-stream-test",
            path="/tmp/ws-stream-test",
            name="Stream Test Workspace",
            data={},
        )
        ws_id = str(ws.id)

        request = SimpleNamespace(
            headers={},
            query_params={},
            is_disconnected=AsyncMock(return_value=False),
        )

        with (
            patch.object(control_stream, "live_event_hub", self.hub),
            patch.object(control_stream, "_user", new=AsyncMock(return_value="user-stream-test")),
            patch.object(control_stream, "workspace_projection_service", self.projection_service),
            patch.object(control_stream, "workspace_context_cache", self.cache),
            patch.object(control_stream, "workspace_metrics", self.metrics),
        ):
            # 1. Projection endpoint
            proj_data = await control_stream.workspace_projection_endpoint(request, ws_id)
            self.assertEqual(proj_data["workspace_id"], ws_id)

            # 2. Snapshot endpoint
            snapshot_data = await control_stream.workspace_stream_snapshot_endpoint(request, ws_id)
            self.assertEqual(snapshot_data["target"], "workspace")
            self.assertEqual(snapshot_data["snapshot"]["workspace_id"], ws_id)

            # 3. Metrics endpoint
            metrics_data = await control_stream.workspace_metrics_endpoint(request, ws_id)
            self.assertIn("hits", metrics_data)
            self.assertIn("invalidations_by_reason", metrics_data)

            # 4. SSE Stream endpoint
            response = await control_stream.workspace_stream_endpoint(request, ws_id)
            iterator = response.body_iterator.__aiter__()
            initial_snapshot = await iterator.__anext__()
            self.assertIn('"target":"workspace"', initial_snapshot)
            self.assertIn(ws_id, initial_snapshot)

            # Publish live event to workspace
            await self.hub.publish(
                user_id="user-stream-test",
                target_key=workspace_target_key(ws_id),
                event_type=EVENTS.WORKSPACE_REVISION_CHANGED.name,
                payload={"workspace_id": ws_id, "revision": "rev-999"},
            )
            live_evt = await iterator.__anext__()
            self.assertIn(EVENTS.WORKSPACE_REVISION_CHANGED.name, live_evt)
            self.assertIn("rev-999", live_evt)

    # -------------------------------------------------------------------------
    # Multi-Workspace Invalidation & Repository Scoping
    # -------------------------------------------------------------------------

    async def test_invalidate_for_repository_affects_multiple_workspaces(self):
        import uuid

        test_uid = uuid.uuid4().hex[:8]
        shared_rid = f"shared-repo-{test_uid}"
        now = int(time.time())
        ws1 = await Workspace.upsert(
            user_id="user-1", path=f"/tmp/ws-repo-1-{test_uid}", name="WS 1", data={}
        )
        ws2 = await Workspace.upsert(
            user_id="user-1", path=f"/tmp/ws-repo-2-{test_uid}", name="WS 2", data={}
        )
        ws1_id, ws2_id = str(ws1.id), str(ws2.id)

        async with await get_db() as db:
            repo = Repository(
                id=shared_rid,
                user_id="user-1",
                name="shared",
                canonical_remote_identity=f"github.com/org/{shared_rid}",
                created_at=now,
                updated_at=now,
            )
            db.add(repo)
            db.add(
                WorkspaceRepository(
                    workspace_id=ws1_id, repository_id=shared_rid, role="primary", primary=True
                )
            )
            db.add(
                WorkspaceRepository(
                    workspace_id=ws2_id, repository_id=shared_rid, role="dependency", primary=False
                )
            )
            await db.commit()

        await self.cache.put(ws1_id, _make_snapshot(ws1_id))
        await self.cache.put(ws2_id, _make_snapshot(ws2_id))
        self.assertTrue(self.cache.contains(ws1_id))
        self.assertTrue(self.cache.contains(ws2_id))

        count = await self.cache.invalidate_for_repository(shared_rid, reason="revision")
        self.assertEqual(count, 2)
        self.assertFalse(self.cache.contains(ws1_id))
        self.assertFalse(self.cache.contains(ws2_id))
        self.assertEqual(self.metrics.invalidations_by_reason["revision"], 2)

    async def test_invalidate_all_clears_cache_and_emits_events(self):
        await self.cache.put("ws-a", _make_snapshot("ws-a"))
        await self.cache.put("ws-b", _make_snapshot("ws-b"))
        self.assertEqual(self.cache.size, 2)

        cleared = await self.cache.invalidate_all(reason="manual")
        self.assertEqual(cleared, 2)
        self.assertEqual(self.cache.size, 0)
        self.assertEqual(self.metrics.invalidations_by_reason["manual"], 2)

    # -------------------------------------------------------------------------
    # Telemetry Span Recording (when enabled)
    # -------------------------------------------------------------------------

    async def test_telemetry_span_recording(self):
        from unittest.mock import MagicMock
        from cptr.services.telemetry import CptrTelemetry

        otel = CptrTelemetry(enabled=True)
        otel._configured = True
        span_mock = MagicMock()
        span_mock.__enter__ = lambda s: s
        span_mock.__exit__ = lambda s, *args: None
        otel.start_span = MagicMock(return_value=span_mock)

        metrics = WorkspaceObservabilityMetrics(telemetry=otel)
        cache = WorkspaceContextCache(metrics=metrics, hub=self.hub)
        await cache.put("ws-otel", _make_snapshot("ws-otel"))
        await cache.invalidate("ws-otel", reason=ContextInvalidationReason.REVISION)

        otel.start_span.assert_called_once()
        args, kwargs = otel.start_span.call_args
        self.assertEqual(args[0], "workspace.context.invalidation")
        self.assertEqual(kwargs["attributes"]["cptr.component"], "workspace_os")
        self.assertEqual(
            kwargs["attributes"]["cptr.operation"], "context_cache.invalidate.revision"
        )

    # -------------------------------------------------------------------------
    # Workbench Live Mirroring
    # -------------------------------------------------------------------------

    async def test_workspace_event_mirrors_to_workbench(self):
        from cptr.services.live_events import (
            publish_workspace_event,
            workbench_target_key,
        )

        with patch("cptr.services.live_events.live_event_hub", self.hub):
            sub_ws = self.hub.subscribe(workspace_target_key("ws-mirror")).__aiter__()
            sub_wb = self.hub.subscribe(workbench_target_key("session-123")).__aiter__()

            task_ws = asyncio.create_task(sub_ws.__anext__())
            task_wb = asyncio.create_task(sub_wb.__anext__())
            await asyncio.sleep(0.01)

            await publish_workspace_event(
                user_id="user-1",
                workspace_id="ws-mirror",
                event_type=EVENTS.WORKSPACE_CONTEXT_UPDATED.name,
                payload={"custom": "data"},
                workbench_session_id="session-123",
                hub=self.hub,
            )

            ws_evt = await task_ws
            self.assertEqual(ws_evt.event_type, EVENTS.WORKSPACE_CONTEXT_UPDATED.name)
            self.assertEqual(ws_evt.payload["custom"], "data")

            wb_evt = await task_wb
            self.assertEqual(wb_evt.event_type, EVENTS.WORKSPACE_CONTEXT_UPDATED.name)
            self.assertEqual(wb_evt.payload["target"]["type"], "workspace")
            self.assertEqual(wb_evt.payload["target"]["workspace_id"], "ws-mirror")
            self.assertEqual(wb_evt.payload["payload"]["custom"], "data")

    # -------------------------------------------------------------------------
    # Live Projections Event Recording and Fingerprint Divergence
    # -------------------------------------------------------------------------

    async def test_live_projection_records_events_and_detects_changes(self):
        ws = await Workspace.upsert(
            user_id="user-1", path="/tmp/ws-event-rec", name="Event Rec", data={}
        )
        ws_id = str(ws.id)

        proj1 = await self.projection_service.get_projection(ws_id, user_id="user-1")
        fp1 = proj1["fingerprint"]

        # Cache a context entry
        await self.cache.put(ws_id, _make_snapshot(ws_id))

        # Refresh projection: should detect changed cache state
        proj2 = await self.projection_service.get_projection(
            ws_id, user_id="user-1", force_refresh=True
        )
        fp2 = proj2["fingerprint"]
        self.assertNotEqual(fp1, fp2)
        self.assertTrue(proj2["context_cache"]["is_cached"])

    # -------------------------------------------------------------------------
    # HTTP Invalidate Endpoint
    # -------------------------------------------------------------------------

    async def test_workspace_context_invalidate_http_endpoint(self):
        await self.cache.put("ws-inval-http", _make_snapshot("ws-inval-http"))
        self.assertTrue(self.cache.contains("ws-inval-http"))

        request = SimpleNamespace(
            headers={},
            query_params={},
            json=AsyncMock(return_value={"reason": "revision", "details": {"commit": "123"}}),
            is_disconnected=AsyncMock(return_value=False),
        )

        with (
            patch.object(control_stream, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(control_stream, "workspace_context_cache", self.cache),
        ):
            res = await control_stream.workspace_context_invalidate_endpoint(
                request, "ws-inval-http"
            )
            self.assertEqual(res["workspace_id"], "ws-inval-http")
            self.assertTrue(res["invalidated"])
            self.assertEqual(res["reason"], "revision")
            self.assertFalse(self.cache.contains("ws-inval-http"))

    # -------------------------------------------------------------------------
    # Snapshot Content Digest Sensitivity (Instructions, Environment, Memory, Divergence)
    # -------------------------------------------------------------------------

    def test_snapshot_digest_sensitivity(self):
        base = _make_snapshot("ws-sens")
        base_digest = base.compute_digest()

        # Instructions change
        s_inst = _make_snapshot("ws-sens", instructions="Brand new instructions")
        self.assertNotEqual(base_digest, s_inst.compute_digest())

        # Environment change
        s_env = _make_snapshot("ws-sens", hostname="new-host-99")
        self.assertNotEqual(base_digest, s_env.compute_digest())

        # Memory canonical memories change with same item count
        s_mem = _make_snapshot("ws-sens")
        s_mem.memory.canonical_memories = ["rule 1 modified"]
        self.assertNotEqual(base_digest, s_mem.compute_digest())

        # Checkpoint divergence revision change
        s_div = _make_snapshot("ws-sens")
        s_div.divergence.checkpoint_revision = "rev-xyz"
        self.assertNotEqual(base_digest, s_div.compute_digest())

    # -------------------------------------------------------------------------
    # Invalidation on None-to-Value Transitions
    # -------------------------------------------------------------------------

    async def test_invalidation_when_transitioning_from_none_to_value(self):
        # Snapshot without checkpoint
        snap_no_chk = _make_snapshot("ws-none-chk", checkpoint_id=None)
        await self.cache.put("ws-none-chk", snap_no_chk)
        self.assertTrue(self.cache.contains("ws-none-chk"))

        # Access with tokens from snapshot with checkpoint created
        snap_with_chk = _make_snapshot("ws-none-chk", checkpoint_id="new-chk-1")
        tokens_with_chk = ContextValidationTokens.from_snapshot(snap_with_chk)

        res = await self.cache.get("ws-none-chk", current_tokens=tokens_with_chk)
        self.assertIsNone(res)
        self.assertFalse(self.cache.contains("ws-none-chk"))
        self.assertEqual(self.metrics.invalidations_by_reason["checkpoint"], 1)

    # -------------------------------------------------------------------------
    # Live Mutation Event Invalidation
    # -------------------------------------------------------------------------

    async def test_live_mutation_event_invalidates_context_cache(self):
        from cptr.services.live_events import publish_workspace_event

        ws_id = "ws-live-inval"
        await self.cache.put(ws_id, _make_snapshot(ws_id))
        self.assertTrue(self.cache.contains(ws_id))

        with (
            patch("cptr.services.live_events.live_event_hub", self.hub),
            patch(
                "cptr.services.workspace_observability.workspace_projection_service",
                self.projection_service,
            ),
        ):
            await publish_workspace_event(
                user_id="user-1",
                workspace_id=ws_id,
                event_type=EVENTS.WORKSPACE_REVISION_CHANGED.name,
                payload={"workspace_id": ws_id, "revision": "rev-new"},
                hub=self.hub,
            )

        # Cache should be invalidated synchronously on event receipt
        self.assertFalse(self.cache.contains(ws_id))
        self.assertEqual(self.metrics.invalidations_by_reason["revision"], 1)

    # -------------------------------------------------------------------------
    # Workbench Active Sessions Integration in Projection
    # -------------------------------------------------------------------------

    async def test_projection_includes_workbench_sessions_and_health(self):
        import uuid
        from cptr.models.control import WorkbenchSession

        now = int(time.time())
        test_uid = uuid.uuid4().hex[:8]
        ws = await Workspace.upsert(
            user_id="user-1",
            path=f"/tmp/ws-wb-test-{test_uid}",
            name="WB Test Workspace",
            data={},
        )
        ws_id = str(ws.id)

        # Create active Workbench session linked to ws_id
        async with await get_db() as db:
            wb_session = WorkbenchSession(
                id=f"wbs-test-{int(time.time() * 1000)}",
                user_id="user-1",
                name="My Workbench Session",
                workspace_id=ws_id,
                status="OPEN",
                created_at=now,
                updated_at=now,
            )
            db.add(wb_session)
            await db.commit()

        proj = await self.projection_service.get_projection(ws_id, user_id="user-1")
        self.assertIn("workbench", proj)
        self.assertEqual(proj["workbench"]["active_sessions_count"], 1)
        self.assertEqual(proj["workbench"]["sessions"][0]["name"], "My Workbench Session")

        self.assertIn("health", proj)
        self.assertEqual(proj["health"]["status"], "active")
        self.assertIn("checks", proj["health"])
        self.assertEqual(proj["health"]["checks"]["workbench_sessions_active"], 1)

    # -------------------------------------------------------------------------
    # Observability Stats Endpoint
    # -------------------------------------------------------------------------

    async def test_observability_stats_endpoint(self):
        request = SimpleNamespace(
            headers={},
            query_params={},
            is_disconnected=AsyncMock(return_value=False),
        )

        with (
            patch.object(control_stream, "_user", new=AsyncMock(return_value="user-stream-test")),
            patch.object(control_stream, "workspace_context_cache", self.cache),
        ):
            stats_res = await control_stream.workspace_observability_stats_endpoint(request)
            self.assertIn("size", stats_res)
            self.assertIn("metrics", stats_res)
