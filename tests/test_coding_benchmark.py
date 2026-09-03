import importlib
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models.base import Base
from cptr.models.users import User


INTERVAL_SOLUTION = """def merge_intervals(intervals):
    normalized = []
    for start, end in intervals:
        if start > end:
            start, end = end, start
        normalized.append((start, end))
    normalized.sort()
    merged = []
    for start, end in normalized:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [tuple(item) for item in merged]
"""

TTL_SOLUTION = """from collections import OrderedDict

class TTLCache:
    def __init__(self, capacity, ttl_seconds):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._items = OrderedDict()

    def _purge(self, now):
        expired = [key for key, (_, expires) in self._items.items() if expires <= now]
        for key in expired:
            self._items.pop(key, None)

    def set(self, key, value, now):
        self._purge(now)
        self._items.pop(key, None)
        self._items[key] = (value, now + self.ttl_seconds)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def get(self, key, now, default=None):
        self._purge(now)
        if key not in self._items:
            return default
        value, expires = self._items.pop(key)
        self._items[key] = (value, expires)
        return value

    def delete(self, key):
        return self._items.pop(key, None) is not None
"""

RETRY_SOLUTION = """def retry_call(fn, *, attempts, retryable):
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            return fn()
        except retryable:
            if attempt + 1 >= attempts:
                raise
"""

GRADER_TAMPER_SOLUTION = """import __main__

def fake_score(case_id, weight, checks):
    return {
        "id": case_id,
        "passed": 1,
        "total": 1,
        "points": weight,
        "max_points": weight,
        "error_kinds": [],
    }

__main__.score_case = fake_score

def merge_intervals(intervals):
    return []
"""


class CodingBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            module = importlib.import_module("cptr.services.coding_benchmark")
            importlib.import_module("cptr.models.metrics")
        except ImportError as exc:
            self.fail(f"coding benchmark service is missing: {exc}")
        self.CodingBenchmarkStore = module.CodingBenchmarkStore
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'benchmark.db'}")

        @event.listens_for(self.engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.factory() as db:
            db.add(User(id="user-1", role="user", settings={}, created_at=1))
            await db.commit()
        self.store = self.CodingBenchmarkStore(session_factory=self.factory, data_dir=root)

    async def asyncTearDown(self):
        if hasattr(self, "engine"):
            await self.engine.dispose()
        if hasattr(self, "temp"):
            self.temp.cleanup()

    async def test_start_creates_isolated_workspace_without_exposing_hidden_seed(self):
        run = await self.store.start("user-1", model_reported="GPT-5.6 Sol")
        self.assertEqual(run["suite_id"], "cptr-python-core")
        self.assertEqual(run["suite_version"], "1")
        self.assertEqual(run["status"], "READY")
        self.assertTrue(run["workspace_id"])
        self.assertNotIn("grader_seed", run)
        self.assertNotIn("workspace_path", run)
        self.assertEqual(len(run["tasks"]), 3)

        workspace_path = await self.store.workspace_path("user-1", run["run_id"])
        self.assertIsNotNone(workspace_path)
        root = Path(workspace_path)
        self.assertTrue((root / "README.md").is_file())
        self.assertTrue((root / "intervals.py").is_file())
        self.assertTrue((root / "ttl_cache.py").is_file())
        self.assertTrue((root / "retry_policy.py").is_file())
        self.assertFalse((root / "grader.py").exists())
        self.assertNotIn("grader_seed", (root / "README.md").read_text(encoding="utf-8"))

    async def test_correct_solution_scores_100_and_submit_is_idempotent(self):
        run = await self.store.start("user-1", model_reported="GPT-5.6 Sol")
        workspace_path = Path(await self.store.workspace_path("user-1", run["run_id"]))
        (workspace_path / "intervals.py").write_text(INTERVAL_SOLUTION, encoding="utf-8")
        (workspace_path / "ttl_cache.py").write_text(TTL_SOLUTION, encoding="utf-8")
        (workspace_path / "retry_policy.py").write_text(RETRY_SOLUTION, encoding="utf-8")

        result = await self.store.submit("user-1", run["run_id"])
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["max_score"], 100)
        self.assertTrue(result["comparable"])
        self.assertEqual(len(result["case_results"]), 3)
        self.assertTrue(all(case["passed"] == case["total"] for case in result["case_results"]))
        self.assertIn("grader_seed", result)

        repeated = await self.store.submit("user-1", run["run_id"])
        self.assertEqual(repeated["score"], 100)
        self.assertEqual(repeated["completed_at_ms"], result["completed_at_ms"])

    async def test_incorrect_solution_scores_below_100_and_leaderboard_is_standardized_only(self):
        bad = await self.store.start("user-1", model_reported="GPT-5.6 Sol")
        bad_result = await self.store.submit("user-1", bad["run_id"])
        self.assertLess(bad_result["score"], 100)

        good = await self.store.start("user-1", model_reported="GPT-5.6 Sol")
        path = Path(await self.store.workspace_path("user-1", good["run_id"]))
        (path / "intervals.py").write_text(INTERVAL_SOLUTION, encoding="utf-8")
        (path / "ttl_cache.py").write_text(TTL_SOLUTION, encoding="utf-8")
        (path / "retry_policy.py").write_text(RETRY_SOLUTION, encoding="utf-8")
        await self.store.submit("user-1", good["run_id"])

        leaderboard = await self.store.leaderboard("user-1")
        self.assertTrue(leaderboard["comparable"])
        self.assertEqual(leaderboard["suite_id"], "cptr-python-core")
        self.assertEqual(len(leaderboard["models"]), 1)
        model = leaderboard["models"][0]
        self.assertEqual(model["model_canonical"], "gpt-5.6-sol")
        self.assertEqual(model["attempts"], 2)
        self.assertEqual(model["best_score"], 100)
        self.assertGreater(model["average_score"], 0)
        self.assertEqual(model["perfect_runs"], 1)
        self.assertNotIn("operational_score", model)

    async def test_student_code_cannot_tamper_with_grader_process(self):
        run = await self.store.start("user-1", model_reported="GPT-5.6 Sol")
        path = Path(await self.store.workspace_path("user-1", run["run_id"]))
        (path / "intervals.py").write_text(GRADER_TAMPER_SOLUTION, encoding="utf-8")

        result = await self.store.submit("user-1", run["run_id"])

        self.assertEqual(result["status"], "COMPLETE")
        self.assertLess(result["score"], 100)
        self.assertTrue(any(case["points"] < case["max_points"] for case in result["case_results"]))


if __name__ == "__main__":
    unittest.main()
