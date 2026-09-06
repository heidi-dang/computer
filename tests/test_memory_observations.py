import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.memory.domain import MemoryQuery
from cptr.memory.graph import MemoryGraphStore
from cptr.memory.jobs import MemoryJobStore
from cptr.memory.observations import observe_execution_outcome
from cptr.memory.service import EmbeddedMemoryService
from cptr.memory.store import SqlMemoryStore
from cptr.memory.worker import MemoryWorker
from cptr.models import Base, User
from cptr.services.memory_fabric import MemoryFabricStore


class MemoryObservationTests(unittest.IsolatedAsyncioTestCase):
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

    async def _process_one(self):
        job = await self.jobs.claim_due()
        self.assertIsNotNone(job)
        await self.worker.process(job)

    async def test_verified_test_outcome_becomes_searchable_procedure(self):
        job_id = await observe_execution_outcome(
            user_id="user-1",
            workspace="/repo",
            action="test",
            command="python -m pytest tests/test_memory_core.py",
            status="COMPLETE",
            exit_code=0,
            output="16 passed",
            metadata={"target": "python_pytest", "transport": "local-test"},
            service=self.service,
        )
        self.assertIsNotNone(job_id)
        await self._process_one()

        results = await self.service.search(
            MemoryQuery(
                user_id="user-1",
                workspace="/repo",
                query="pytest validation procedure",
                limit=8,
            )
        )
        self.assertTrue(results)
        self.assertEqual(results[0].kind, "procedure")
        self.assertIn("python_pytest", results[0].canonical_text)
        self.assertEqual(results[0].trust_level, "verified_system_fact")

    async def test_low_value_read_only_command_is_not_promoted(self):
        job_id = await observe_execution_outcome(
            user_id="user-1",
            workspace="/repo",
            action="command",
            command="ls -la",
            status="COMPLETE",
            exit_code=0,
            output="README.md",
            service=self.service,
        )
        self.assertIsNone(job_id)
        counts = await self.jobs.counts(user_id="user-1", workspace="/repo")
        self.assertEqual(counts["pending"], 0)

    async def test_failure_memory_is_bounded_and_secret_redacted(self):
        secret = "supersecretvalue123"
        job_id = await observe_execution_outcome(
            user_id="user-1",
            workspace="/repo",
            action="command",
            command=f"npm test --token={secret}",
            status="COMPLETE",
            exit_code=1,
            output=f"Authorization: Bearer {secret}\nERROR test suite failed",
            service=self.service,
        )
        self.assertIsNotNone(job_id)
        await self._process_one()

        rows = await self.store.list_candidates(
            user_id="user-1",
            workspace="/repo",
            include_historical=False,
            limit=20,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "failure")
        self.assertNotIn(secret, rows[0]["canonical_text"])
        self.assertIn("[REDACTED]", rows[0]["canonical_text"])
        self.assertIn("test suite failed", rows[0]["canonical_text"])


if __name__ == "__main__":
    unittest.main()
