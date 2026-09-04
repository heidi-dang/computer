import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cptr.routers import mcp as mcp_router
from cptr.services.mcp_diagnostics import (
    McpDiagnosticsBatch,
    McpDiagnosticsStore,
    McpUsageDiagnostic,
)

BASE_TS = 1_788_000_000_000


def request():
    return SimpleNamespace(
        headers={"Authorization": "Bearer test-token"},
        cookies={},
        client=None,
        state=SimpleNamespace(),
        is_disconnected=AsyncMock(return_value=False),
    )


def usage(event_id: str) -> McpUsageDiagnostic:
    return McpUsageDiagnostic(
        event_id=event_id,
        timestamp_ms=BASE_TS,
        request_id=f"req-{event_id}",
        correlation_id=f"corr-{event_id}",
        session_id="session-1",
        client_id="chatgpt",
        model_reported="GPT-5.6 Sol",
        model_canonical="gpt-5.6-sol",
        model_source="self_reported",
        tool_name="cptr_code_read_file",
        input_tokens_estimated=100,
        output_tokens_estimated=20,
        cached_input_tokens_estimated=None,
        estimator_method="output=o200k_base:fallback;input=o200k_base:fallback",
        estimator_exact_for_model=False,
        status="complete",
    )


class McpDurableUsageApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingest_persists_before_live_store_and_filters_restart_duplicate(self):
        self.assertTrue(
            hasattr(mcp_router, "mcp_usage_store"), "router must own durable usage store"
        )
        store = McpDiagnosticsStore(
            max_latency_samples_per_edge=4,
            max_failures=4,
            max_system_samples=2,
            max_usage=4,
            subscriber_queue_size=2,
        )
        durable = SimpleNamespace(ingest=AsyncMock(return_value=set()))
        auth = AsyncMock(return_value="user-1")
        with (
            patch.object(mcp_router, "mcp_diagnostics_store", store),
            patch.object(mcp_router, "mcp_usage_store", durable),
            patch.object(mcp_router, "require_control_user", auth),
        ):
            result = await mcp_router.ingest_mcp_diagnostics(
                request(), McpDiagnosticsBatch(events=[usage("usage-replay-1")])
            )

        durable.ingest.assert_awaited_once()
        self.assertEqual(result, {"accepted": 0, "duplicates": 1, "dropped": 0})
        self.assertEqual((await store.snapshot())["usage"], [])

    async def test_snapshot_includes_database_backed_usage_periods(self):
        self.assertTrue(
            hasattr(mcp_router, "mcp_usage_store"), "router must own durable usage store"
        )
        periods = {
            "week": {"requests": 2, "total_tokens_estimated": 120},
            "month": {"requests": 3, "total_tokens_estimated": 240},
        }
        durable = SimpleNamespace(summary=AsyncMock(return_value=periods))
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        sampler = SimpleNamespace(ensure_started=AsyncMock())
        with (
            patch.object(mcp_router, "mcp_usage_store", durable),
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "mcp_metrics_sampler", sampler),
        ):
            snapshot = await mcp_router.get_mcp_diagnostics_snapshot(request())

        self.assertEqual(snapshot["usage_periods"], periods)
        durable.summary.assert_awaited_once_with("admin-1")

    async def test_benchmark_leaderboard_endpoint_is_admin_scoped_and_comparable(self):
        self.assertTrue(
            hasattr(mcp_router, "get_mcp_benchmark_leaderboard"),
            "MCP router must expose the standardized benchmark leaderboard",
        )
        payload = {"comparable": True, "suite_id": "cptr-python-core", "models": []}
        benchmark = SimpleNamespace(leaderboard=AsyncMock(return_value=payload))
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        with (
            patch.object(mcp_router, "coding_benchmark_store", benchmark),
            patch.object(mcp_router, "require_admin", admin),
        ):
            result = await mcp_router.get_mcp_benchmark_leaderboard(
                request(), suite_id="cptr-python-core"
            )
        self.assertEqual(result, payload)
        benchmark.leaderboard.assert_awaited_once_with("admin-1", suite_id="cptr-python-core")

    async def test_engineering_sessions_endpoint_is_admin_scoped_and_non_comparable(self):
        self.assertTrue(
            hasattr(mcp_router, "get_mcp_engineering_sessions"),
            "MCP router must expose observed engineering sessions",
        )
        payload = {"comparable": False, "sessions": []}
        durable = SimpleNamespace(engineering_sessions=AsyncMock(return_value=payload))
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        with (
            patch.object(mcp_router, "mcp_usage_store", durable),
            patch.object(mcp_router, "require_admin", admin),
        ):
            result = await mcp_router.get_mcp_engineering_sessions(request(), limit=25)
        self.assertEqual(result, payload)
        durable.engineering_sessions.assert_awaited_once_with("admin-1", limit=25)


if __name__ == "__main__":
    unittest.main()
