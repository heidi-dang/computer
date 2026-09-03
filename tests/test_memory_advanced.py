import math
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.memory.conflicts import MemoryConflictStore
from cptr.memory.domain import MemoryQuery, RetrievalFeedback
from cptr.memory.embeddings import HashingEmbeddingProvider, SqlVectorIndex
from cptr.memory.intelligence import MemoryIntelligenceStore
from cptr.memory.lexical import MemoryLexicalIndex
from cptr.memory.mcp_adapter import MemoryMcpAdapter
from cptr.memory.pgvector import PgVectorIndex
from cptr.memory.service import EmbeddedMemoryService
from cptr.memory.store import SqlMemoryStore
from cptr.models import Base, User, Workspace
from cptr.services.memory_fabric import MemoryFabricStore


class FakeSemanticEmbeddingProvider:
    model_id = "fake-semantic"
    dimensions = 3

    async def embed(self, texts: list[str]) -> list[list[float]]:
        rows: list[list[float]] = []
        for text in texts:
            value = text.lower()
            if "postgres" in value or "database" in value or "sql" in value:
                rows.append([1.0, 0.0, 0.0])
            elif "deploy" in value or "release" in value:
                rows.append([0.0, 1.0, 0.0])
            else:
                rows.append([0.0, 0.0, 1.0])
        return rows


