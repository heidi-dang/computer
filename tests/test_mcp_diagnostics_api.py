import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from cptr.routers import mcp as mcp_router
from cptr.routers.gateway import DEFAULT_CONTROL_SCOPES
from cptr.services.mcp_activity import McpActivityEvent, McpActivityStore
from cptr.services.mcp_diagnostics import (
    McpBackendMetricsSample,
    McpDiagnosticsBatch,
    McpDiagnosticsStore,
    McpFailureDiagnostic,
    McpLatencySample,
    McpUsageDiagnostic,
)
from cptr.services.mcp_traffic import McpTrafficEvent, McpTrafficStore

BASE_TS = 1_788_000_000_000


def route_request(*, disconnected: bool = False):
    return SimpleNamespace(
        headers={"Authorization": "Bearer test-token"},
        cookies={},
        client=None,
        state=SimpleNamespace(),
        is_disconnected=AsyncMock(return_value=disconnected),
    )


def latency(event_id: str = "latency-001") -> McpLatencySample:
    return McpLatencySample(
        event_id=event_id,
        timestamp_ms=BASE_TS,
        request_id="request-1",
        correlation_id="corr-1",
        edge_id="cptr-mcp-cptr-backend",
        metric_type="backend_api_rtt",
        duration_ms=25,
        status="ok",
    )


def failure(diagnostic_id: str = "failure-001") -> McpFailureDiagnostic:
    return McpFailureDiagnostic(
        diagnostic_id=diagnostic_id,
        request_id="request-1",
        correlation_id="corr-1",
        session_id="session-1",
        client_id="chatgpt",
        method="tools/call",
        tool_name="cptr_list_workspaces",
        stage="cptr_backend",
        error_code="backend_unavailable",
        http_status=503,
        retryable=True,
        started_at_ms=BASE_TS,
        completed_at_ms=BASE_TS + 25,
        duration_ms=25,
        request_bytes=120,
        response_bytes=64,
        summary="Backend unavailable.",
    )


def usage(event_id: str = "usage-0001") -> McpUsageDiagnostic:
    return McpUsageDiagnostic(
        event_id=event_id,
        timestamp_ms=BASE_TS + 30,
        request_id="request-1",
        correlation_id="corr-1",
        session_id="session-1",
        client_id="chatgpt",
        model_reported="GPT-5.6 Sol",
        model_canonical="gpt-5.6-sol",
        model_source="self_reported",
        tool_name="cptr_list_workspaces",
        input_tokens_estimated=100,
        output_tokens_estimated=20,
        cached_input_tokens_estimated=None,
        estimator_method="output=o200k_base:fallback;input=o200k_base:fallback",
        estimator_exact_for_model=False,
        status="complete",
    )


class McpDiagnosticsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingestion_requires_dedicated_diagnostics_scope(self):
        self.assertTrue(hasattr(mcp_router, "ingest_mcp_diagnostics"))
        store = McpDiagnosticsStore(
            max_latency_samples_per_edge=8,
            max_failures=8,
            max_system_samples=4,
            subscriber_queue_size=2,
        )
        auth = AsyncMock(return_value="user-1")
        body = McpDiagnosticsBatch(events=[latency()])
        with (
            patch.object(mcp_router, "mcp_diagnostics_store", store),
            patch.object(mcp_router, "authenticate_control_request", auth),
        ):
            result = await mcp_router.ingest_mcp_diagnostics(route_request(), body)
        auth.assert_awaited_once()
        self.assertEqual(auth.await_args.args[1], "mcp:diagnostics:write")
        self.assertEqual(result, {"accepted": 1, "duplicates": 0, "dropped": 0})

    async def test_missing_scope_maps_403_and_invalid_token_maps_401(self):
        self.assertTrue(hasattr(mcp_router, "ingest_mcp_diagnostics"))
        body = McpDiagnosticsBatch(events=[latency()])
        for message, expected in (
            ("missing required scope: mcp:diagnostics:write", 403),
            ("invalid control-plane bearer token", 401),
        ):
            with self.subTest(message=message):
                with patch.object(
                    mcp_router,
                    "authenticate_control_request",
                    AsyncMock(side_effect=PermissionError(message)),
                ):
                    with self.assertRaises(HTTPException) as raised:
                        await mcp_router.ingest_mcp_diagnostics(route_request(), body)
                self.assertEqual(raised.exception.status_code, expected)

    async def test_snapshot_and_stream_require_admin_and_start_sampler(self):
        self.assertTrue(hasattr(mcp_router, "get_mcp_diagnostics_snapshot"))
        self.assertTrue(hasattr(mcp_router, "stream_mcp_diagnostics"))
        store = McpDiagnosticsStore(
            max_latency_samples_per_edge=8,
            max_failures=8,
            max_system_samples=4,
            subscriber_queue_size=2,
        )
        await store.ingest([latency()])
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        sampler = SimpleNamespace(ensure_started=AsyncMock())
        durable = SimpleNamespace(summary=AsyncMock(return_value={"week": {}, "month": {}}))
        request = route_request()
        with (
            patch.object(mcp_router, "mcp_diagnostics_store", store),
            patch.object(mcp_router, "mcp_metrics_sampler", sampler),
            patch.object(mcp_router, "mcp_usage_store", durable),
            patch.object(mcp_router, "require_admin", admin),
        ):
            snapshot = await mcp_router.get_mcp_diagnostics_snapshot(request)
            response = await mcp_router.stream_mcp_diagnostics(request)
            self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 0)
            iterator = response.body_iterator.__aiter__()
            first = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 1)
            await iterator.aclose()
        self.assertIn("cptr-mcp-cptr-backend", snapshot["latency"])
        self.assertIn("event: snapshot", first)
        self.assertEqual(admin.call_count, 2)
        self.assertEqual(sampler.ensure_started.await_count, 2)
        self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 0)

    async def test_sse_emits_named_latency_failure_usage_and_system_events(self):
        self.assertTrue(hasattr(mcp_router, "stream_mcp_diagnostics"))
        store = McpDiagnosticsStore(
            max_latency_samples_per_edge=8,
            max_failures=8,
            max_system_samples=4,
            subscriber_queue_size=4,
        )
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        sampler = SimpleNamespace(ensure_started=AsyncMock())
        durable = SimpleNamespace(summary=AsyncMock(return_value={"week": {}, "month": {}}))
        request = route_request()
        with (
            patch.object(mcp_router, "mcp_diagnostics_store", store),
            patch.object(mcp_router, "mcp_metrics_sampler", sampler),
            patch.object(mcp_router, "mcp_usage_store", durable),
            patch.object(mcp_router, "require_admin", admin),
        ):
            response = await mcp_router.stream_mcp_diagnostics(request)
            iterator = response.body_iterator.__aiter__()
            await asyncio.wait_for(iterator.__anext__(), timeout=1)

            await store.ingest([latency("latency-002")])
            latency_event = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertIn("event: latency", latency_event)

            await store.ingest([failure("failure-002")])
            failure_event = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertIn("event: failure", failure_event)

            await store.ingest([usage("usage-0002")])
            usage_event = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertIn("event: usage", usage_event)
            self.assertIn('"model_reported":"GPT-5.6 Sol"', usage_event)

            await store.record_system_sample(
                McpBackendMetricsSample(timestamp_ms=BASE_TS + 50, cpu_count=8)
            )
            system_event = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertIn("event: system", system_event)
            await iterator.aclose()

    async def test_exact_diagnostics_post_path_bypasses_cookie_middleware_only(self):
        from cptr import app as cptr_app

        sentinel = object()
        call_next = AsyncMock(return_value=sentinel)
        post_request = SimpleNamespace(
            url=SimpleNamespace(path="/api/mcp/diagnostics/events"), method="POST"
        )
        result = await cptr_app.auth_middleware(post_request, call_next)
        self.assertIs(result, sentinel)
        call_next.assert_awaited_once_with(post_request)

        get_request = SimpleNamespace(
            url=SimpleNamespace(path="/api/mcp/diagnostics/events"),
            method="GET",
            client=None,
            cookies={},
            headers={},
        )
        with (
            patch.object(cptr_app, "check_access", return_value=None),
            patch.object(cptr_app, "load_config", return_value={"auth": {}}),
        ):
            response = await cptr_app.auth_middleware(get_request, AsyncMock())
        self.assertEqual(response.status_code, 401)

    def test_default_plugin_credentials_include_diagnostics_scope_once(self):
        self.assertIn("mcp:diagnostics:write", DEFAULT_CONTROL_SCOPES)
        self.assertEqual(DEFAULT_CONTROL_SCOPES.count("mcp:diagnostics:write"), 1)

    async def test_correlation_crosses_channels_without_privacy_crossover(self):
        traffic_store = McpTrafficStore(max_events=8, max_sessions=4, subscriber_queue_size=2)
        activity_store = McpActivityStore(max_events=8, subscriber_queue_size=2)
        diagnostics_store = McpDiagnosticsStore(
            max_latency_samples_per_edge=8,
            max_failures=8,
            max_system_samples=4,
            subscriber_queue_size=2,
        )
        traffic = McpTrafficEvent(
            version=1,
            event_id="traffic-001",
            sequence=1,
            event_type="request_started",
            timestamp_ms=BASE_TS,
            session_id="session-1",
            client={"id": "chatgpt", "label": "ChatGPT", "version": "1.0"},
            request_id="request-1",
            correlation_id="corr-1",
            method="tools/call",
            tool_name="cptr_list_workspaces",
            status="started",
            duration_ms=None,
            request_bytes=120,
            response_bytes=None,
            error_code=None,
        )
        activity = McpActivityEvent(
            version=1,
            event_id="activity-001",
            sequence=1,
            timestamp_ms=BASE_TS,
            client={"id": "chatgpt", "label": "ChatGPT", "version": "1.0"},
            session_id="session-1",
            request_id="request-1",
            correlation_id="corr-1",
            tool_name="cptr_list_workspaces",
            title="List workspaces",
            phase="started",
            summary="Working: List workspaces.",
            arguments_json='{"include_unavailable":false}',
            result_json=None,
            error_json=None,
            duration_ms=None,
        )
        await traffic_store.ingest([traffic])
        await activity_store.ingest([activity])
        await diagnostics_store.ingest([failure()])

        traffic_snapshot = await traffic_store.snapshot()
        activity_snapshot = await activity_store.snapshot()
        diagnostics_snapshot = await diagnostics_store.snapshot()
        self.assertIn("corr-1", str(traffic_snapshot))
        self.assertIn("corr-1", str(activity_snapshot))
        self.assertIn("corr-1", str(diagnostics_snapshot))
        self.assertNotIn("arguments_json", str(traffic_snapshot))
        self.assertNotIn("result_json", str(traffic_snapshot))
        self.assertNotIn("arguments_json", str(diagnostics_snapshot))
        self.assertNotIn("result_json", str(diagnostics_snapshot))


if __name__ == "__main__":
    unittest.main()
