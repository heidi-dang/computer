import unittest

from pydantic import ValidationError

from cptr.services.mcp_traffic import McpTrafficEvent, McpTrafficStore


BASE_TS = 1_788_000_000_000


def traffic_event(
    event_id: str,
    event_type: str,
    *,
    session_id: str | None = "session-1",
    request_id: str | None = "request-1",
    status: str = "started",
    client_id: str = "chatgpt",
    label: str = "ChatGPT",
    timestamp_ms: int = BASE_TS,
    tool_name: str | None = "cptr_list_workspaces",
    error_code: str | None = None,
    request_bytes: int | None = 120,
    response_bytes: int | None = None,
) -> McpTrafficEvent:
    return McpTrafficEvent(
        version=1,
        event_id=event_id,
        sequence=1,
        event_type=event_type,
        timestamp_ms=timestamp_ms,
        session_id=session_id,
        client={"id": client_id, "label": label, "version": "1.0"},
        request_id=request_id,
        method="tools/call",
        tool_name=tool_name,
        status=status,
        duration_ms=None,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        error_code=error_code,
    )


class McpTrafficStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_event_id_is_ignored(self):
        store = McpTrafficStore(max_events=8, max_sessions=4, subscriber_queue_size=2)
        first = traffic_event("event-001", "request_started")

        result = await store.ingest([first, first])
        snapshot = await store.snapshot()

        self.assertEqual(result, {"accepted": 1, "duplicates": 1, "dropped": 0})
        self.assertEqual(len(snapshot["events"]), 1)
        self.assertEqual(snapshot["sequence"], 1)
        self.assertEqual(snapshot["events"][0]["ingestion_sequence"], 1)

    async def test_recent_event_ring_is_bounded_newest_last(self):
        store = McpTrafficStore(max_events=2, max_sessions=4, subscriber_queue_size=2)
        await store.ingest(
            [
                traffic_event("event-001", "request_started", request_id="request-1"),
                traffic_event(
                    "event-002",
                    "request_finished",
                    request_id="request-1",
                    status="complete",
                    response_bytes=240,
                ),
                traffic_event("event-003", "request_started", request_id="request-2"),
            ]
        )

        snapshot = await store.snapshot()

        self.assertEqual(
            [event["event_id"] for event in snapshot["events"]], ["event-002", "event-003"]
        )
        self.assertEqual(snapshot["sequence"], 3)

    async def test_session_and_request_aggregates_follow_lifecycle(self):
        store = McpTrafficStore(max_events=16, max_sessions=4, subscriber_queue_size=2)
        await store.ingest(
            [
                traffic_event(
                    "event-001",
                    "session_opened",
                    request_id=None,
                    status="connected",
                    tool_name=None,
                ),
                traffic_event("event-002", "request_started"),
                traffic_event("event-003", "tool_started", status="started"),
            ]
        )
        active = await store.snapshot()
        client = active["clients"][0]
        self.assertEqual(client["active_sessions"], 1)
        self.assertEqual(client["active_requests"], 1)
        self.assertEqual(client["total_requests"], 0)
        self.assertEqual(client["last_tool"], "cptr_list_workspaces")

        await store.ingest(
            [
                traffic_event(
                    "event-004",
                    "request_failed",
                    status="error",
                    error_code="tool_error",
                    response_bytes=64,
                ),
                traffic_event(
                    "event-005",
                    "session_closed",
                    request_id=None,
                    status="disconnected",
                    tool_name=None,
                ),
            ]
        )
        terminal = await store.snapshot()
        client = terminal["clients"][0]
        self.assertEqual(client["active_sessions"], 0)
        self.assertEqual(client["active_requests"], 0)
        self.assertEqual(client["total_requests"], 1)
        self.assertEqual(client["errors"], 1)
        self.assertEqual(terminal["sessions"], [])

    async def test_stale_sessions_expire_without_fabricating_events(self):
        store = McpTrafficStore(
            max_events=8,
            max_sessions=4,
            subscriber_queue_size=2,
            session_ttl_seconds=5,
        )
        await store.ingest(
            [
                traffic_event(
                    "event-001",
                    "session_opened",
                    request_id=None,
                    status="connected",
                    tool_name=None,
                    timestamp_ms=BASE_TS,
                )
            ]
        )

        expired = await store.expire_stale_sessions(now_ms=BASE_TS + 6_000)
        snapshot = await store.snapshot()

        self.assertEqual(expired, 1)
        self.assertEqual(snapshot["sessions"], [])
        self.assertEqual(snapshot["clients"][0]["active_sessions"], 0)
        self.assertEqual(len(snapshot["events"]), 1)

    async def test_subscriber_queue_drops_oldest_without_blocking_ingest(self):
        store = McpTrafficStore(max_events=8, max_sessions=4, subscriber_queue_size=1)
        queue = store.subscribe()

        await store.ingest(
            [
                traffic_event("event-001", "request_started", request_id="request-1"),
                traffic_event("event-002", "request_started", request_id="request-2"),
            ]
        )

        queued = queue.get_nowait()
        snapshot = await store.snapshot()
        self.assertEqual(queued["event_id"], "event-002")
        self.assertEqual(snapshot["stream_health"]["slow_subscriber_drops"], 1)
        store.unsubscribe(queue)
        self.assertEqual(snapshot["stream_health"]["subscriber_count"], 1)
        self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 0)

    async def test_active_requests_are_bounded_when_terminal_events_are_lost(self):
        store = McpTrafficStore(max_events=2, max_sessions=4, subscriber_queue_size=2)
        await store.ingest(
            [
                traffic_event("event-001", "request_started", request_id="request-1"),
                traffic_event("event-002", "request_started", request_id="request-2"),
                traffic_event("event-003", "request_started", request_id="request-3"),
            ]
        )

        snapshot = await store.snapshot()
        self.assertEqual(snapshot["clients"][0]["active_requests"], 2)
        self.assertEqual(snapshot["stream_health"]["request_evictions"], 1)

    async def test_max_sessions_evicts_oldest_session_state_only(self):
        store = McpTrafficStore(max_events=8, max_sessions=1, subscriber_queue_size=2)
        await store.ingest(
            [
                traffic_event(
                    "event-001",
                    "session_opened",
                    session_id="session-1",
                    request_id=None,
                    status="connected",
                    tool_name=None,
                    timestamp_ms=BASE_TS,
                ),
                traffic_event(
                    "event-002",
                    "session_opened",
                    session_id="session-2",
                    request_id=None,
                    status="connected",
                    tool_name=None,
                    timestamp_ms=BASE_TS + 1,
                ),
            ]
        )

        snapshot = await store.snapshot()
        self.assertEqual([session["session_id"] for session in snapshot["sessions"]], ["session-2"])
        self.assertEqual(snapshot["stream_health"]["session_evictions"], 1)


