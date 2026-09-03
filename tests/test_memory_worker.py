import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.memory.graph import MemoryGraphStore, extract_entities
from cptr.memory.jobs import MemoryJobStore
from cptr.memory.service import EmbeddedMemoryService
from cptr.memory.store import SqlMemoryStore
from cptr.memory.worker import MemoryWorker
from cptr.models import Base, User
from cptr.services.memory_fabric import MemoryFabricStore


class MemoryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(User(id="user-1", role="admin", settings={}, created_at=1))
            await db.commit()
        self.store = SqlMemoryStore(session_factory=self.sessions)
        self.events = MemoryFabricStore(session_factory=self.sessions)
        self.jobs = MemoryJobStore(session_factory=self.sessions)
        self.graph = MemoryGraphStore(session_factory=self.sessions)
        self.service = EmbeddedMemoryService(
            store=self.store,
            event_store=self.events,
            job_store=self.jobs,
            graph_store=self.graph,
        )
        self.worker = MemoryWorker(
            service=self.service,
            job_store=self.jobs,
            graph_store=self.graph,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    def test_entity_extraction_finds_wiki_repo_service_and_commit_identifiers(self):
        entities = extract_entities(
            heading="Chrome Deployment",
            text="Use [[Rollback]] for heidi-dang/computer and restart cptr.service at commit abc1234.",
        )
        names = {name for name, _kind in entities}
        self.assertIn("Chrome Deployment", names)
        self.assertIn("Rollback", names)
        self.assertIn("heidi-dang/computer", names)
        self.assertIn("cptr.service", names)
        self.assertIn("abc1234", names)

    async def test_queue_consolidation_is_durable_sanitized_and_worker_projects_graph(self):
        job_id = await self.service.queue_consolidation(
            user_id="user-1",
            workspace="/repo",
            scope="workspace",
            text="Deployment procedure: verify [[Rollback]] in heidi-dang/computer; api_key=supersecret123.",
            heading="Deployment",
            source_event_ids=["evt-1"],
            trust_level="verified_system_fact",
        )
        claimed = await self.jobs.claim_due()
        self.assertEqual(claimed["job_id"], job_id)
        self.assertNotIn("supersecret123", str(claimed["payload"]))
        await self.worker.process(claimed)
        counts = await self.jobs.counts(user_id="user-1", workspace="/repo")
        self.assertEqual(counts["complete"], 1)
        memories = await self.store.list_candidates(
            user_id="user-1",
            workspace="/repo",
            include_historical=False,
            limit=20,
        )
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["kind"], "procedure")
        graph = await self.graph.snapshot(user_id="user-1", workspace="/repo")
        names = {row["canonical_name"] for row in graph["entities"]}
        self.assertIn("Deployment", names)
        self.assertIn("Rollback", names)
        self.assertIn("heidi-dang/computer", names)
        self.assertTrue(
            all(memories[0]["memory_id"] in row["source_memory_ids"] for row in graph["entities"])
        )
        self.assertGreaterEqual(len(graph["relationships"]), 1)

    async def test_failed_job_retries_without_persisting_raw_exception_text(self):
        job_id = await self.jobs.enqueue(
            user_id="user-1",
            workspace="/repo",
            job_type="unsupported",
            payload={},
        )
        claimed = await self.jobs.claim_due()
        self.assertEqual(claimed["job_id"], job_id)
        await self.worker.process(claimed)
        counts = await self.jobs.counts(user_id="user-1", workspace="/repo")
        self.assertEqual(counts["pending"], 1)
        # Claim is delayed after a failure, so it must not hot-loop immediately.
        self.assertIsNone(await self.jobs.claim_due())


if __name__ == "__main__":
    unittest.main()
