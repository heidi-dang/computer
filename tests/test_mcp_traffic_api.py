import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from cptr.routers import mcp as mcp_router
from cptr.routers.gateway import DEFAULT_CONTROL_SCOPES
from cptr.services.mcp_traffic import McpTrafficBatch, McpTrafficEvent, McpTrafficStore


BASE_TS = 1_788_000_000_000


def traffic_event(event_id: str, event_type: str, *, status: str = "started") -> McpTrafficEvent:
    return McpTrafficEvent(
        version=1,
        event_id=event_id,
        sequence=1,
        event_type=event_type,
        timestamp_ms=BASE_TS,
        session_id="session-1",
        client={"id": "chatgpt", "label": "ChatGPT", "version": "1.0"},
        request_id="request-1",
        method="tools/call",
        tool_name="cptr_list_workspaces",
        status=status,
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


class McpTrafficApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingestion_requires_dedicated_bearer_scope(self):
        store = McpTrafficStore(max_events=8, max_sessions=4, subscriber_queue_size=2)
        auth = AsyncMock(return_value="user-1")
        body = McpTrafficBatch(events=[traffic_event("event-001", "request_started")])

        with (
            patch.object(mcp_router, "mcp_traffic_store", store),
            patch.object(mcp_router, "authenticate_control_request", auth),
        ):
            result = await mcp_router.ingest_mcp_traffic(route_request(), body)

        auth.assert_awaited_once()
        self.assertEqual(auth.await_args.args[1], "mcp:traffic:write")
        self.assertEqual(result, {"accepted": 1, "duplicates": 0, "dropped": 0})

    async def test_missing_scope_maps_to_403_and_invalid_token_maps_to_401(self):
        body = McpTrafficBatch(events=[traffic_event("event-001", "request_started")])
        for message, expected_status in (
            ("missing required scope: mcp:traffic:write", 403),
            ("invalid control-plane bearer token", 401),
        ):
            with self.subTest(message=message):
                with patch.object(
                    mcp_router,
                    "authenticate_control_request",
                    AsyncMock(side_effect=PermissionError(message)),
                ):
                    with self.assertRaises(HTTPException) as raised:
                        await mcp_router.ingest_mcp_traffic(route_request(), body)
                self.assertEqual(raised.exception.status_code, expected_status)

    async def test_snapshot_requires_admin_and_returns_safe_projection(self):
        store = McpTrafficStore(max_events=8, max_sessions=4, subscriber_queue_size=2)
        await store.ingest([traffic_event("event-001", "request_started")])
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        request = route_request()

        with (
            patch.object(mcp_router, "mcp_traffic_store", store),
            patch.object(mcp_router, "require_admin", admin),
        ):
            snapshot = await mcp_router.get_mcp_traffic_snapshot(request)

        admin.assert_called_once_with(request)
        encoded = str(snapshot).lower()
        self.assertIn("chatgpt", encoded)
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("arguments", encoded)
        self.assertNotIn("result", encoded)

    async def test_sse_emits_snapshot_then_new_traffic_event(self):
        store = McpTrafficStore(max_events=8, max_sessions=4, subscriber_queue_size=2)
        await store.ingest([traffic_event("event-001", "request_started")])
        request = route_request()
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))

        with (
            patch.object(mcp_router, "mcp_traffic_store", store),
            patch.object(mcp_router, "require_admin", admin),
        ):
            response = await mcp_router.stream_mcp_traffic(request)
            iterator = response.body_iterator.__aiter__()
            first = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertIn("event: snapshot", first)
            self.assertIn('"sequence":1', first)

            await store.ingest(
                [
                    traffic_event(
                        "event-002",
                        "request_finished",
                        status="complete",
                    )
                ]
            )
            second = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertIn("event: traffic", second)
            self.assertIn('"ingestion_sequence":2', second)
            self.assertNotIn("arguments", second.lower())
            self.assertNotIn("result", second.lower())
            await iterator.aclose()

        admin.assert_called_once_with(request)
        self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 0)

    async def test_exact_post_ingestion_path_bypasses_cookie_middleware_only(self):
        from cptr import app as cptr_app

        sentinel = object()
        call_next = AsyncMock(return_value=sentinel)
        post_request = SimpleNamespace(
            url=SimpleNamespace(path="/api/mcp/traffic/events"),
            method="POST",
        )
        result = await cptr_app.auth_middleware(post_request, call_next)
        self.assertIs(result, sentinel)
        call_next.assert_awaited_once_with(post_request)

        get_request = SimpleNamespace(
            url=SimpleNamespace(path="/api/mcp/traffic/events"),
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

    def test_default_plugin_credentials_include_traffic_scope(self):
        self.assertIn("mcp:traffic:write", DEFAULT_CONTROL_SCOPES)
        self.assertEqual(DEFAULT_CONTROL_SCOPES.count("mcp:traffic:write"), 1)


if __name__ == "__main__":
    unittest.main()
