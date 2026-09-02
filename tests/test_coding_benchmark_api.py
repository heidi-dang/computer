import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException


def request():
    return SimpleNamespace(
        headers={"Authorization": "Bearer token"}, cookies={}, client=None, state=SimpleNamespace()
    )


class CodingBenchmarkApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cptr.routers import benchmarks as benchmark_router

        self.router = benchmark_router

    def test_routes_expose_start_submit_get_and_leaderboard(self):
        paths = {route.path for route in self.router.router.routes if hasattr(route, "path")}
        self.assertIn("/api/control/v1/benchmarks/runs", paths)
        self.assertIn("/api/control/v1/benchmarks/runs/{run_id}", paths)
        self.assertIn("/api/control/v1/benchmarks/runs/{run_id}/submit", paths)
        self.assertIn("/api/control/v1/benchmarks/leaderboard", paths)

    async def test_start_forwards_authenticated_owner_model_and_suite(self):
        body = self.router.StartBenchmarkRequest(
            suite_id="cptr-python-core", model_reported="GPT-5.6 Sol"
        )
        expected = {"run_id": "bench_1", "status": "READY"}
        store = SimpleNamespace(start=AsyncMock(return_value=expected))
        with (
            patch.object(self.router, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(self.router, "coding_benchmark_store", store),
        ):
            result = await self.router.start_benchmark(request(), body)
        self.assertEqual(result, expected)
        store.start.assert_awaited_once_with(
            "user-1", model_reported="GPT-5.6 Sol", suite_id="cptr-python-core"
        )

    async def test_submit_is_owner_scoped_and_missing_run_maps_404(self):
        store = SimpleNamespace(submit=AsyncMock(side_effect=KeyError("benchmark run not found")))
        with (
            patch.object(self.router, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(self.router, "coding_benchmark_store", store),
        ):
            with self.assertRaises(HTTPException) as raised:
                await self.router.submit_benchmark(request(), "bench_missing")
        self.assertEqual(raised.exception.status_code, 404)
        store.submit.assert_awaited_once_with("user-1", "bench_missing")

    async def test_get_and_leaderboard_use_read_scope(self):
        run = {"run_id": "bench_1", "status": "COMPLETE", "score": 100}
        leaderboard = {"comparable": True, "models": []}
        store = SimpleNamespace(
            get=AsyncMock(return_value=run),
            leaderboard=AsyncMock(return_value=leaderboard),
        )
        auth = AsyncMock(return_value="user-1")
        with (
            patch.object(self.router, "_user", new=auth),
            patch.object(self.router, "coding_benchmark_store", store),
        ):
            self.assertEqual(await self.router.get_benchmark(request(), "bench_1"), run)
            self.assertEqual(
                await self.router.get_benchmark_leaderboard(request(), suite_id="cptr-python-core"),
                leaderboard,
            )
        self.assertEqual(auth.await_args_list[0].args[1], "task:read")
        self.assertEqual(auth.await_args_list[1].args[1], "task:read")
        store.leaderboard.assert_awaited_once_with("user-1", suite_id="cptr-python-core")

    async def test_unknown_suite_maps_to_422_without_internal_details(self):
        body = self.router.StartBenchmarkRequest(suite_id="unknown", model_reported=None)
        store = SimpleNamespace(start=AsyncMock(side_effect=ValueError("unknown benchmark suite")))
        with (
            patch.object(self.router, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(self.router, "coding_benchmark_store", store),
        ):
            with self.assertRaises(HTTPException) as raised:
                await self.router.start_benchmark(request(), body)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail, "unknown benchmark suite")


if __name__ == "__main__":
    unittest.main()