class MemoryAdvancedTests(unittest.IsolatedAsyncioTestCase):
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
        self.lexical = MemoryLexicalIndex(session_factory=self.sessions)
        self.vector = SqlVectorIndex(
            session_factory=self.sessions,
            provider=FakeSemanticEmbeddingProvider(),
        )
        self.conflicts = MemoryConflictStore(session_factory=self.sessions)
        self.intelligence = MemoryIntelligenceStore(session_factory=self.sessions)
        self.settings = {
            "enabled": True,
            "required_for_execution": True,
            "context_char_limit": 9000,
            "canonical_char_limit": 3000,
            "verification_ttl_seconds": 86400,
        }
        self.service = EmbeddedMemoryService(
            store=self.store,
            event_store=self.events,
            managed_context_loader=AsyncMock(
                return_value=__import__(
                    "cptr.memory.domain", fromlist=["ManagedContext"]
                ).ManagedContext(rendered="", items=[])
            ),
            settings_loader=AsyncMock(return_value=self.settings),
            vector_search=self.vector,
            lexical_search=self.lexical,
            conflict_store=self.conflicts,
            intelligence_store=self.intelligence,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _memory(
        self,
        text: str,
        *,
        kind: str = "semantic",
        structured_value=None,
        trust="verified_system_fact",
    ):
        ref = await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind=kind,
            canonical_text=text,
            structured_value=structured_value or {},
            trust_level=trust,
        )
        return ref

    async def test_hashing_embedding_is_deterministic_normalized_and_offline(self):
        provider = HashingEmbeddingProvider(dimensions=64)
        first, second = await provider.embed(["deploy safely", "deploy safely"])
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in first)), 1.0, places=6)

    async def test_vector_index_persists_embeddings_and_semantically_ranks(self):
        database = await self._memory("PostgreSQL is the production database.")
        deploy = await self._memory("Deploy releases using the atomic runbook.", kind="procedure")
        await self.vector.index_memory(await self.store.get_memory(database.memory_id))
        await self.vector.index_memory(await self.store.get_memory(deploy.memory_id))
        scores = await self.vector.score(
            user_id="user-1",
            workspace="/repo",
            query="which SQL database do we use",
            memory_ids=[database.memory_id, deploy.memory_id],
        )
        self.assertGreater(scores[database.memory_id], scores[deploy.memory_id])
        self.assertGreater(scores[database.memory_id], 0.95)

    async def test_portable_bm25_index_rewards_rare_exact_terms(self):
        rare = await self._memory("Deployment target is zeta-canary-47.")
        common = await self._memory("Deployment target is production.")
        await self.lexical.index_memory(await self.store.get_memory(rare.memory_id))
        await self.lexical.index_memory(await self.store.get_memory(common.memory_id))
        scores = await self.lexical.score(
            user_id="user-1",
            workspace="/repo",
            query="zeta-canary-47 deployment target",
            memory_ids=[rare.memory_id, common.memory_id],
        )
        self.assertGreater(scores[rare.memory_id], scores[common.memory_id])
        self.assertEqual(max(scores.values()), 1.0)

    async def test_search_combines_vector_bm25_and_learned_profile(self):
        database = await self._memory("PostgreSQL is the production database.")
        await self.lexical.index_memory(await self.store.get_memory(database.memory_id))
        await self.vector.index_memory(await self.store.get_memory(database.memory_id))
        results = await self.service.search(
            MemoryQuery(user_id="user-1", workspace="/repo", query="production SQL database")
        )
        self.assertEqual(results[0].memory_id, database.memory_id)
        self.assertGreater(results[0].features.get("bm25", 0), 0)
        self.assertGreater(results[0].features.get("vector", 0), 0)
        before = await self.store.get_retrieval_profile("user-1", "/repo")
        await self.service.feedback(
            RetrievalFeedback(
                user_id="user-1",
                workspace="/repo",
                memory_id=database.memory_id,
                context_id="ctx-learning",
                query="production SQL database",
                rank=1,
                score=results[0].score,
                used=True,
                helpful=True,
                outcome="success",
                features=results[0].features,
            )
        )
        after = await self.store.get_retrieval_profile("user-1", "/repo")
        self.assertGreater(after["observations"], before["observations"])
        self.assertAlmostEqual(sum(after["weights"].values()), 1.0, places=6)

    async def test_conflict_detection_and_temporal_resolution_preserve_history(self):
        old = await self._memory(
            "Production database is SQLite.",
            structured_value={
                "subject": "production database",
                "predicate": "is",
                "value": "SQLite",
            },
        )
        new = await self._memory(
            "Production database is PostgreSQL.",
            structured_value={
                "subject": "production database",
                "predicate": "is",
                "value": "PostgreSQL",
            },
        )
        conflicts = await self.service.analyze_conflicts(new.memory_id)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].left_memory_id, old.memory_id)
        self.assertEqual(conflicts[0].right_memory_id, new.memory_id)
        await self.service.resolve_conflict(conflicts[0].conflict_id, resolution="temporal_change")
        old_row = await self.store.get_memory(old.memory_id)
        new_row = await self.store.get_memory(new.memory_id)
        conflict = await self.conflicts.get(conflicts[0].conflict_id, user_id="user-1")
        self.assertEqual(old_row["status"], "superseded")
        self.assertEqual(old_row["superseded_by_id"], new.memory_id)
        self.assertEqual(new_row["status"], "active")
        self.assertEqual(conflict["status"], "resolved")

    async def test_procedure_and_failure_intelligence_are_structured_and_outcome_aware(self):
        procedure = await self._memory(
            "Deployment procedure:\n1. Run targeted tests.\n2. Deploy atomically.\n3. Verify health endpoint.",
            kind="procedure",
        )
        failure = await self._memory(
            "Symptom: SSE reconnect loop. Root cause: lifecycle tied to model turn. Successful fix: persist session ownership. Verification: 30 minute stream passed.",
            kind="failure",
        )
        await self.intelligence.project(await self.store.get_memory(procedure.memory_id))
        await self.intelligence.project(await self.store.get_memory(failure.memory_id))
        procedure_profile = await self.intelligence.get_procedure(procedure.memory_id)
        failure_profile = await self.intelligence.get_failure(failure.memory_id)
        self.assertGreaterEqual(len(procedure_profile["steps"]), 3)
        self.assertIn("health", " ".join(procedure_profile["verification"]).lower())
        self.assertIn("lifecycle", failure_profile["root_cause"].lower())
        await self.intelligence.record_outcome(procedure.memory_id, outcome="success")
        updated = await self.intelligence.get_procedure(procedure.memory_id)
        self.assertEqual(updated["success_count"], 1)

    async def test_snapshot_diff_time_travel_and_verified_branch_merge(self):
        base = await self._memory("Strategy is A.", kind="decision")
        snapshot_a = await self.service.snapshot("user-1", "/repo", label="A")
        await self.service.verify(base.memory_id, user_id="user-1", workspace="/repo")
        branch = await self.service.create_branch(
            "user-1", "/repo", name="experiment", from_snapshot_id=snapshot_a.snapshot_id
        )
        branched = await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="decision",
            canonical_text="Strategy is B.",
            trust_level="verified_system_fact",
            branch_id=branch.branch_id,
            parent_memory_id=base.memory_id,
            verified_at_ms=int(time.time() * 1000),
        )
        snapshot_b = await self.service.snapshot("user-1", "/repo", label="before merge")
        diff = await self.service.compare_snapshots(
            "user-1", "/repo", snapshot_a.snapshot_id, snapshot_b.snapshot_id
        )
        self.assertIn("added", diff)
        merge = await self.service.merge_branch(
            "user-1", "/repo", branch.branch_id, strategy="verified_only"
        )
        self.assertEqual(merge["merged_count"], 1)
        base_after = await self.store.get_memory(base.memory_id)
        self.assertEqual(base_after["status"], "superseded")
        at_old = await self.service.time_travel(
            "user-1", "/repo", at_ms=base_after["valid_from_ms"]
        )
        self.assertTrue(any(row["memory_id"] == base.memory_id for row in at_old))
        self.assertEqual(
            (await self.store.get_memory(branched.memory_id))["branch_id"], branch.branch_id
        )

    async def test_branch_merge_refuses_to_overwrite_a_mainline_parent_that_changed(self):
        base = await self._memory("Strategy is A.", kind="decision")
        snapshot = await self.service.snapshot("user-1", "/repo", label="branch base")
        branch = await self.service.create_branch(
            "user-1", "/repo", name="experiment-race", from_snapshot_id=snapshot.snapshot_id
        )
        branch_memory = await self.store.create_memory(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="decision",
            canonical_text="Strategy is B.",
            trust_level="verified_system_fact",
            branch_id=branch.branch_id,
            parent_memory_id=base.memory_id,
            verified_at_ms=int(time.time() * 1000),
        )
        mainline = await self.store.supersede_memory(
            old_memory_id=base.memory_id,
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            kind="decision",
            canonical_text="Strategy is C.",
            structured_value={},
            source_event_ids=[],
            trust_level="verified_system_fact",
            confidence_ppm=950_000,
            importance_ppm=700_000,
            valid_from_ms=int(time.time() * 1000) + 1,
            verification_expires_at_ms=None,
            branch_id=None,
        )
        merge = await self.service.merge_branch(
            "user-1", "/repo", branch.branch_id, strategy="verified_only"
        )
        self.assertEqual(merge["merged_count"], 0)
        self.assertEqual(merge["conflicted_count"], 1)
        self.assertEqual(merge["conflicted_memory_ids"], [branch_memory.memory_id])
        self.assertEqual((await self.store.get_memory(mainline.memory_id))["status"], "active")
        self.assertEqual((await self.store.get_memory(branch_memory.memory_id))["status"], "active")

    async def test_entity_graph_contributes_relationship_recall_signal(self):
        direct = await self._memory("Payments platform overview.")
        related = await self._memory(
            "Run migration on billing.service after verification.", kind="procedure"
        )
        await self.service.project_graph(
            user_id="user-1",
            workspace="/repo",
            memory_id=direct.memory_id,
            heading="Payments",
        )
        await self.service.project_graph(
            user_id="user-1",
            workspace="/repo",
            memory_id=related.memory_id,
            heading="Payments",
        )

        results = await self.service.search(
            MemoryQuery(user_id="user-1", workspace="/repo", query="Payments", limit=10)
        )
        projected = next(row for row in results if row.memory_id == related.memory_id)
        self.assertGreater(projected.features["graph"], 0.0)
        self.assertIn("graph=", projected.reason)

    async def test_derived_indexes_enforce_workspace_scope_independently(self):
        local = await self._memory("Production database is PostgreSQL.")
        global_memory = await self.store.create_memory(
            user_id="user-1",
            workspace="",
            scope="user",
            kind="semantic",
            canonical_text="Global database guidance uses PostgreSQL.",
            trust_level="verified_system_fact",
        )
        other = await self.store.create_memory(
            user_id="user-1",
            workspace="/other",
            scope="workspace",
            kind="semantic",
            canonical_text="Other workspace database is PostgreSQL.",
            trust_level="verified_system_fact",
        )
        for ref in (local, global_memory, other):
            row = await self.store.get_memory(ref.memory_id)
            await self.lexical.index_memory(row)
            await self.vector.index_memory(row)

        lexical_scores = await self.lexical.score(
            user_id="user-1",
            workspace="/repo",
            query="PostgreSQL database",
            memory_ids=[local.memory_id, global_memory.memory_id, other.memory_id],
        )
        vector_scores = await self.vector.score(
            user_id="user-1",
            workspace="/repo",
            query="PostgreSQL database",
            memory_ids=[local.memory_id, global_memory.memory_id, other.memory_id],
        )
        self.assertIn(local.memory_id, lexical_scores)
        self.assertIn(global_memory.memory_id, lexical_scores)
        self.assertNotIn(other.memory_id, lexical_scores)
        self.assertIn(local.memory_id, vector_scores)
        self.assertIn(global_memory.memory_id, vector_scores)
        self.assertNotIn(other.memory_id, vector_scores)

    async def test_forget_purges_canonical_and_all_rebuildable_derivatives(self):
        memory = await self._memory(
            "Deploy [[Payments]] from heidi/repo only after verification.",
            kind="procedure",
        )
        await self.service.project_graph(
            user_id="user-1",
            workspace="/repo",
            memory_id=memory.memory_id,
            heading="Deploy",
        )
        await self.service.index_memory(memory.memory_id, user_id="user-1", workspace="/repo")
        adapter = MemoryMcpAdapter(
            service=self.service,
            user_id="user-1",
            workspace="/repo",
            allow_mutations=True,
        )

        result = await adapter.call_tool("memory.forget", {"memory_id": memory.memory_id})

        self.assertEqual(result, {"memory_id": memory.memory_id, "forgotten": True})
        with self.assertRaises(KeyError):
            await self.store.get_memory(memory.memory_id)
        self.assertEqual(await self.lexical.coverage(user_id="user-1", workspace="/repo"), 0)
        self.assertEqual(await self.vector.coverage(user_id="user-1", workspace="/repo"), 0)
        graph = await self.service.graph_store.snapshot(user_id="user-1", workspace="/repo")
        self.assertEqual(graph["entities"], [])
        self.assertEqual(graph["relationships"], [])

    async def test_bulk_rebuild_recovers_all_derived_indexes_from_canonical_truth(self):
        first = await self._memory(
            "Deploy [[Payments]] from heidi/repo only after verification.",
            kind="procedure",
        )
        second = await self._memory("Production database is PostgreSQL.")
        self.assertEqual(await self.lexical.coverage(user_id="user-1", workspace="/repo"), 0)
        self.assertEqual(await self.vector.coverage(user_id="user-1", workspace="/repo"), 0)

        rebuilt = await self.service.rebuild_derived_indexes("user-1", "/repo", batch_size=1)

        self.assertEqual(rebuilt["scanned"], 2)
        self.assertEqual(rebuilt["indexed"], 2)
        self.assertEqual(set(rebuilt["memory_ids"]), {first.memory_id, second.memory_id})
        self.assertEqual(await self.lexical.coverage(user_id="user-1", workspace="/repo"), 2)
        self.assertEqual(await self.vector.coverage(user_id="user-1", workspace="/repo"), 2)
        graph = await self.service.graph_store.snapshot(user_id="user-1", workspace="/repo")
        self.assertGreaterEqual(len(graph["entities"]), 2)

    async def test_memory_mcp_adapter_exposes_bounded_core_tools(self):
        memory = await self._memory("Production database is PostgreSQL.")
        adapter = MemoryMcpAdapter(service=self.service, user_id="user-1", workspace="/repo")
        names = {tool["name"] for tool in adapter.tool_definitions()}
        self.assertEqual(
            names,
            {
                "memory.search",
                "memory.inspect",
                "memory.timeline",
                "memory.snapshot",
                "memory.verify",
                "memory.correct",
                "memory.forget",
                "memory.rebuild",
                "memory.conflicts",
                "memory.health",
            },
        )
        result = await adapter.call_tool(
            "memory.inspect",
            {"memory_id": memory.memory_id},
        )
        self.assertEqual(result["memory_id"], memory.memory_id)
        self.assertNotIn("source_event_ids", result)

    async def test_pgvector_adapter_is_optional_configured_and_uses_no_secret_persistence(self):
        with patch.dict(
            os.environ,
            {
                "CPTR_MEMORY_PGVECTOR_URL": "postgresql+asyncpg://user:secret@db.example/memory",
                "CPTR_MEMORY_EMBEDDING_MODEL": "embed-model",
                "CPTR_MEMORY_EMBEDDING_URL": "https://embedding.example/v1/embeddings",
                "CPTR_MEMORY_EMBEDDING_API_KEY": "top-secret",
            },
            clear=False,
        ):
            adapter = PgVectorIndex.from_env(provider=FakeSemanticEmbeddingProvider())
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.model_id, "fake-semantic")
        self.assertNotIn("secret", repr(adapter).lower())
        self.assertEqual(adapter.sanitized_config()["backend"], "postgresql+pgvector")
        self.assertNotIn("url", adapter.sanitized_config())


if __name__ == "__main__":
    unittest.main()
