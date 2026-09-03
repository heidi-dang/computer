import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cptr.memory.evaluation import MemoryEvaluationCase, MemoryEvaluationHarness


class MemoryEvaluationHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_perfect_retrieval_scores_one_without_leakage_or_staleness(self):
        result = SimpleNamespace(memory_id="mem-good", verification_stale=False)
        service = SimpleNamespace(search=AsyncMock(return_value=[result]))
        harness = MemoryEvaluationHarness(service)

        metrics = await harness.evaluate(
            user_id="user-1",
            workspace="/repo",
            cases=[
                MemoryEvaluationCase(
                    query="production database",
                    relevant_memory_ids=frozenset({"mem-good"}),
                    forbidden_memory_ids=frozenset({"mem-other"}),
                )
            ],
            k=1,
        )

        self.assertEqual(metrics["precision_at_k"], 1.0)
        self.assertEqual(metrics["recall_at_k"], 1.0)
        self.assertEqual(metrics["mrr"], 1.0)
        self.assertEqual(metrics["forbidden_hit_rate"], 0.0)
        self.assertEqual(metrics["stale_top_rate"], 0.0)
        self.assertEqual(metrics["quality_score"], 1.0)

    async def test_forbidden_retrieval_is_explicitly_penalized(self):
        result = SimpleNamespace(memory_id="mem-forbidden", verification_stale=False)
        service = SimpleNamespace(search=AsyncMock(return_value=[result]))
        harness = MemoryEvaluationHarness(service)

        metrics = await harness.evaluate(
            user_id="user-1",
            workspace="/repo",
            cases=[
                MemoryEvaluationCase(
                    query="private other project",
                    relevant_memory_ids=frozenset({"mem-good"}),
                    forbidden_memory_ids=frozenset({"mem-forbidden"}),
                )
            ],
            k=1,
        )

        self.assertEqual(metrics["forbidden_hit_rate"], 1.0)
        self.assertEqual(metrics["quality_score"], 0.0)


if __name__ == "__main__":
    unittest.main()
