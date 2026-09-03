import time
import unittest
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.memory.domain import (
    CheckpointState,
    ConsolidationInput,
    ManagedContext,
    MemoryQuery,
    MemoryReplacement,
    PrepareContextInput,
    RetrievalFeedback,
)
from cptr.memory.service import EmbeddedMemoryService, MemoryUnavailableError
from cptr.memory.store import SqlMemoryStore
from cptr.models import Base, User, Workspace
from cptr.services.memory_fabric import MemoryFabricStore


class MemoryCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(User(id="user-1", role="admin", settings={}, created_at=1))
            db.add(
                Workspace(
                    id="workspace-1",
                    user_id="user-1",
                    path="/repo",
                    name="Repo",
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
        await self.engine.dispose()

    def service(self, *, managed_loader=None, settings=None) -> EmbeddedMemoryService:
        return EmbeddedMemoryService(
            store=self.store,
            event_store=self.events,
            managed_context_loader=managed_loader
            or AsyncMock(return_value=ManagedContext(rendered="", items=[])),
            settings_loader=AsyncMock(return_value=settings or self.settings),
        )

    async def test_prepare_context_fails_closed_when_enabled_memory_cannot_load(self):
        service = self.service(
            managed_loader=AsyncMock(side_effect=RuntimeError("disk unavailable"))
        )
        with self.assertRaises(MemoryUnavailableError):
            await service.prepare_context(
                PrepareContextInput(
                    user_id="user-1",
                    workspace="/repo",
                    task_key="task-1",
                    current_message="deploy safely",
                )
            )
        events = await self.events.list_events("user-1", workspace="/repo", limit=10)
        self.assertEqual(events[0]["event_type"], "gate_failed")
        self.assertNotIn("disk unavailable", str(events[0]["payload"]))

    async def test_explicitly_disabled_memory_returns_disabled_bundle_without_failing(self):
        disabled = {**self.settings, "enabled": False}
        loader = AsyncMock(side_effect=AssertionError("loader must not run"))
        service = self.service(managed_loader=loader, settings=disabled)
        bundle = await service.prepare_context(
            PrepareContextInput(user_id="user-1", workspace="/repo", task_key="task-disabled")
        )
        self.assertEqual(bundle.status, "disabled")
        self.assertEqual(bundle.rendered, "")
        loader.assert_not_awaited()

    async def test_prepare_context_compiles_managed_canonical_and_latest_checkpoint(self):
        await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="procedure",
            canonical_text="Deploy by running verification before restart.",
            importance_ppm=900_000,
            trust_level="verified_system_fact",
        )
        service = self.service(
            managed_loader=AsyncMock(
                return_value=ManagedContext(
                    rendered="[Workspace Memory]\nUse atomic deployment.",
                    items=[
                        {
                            "node_id": "managed-1",
                            "scope": "workspace",
                            "path": "deploy.md",
                            "heading": "Deploy",
                            "memory_id": "deploy",
                            "reason": "query matched text",
                        }
                    ],
                )
            )
        )
        first_checkpoint = await service.checkpoint(
            CheckpointState(
                user_id="user-1",
                workspace="/repo",
                task_key="task-2",
                stage="planned",
                state={"step": 1},
            )
        )
        bundle = await service.prepare_context(
            PrepareContextInput(
                user_id="user-1",
                workspace="/repo",
                task_key="task-2",
                current_message="deploy verification restart",
            )
        )
        self.assertEqual(bundle.status, "ready")
        self.assertIn("Use atomic deployment", bundle.rendered)
        self.assertIn("Deploy by running verification", bundle.rendered)
        self.assertEqual(bundle.checkpoint_id, first_checkpoint.checkpoint_id)
        self.assertGreaterEqual(bundle.memory_version, 1)
        self.assertTrue(bundle.context_id.startswith("memctx_"))
        events = await self.events.list_events("user-1", workspace="/repo", limit=10)
        self.assertTrue(any(row["event_type"] == "context_prepared" for row in events))

    async def test_checkpoint_versions_increment_and_latest_checkpoint_is_recoverable(self):
        service = self.service()
        first = await service.checkpoint(
            CheckpointState(
                user_id="user-1",
                workspace="/repo",
                task_key="task-3",
                stage="context_prepared",
                state={"context_id": "one"},
            )
        )
        second = await service.checkpoint(
            CheckpointState(
                user_id="user-1",
                workspace="/repo",
                task_key="task-3",
                stage="tool_complete",
                state={"tool": "terminal"},
            )
        )
        latest = await self.store.latest_checkpoint("user-1", "/repo", "task-3")
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        self.assertEqual(latest["checkpoint_id"], second.checkpoint_id)
        self.assertEqual(latest["stage"], "tool_complete")

    async def test_supersession_preserves_temporal_history_and_search_prefers_active_fact(self):
        service = self.service()
        old = await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="semantic",
            canonical_text="Production database is SQLite.",
            trust_level="verified_system_fact",
            valid_from_ms=1000,
        )
        replacement = await service.supersede(
            old.memory_id,
            MemoryReplacement(
                user_id="user-1",
                workspace="/repo",
                scope="workspace",
                kind="semantic",
                canonical_text="Production database is PostgreSQL.",
                trust_level="verified_system_fact",
                valid_from_ms=2000,
                source_event_ids=["evt-cutover"],
            ),
        )
        historical = await self.store.get_memory(old.memory_id)
        self.assertEqual(historical["status"], "superseded")
        self.assertEqual(historical["valid_until_ms"], 2000)
        self.assertEqual(historical["superseded_by_id"], replacement.memory_id)
        results = await service.search(
            MemoryQuery(
                user_id="user-1",
                workspace="/repo",
                query="production database PostgreSQL",
                limit=10,
            )
        )
        self.assertEqual(results[0].memory_id, replacement.memory_id)
        self.assertFalse(any(row.memory_id == old.memory_id for row in results))
        historical_results = await service.search(
            MemoryQuery(
                user_id="user-1",
                workspace="/repo",
                query="production database SQLite",
                include_historical=True,
                limit=10,
            )
        )
        self.assertTrue(any(row.memory_id == old.memory_id for row in historical_results))

    async def test_consolidation_deduplicates_and_classifies_procedure_and_failure(self):
        service = self.service()
        first = await service.consolidate(
            ConsolidationInput(
                user_id="user-1",
                workspace="/repo",
                scope="workspace",
                text="Deployment procedure: verify tests, deploy atomically, then smoke test.",
                heading="Deployment procedure",
                source_event_ids=["event-1"],
                trust_level="agent_observation",
            )
        )
        duplicate = await service.consolidate(
            ConsolidationInput(
                user_id="user-1",
                workspace="/repo",
                scope="workspace",
                text="Deployment procedure: verify tests, deploy atomically, then smoke test.",
                heading="Deployment procedure",
                source_event_ids=["event-2"],
                trust_level="agent_observation",
            )
        )
        failure = await service.consolidate(
            ConsolidationInput(
                user_id="user-1",
                workspace="/repo",
                scope="workspace",
                text="Root cause failure: stale process kept port 3000 occupied; successful fix stopped it.",
                heading="Root cause",
                source_event_ids=["event-3"],
                trust_level="verified_system_fact",
            )
        )
        self.assertEqual(first.memory_id, duplicate.memory_id)
        self.assertEqual(first.kind, "procedure")
        self.assertEqual(failure.kind, "failure")
        stored = await self.store.get_memory(first.memory_id)
        self.assertEqual(set(stored["source_event_ids"]), {"event-1", "event-2"})

    async def test_decay_and_verification_ttl_lower_rank_without_deleting_memory(self):
        now_ms = int(time.time() * 1000)
        stale = await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="semantic",
            canonical_text="Deploy target is production-east.",
            confidence_ppm=950_000,
            importance_ppm=800_000,
            verified_at_ms=now_ms - 10 * 86400 * 1000,
            verification_expires_at_ms=now_ms - 1000,
            updated_at_ms=now_ms - 10 * 86400 * 1000,
        )
        fresh = await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="semantic",
            canonical_text="Deploy target is production-west.",
            confidence_ppm=950_000,
            importance_ppm=800_000,
            verified_at_ms=now_ms,
            verification_expires_at_ms=now_ms + 86400 * 1000,
            updated_at_ms=now_ms,
        )
        service = self.service()
        results = await service.search(
            MemoryQuery(
                user_id="user-1",
                workspace="/repo",
                query="deploy target production",
                limit=10,
                now_ms=now_ms,
            )
        )
        by_id = {row.memory_id: row for row in results}
        self.assertIn(stale.memory_id, by_id)
        self.assertIn(fresh.memory_id, by_id)
        self.assertTrue(by_id[stale.memory_id].verification_stale)
        self.assertLess(by_id[stale.memory_id].score, by_id[fresh.memory_id].score)
        self.assertEqual((await self.store.get_memory(stale.memory_id))["status"], "active")

    async def test_retrieval_feedback_persists_usage_signal(self):
        memory = await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="semantic",
            canonical_text="Run the targeted test before the full suite.",
        )
        service = self.service()
        await service.feedback(
            RetrievalFeedback(
                user_id="user-1",
                workspace="/repo",
                memory_id=memory.memory_id,
                context_id="memctx_feedback",
                query="which tests",
                rank=1,
                score=0.9,
                used=True,
                helpful=True,
                outcome="success",
            )
        )
        stored = await self.store.get_memory(memory.memory_id)
        self.assertEqual(stored["access_count"], 1)
        feedback = await self.store.list_feedback("user-1", memory.memory_id)
        self.assertEqual(feedback[0]["outcome"], "success")
        self.assertTrue(feedback[0]["helpful"])

    async def test_snapshot_branch_and_restore_are_versioned_without_destroying_history(self):
        service = self.service()
        base = await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="decision",
            canonical_text="Use strategy A.",
        )
        snapshot = await service.snapshot("user-1", "/repo", label="before experiment")
        branch = await service.create_branch(
            "user-1", "/repo", name="experiment", from_snapshot_id=snapshot.snapshot_id
        )
        branched = await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="decision",
            canonical_text="Try strategy B.",
            branch_id=branch.branch_id,
        )
        await service.restore_snapshot("user-1", "/repo", snapshot.snapshot_id)
        base_after = await self.store.get_memory(base.memory_id)
        branch_after = await self.store.get_memory(branched.memory_id)
        self.assertEqual(base_after["status"], "active")
        self.assertEqual(branch_after["branch_id"], branch.branch_id)
        self.assertEqual(branch_after["status"], "active")
        restore_events = await self.events.list_events("user-1", workspace="/repo", limit=20)
        self.assertTrue(any(row["event_type"] == "snapshot_restored" for row in restore_events))


if __name__ == "__main__":
    unittest.main()