class McpTrafficSchemaTests(unittest.TestCase):
    def test_unknown_fields_are_rejected(self):
        payload = traffic_event("event-001", "request_started").model_dump()
        payload["authorization"] = "Bearer must-not-be-accepted"
        with self.assertRaises(ValidationError):
            McpTrafficEvent.model_validate(payload)

    def test_client_label_is_bounded(self):
        payload = traffic_event("event-001", "request_started").model_dump()
        payload["client"]["label"] = "x" * 81
        with self.assertRaises(ValidationError):
            McpTrafficEvent.model_validate(payload)

    def test_error_code_is_allowlisted(self):
        payload = traffic_event("event-001", "request_failed", status="error").model_dump()
        payload["error_code"] = "raw stack trace"
        with self.assertRaises(ValidationError):
            McpTrafficEvent.model_validate(payload)

    def test_correlation_id_is_optional_bounded_metadata_only(self):
        payload = traffic_event("event-001", "request_started").model_dump()
        payload["correlation_id"] = "corr-1"
        event = McpTrafficEvent.model_validate(payload)
        self.assertEqual(event.correlation_id, "corr-1")
        encoded = event.model_dump()
        for unsafe in ("arguments_json", "result_json", "authorization", "headers"):
            self.assertNotIn(unsafe, encoded)

        payload["correlation_id"] = "x" * 129
        with self.assertRaises(ValidationError):
            McpTrafficEvent.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
