import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from cptr.routers import mcp as mcp_router
from cptr.routers.gateway import DEFAULT_CONTROL_SCOPES
from cptr.services.mcp_activity import McpActivityBatch, McpActivityEvent, McpActivityStore
from cptr.services.mcp_traffic import McpTrafficEvent, McpTrafficStore

BASE_TS = 1_788_000_000_000


def activity_event(event_id: str, phase: str = "started") -> McpActivityEvent:
    return McpActivityEvent(
        version=1,
        event_id=event_id,
        sequence=1,
        timestamp_ms=BASE_TS,
        client={"id": "chatgpt", "label": "ChatGPT", "version": "1.0"},
        session_id="session-1",
        request_id="request-1",
        tool_name="cptr_list_workspaces",
        title="List workspaces",
        phase=phase,
        summary="Working: List workspaces.",
        arguments_json='{"include_unavailable":false}' if phase == "started" else None,
        result_json='{"workspaces":[]}' if phase == "complete" else None,
        error_json=None,
        duration_ms=12 if phase != "started" else None,
    )


def traffic_event(event_id: str) -> McpTrafficEvent:
    return McpTrafficEvent(
        version=1,
        event_id=event_id,
        sequence=1,
        event_type="request_started",
        timestamp_ms=BASE_TS,
        session_id="session-1",
        client={"id": "chatgpt", "label": "ChatGPT", "version": "1.0"},
        request_id="request-1",
        method="tools/call",
        tool_name="cptr_list_workspaces",
        status="started",
        duration_ms=None,
        request_bytes=120,
        response_bytes=None,
        error_code=None,
    )


def route_request(*, disconnected: bool = False):
    return SimpleNamespace(
        headers={"Authorization": "Bearer test-token"},
        cookies={},
        client=None,
        state=SimpleNamespace(),
        is_disconnected=AsyncMock(return_value=disconnected),
    )


class McpActivityApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingestion_requires_dedicated_activity_scope(self):
        store = McpActivityStore(max_events=8, subscriber_queue_size=2)
        auth = AsyncMock(return_value="user-1")
        body = McpActivityBatch(events=[activity_event("event-001")])
        with (
            patch.object(mcp_router, "mcp_activity_store", store),
            patch.object(mcp_router, "require_control_user", auth),
        ):
            result = await mcp_router.ingest_mcp_activity(route_request(), body)
        auth.assert_awaited_once()
        self.assertEqual(auth.await_args.args[1], "mcp:activity:write")
        self.assertEqual(result, {"accepted": 1, "duplicates": 0, "dropped": 0})

    async def test_missing_scope_maps_403_and_invalid_token_maps_401(self):
        body = McpActivityBatch(events=[activity_event("event-001")])
        for message, expected in (
            ("missing required scope: mcp:activity:write", 403),
            ("invalid control-plane bearer token", 401),
        ):
            with self.subTest(message=message):
                with patch(
                    "cptr.services.control_auth.authenticate_control_request",
                    new=AsyncMock(side_effect=PermissionError(message)),
                ):
                    with self.assertRaises(HTTPException) as raised:
                        await mcp_router.ingest_mcp_activity(route_request(), body)
                self.assertEqual(raised.exception.status_code, expected)

    async def test_snapshot_and_stream_require_admin(self):
        store = McpActivityStore(max_events=8, subscriber_queue_size=2)
        await store.ingest([activity_event("event-001")])
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        request = route_request()
        with (
            patch.object(mcp_router, "mcp_activity_store", store),
            patch.object(mcp_router, "require_admin", admin),
        ):
            snapshot = await mcp_router.get_mcp_activity_snapshot(request)
            response = await mcp_router.stream_mcp_activity(request)
            iterator = response.body_iterator.__aiter__()
            first = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            await iterator.aclose()
        self.assertEqual(snapshot["sequence"], 1)
        self.assertIn("event: snapshot", first)
        self.assertEqual(admin.call_count, 2)

    async def test_sse_subscribes_during_iteration_and_emits_incremental_activity(self):
        store = McpActivityStore(max_events=8, subscriber_queue_size=2)
        request = route_request()
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        with (
            patch.object(mcp_router, "mcp_activity_store", store),
            patch.object(mcp_router, "require_admin", admin),
        ):
            response = await mcp_router.stream_mcp_activity(request)
            self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 0)
            iterator = response.body_iterator.__aiter__()
            first = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertIn("event: snapshot", first)
            self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 1)
            await store.ingest([activity_event("event-002", "complete")])
            second = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertIn("event: activity", second)
            self.assertIn('"ingestion_sequence":1', second)
            self.assertIn("result_json", second)
            await iterator.aclose()
        self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 0)

    async def test_traffic_contract_remains_metadata_only_while_activity_has_payloads(self):
        traffic_store = McpTrafficStore(max_events=8, max_sessions=4, subscriber_queue_size=2)
        activity_store = McpActivityStore(max_events=8, subscriber_queue_size=2)
        await traffic_store.ingest([traffic_event("traffic-001")])
        await activity_store.ingest([activity_event("activity-001")])
        traffic_snapshot = await traffic_store.snapshot()
        activity_snapshot = await activity_store.snapshot()
        self.assertNotIn("arguments_json", str(traffic_snapshot))
        self.assertNotIn("result_json", str(traffic_snapshot))
        self.assertIn("arguments_json", str(activity_snapshot))

    async def test_exact_activity_post_path_bypasses_cookie_middleware_only(self):
        from cptr import app as cptr_app

        sentinel = object()
        call_next = AsyncMock(return_value=sentinel)
        post_request = SimpleNamespace(
            url=SimpleNamespace(path="/api/mcp/activity/events"), method="POST"
        )
        result = await cptr_app.auth_middleware(post_request, call_next)
        self.assertIs(result, sentinel)
        call_next.assert_awaited_once_with(post_request)

        get_request = SimpleNamespace(
            url=SimpleNamespace(path="/api/mcp/activity/events"),
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

    def test_default_plugin_credentials_include_activity_scope_once(self):
        self.assertIn("mcp:activity:write", DEFAULT_CONTROL_SCOPES)
        self.assertEqual(DEFAULT_CONTROL_SCOPES.count("mcp:activity:write"), 1)


if __name__ == "__main__":
    unittest.main()
