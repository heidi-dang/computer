import asyncio
import unittest

from cptr.flowdeck.parallel import (
    BuildNodeStatus,
    ParallelBuildNode,
    ParallelBuildPlan,
    ParallelBuildPlanError,
    ParallelBuildState,
    overlapping_parallel_nodes,
    ready_parallel_nodes,
    run_parallel_build_batch,
    validate_parallel_build_plan,
)


class ParallelBuildTests(unittest.IsolatedAsyncioTestCase):
    def test_dag_requires_isolation_and_rejects_cycles(self):
        with self.assertRaises(ParallelBuildPlanError):
            validate_parallel_build_plan(
                ParallelBuildPlan((ParallelBuildNode("write", mutation=True),))
            )
        with self.assertRaises(ParallelBuildPlanError):
            validate_parallel_build_plan(
                ParallelBuildPlan(
                    (
                        ParallelBuildNode("a", dependencies=("b",)),
                        ParallelBuildNode("b", dependencies=("a",)),
                    )
                )
            )

    def test_ready_nodes_and_overlap_detection_are_deterministic(self):
        plan = ParallelBuildPlan(
            (
                ParallelBuildNode("read-a"),
                ParallelBuildNode("read-b"),
                ParallelBuildNode(
                    "write-a",
                    dependencies=("read-a",),
                    mutation=True,
                    worktree="/tmp/a",
                    common_base="base",
                    overlap_paths=("src/shared.py",),
                ),
                ParallelBuildNode(
                    "write-b",
                    dependencies=("read-b",),
                    mutation=True,
                    worktree="/tmp/b",
                    common_base="base",
                    overlap_paths=("src/shared.py",),
                ),
            )
        )
        validate_parallel_build_plan(plan)
        state = ParallelBuildState(status={"read-a": BuildNodeStatus.SUCCEEDED})
        self.assertEqual([node.key for node in ready_parallel_nodes(plan, state)], ["read-b", "write-a"])
        self.assertEqual(overlapping_parallel_nodes(plan), (("write-a", "write-b"),))

    async def test_batch_runs_concurrently_and_cancels_future_work(self):
        started = []

        async def execute(node):
            started.append(node.key)
            await asyncio.sleep(0)
            return node.key

        state = await run_parallel_build_batch(
            (ParallelBuildNode("a"), ParallelBuildNode("b")),
            state=ParallelBuildState(),
            execute=execute,
            max_concurrency=2,
        )
        self.assertEqual(set(started), {"a", "b"})
        self.assertEqual(state.status["a"], BuildNodeStatus.SUCCEEDED)