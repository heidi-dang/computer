import asyncio
import unittest

from pydantic import ValidationError

from cptr.services.mcp_activity import McpActivityEvent, McpActivityStore

BASE_TS = 1_788_000_000_000


def activity_event(event_id: str, phase: str = "started", **overrides) -> McpActivityEvent:
    payload = {
        "version": 1,
        "event_id": event_id,
        "sequence": 1,
        "timestamp_ms": BASE_TS,
        "client": {"id": "chatgpt", "label": "ChatGPT", "version": "1.0"},
        "session_id": "session-1",
        "request_id": "request-1",
        "tool_name": "cptr_list_workspaces",
        "title": "List workspaces",
        "phase": phase,
        "summary": "Working: List workspaces.",
        "arguments_json": '{"include_unavailable":false}' if phase == "started" else None,
        "result_json": '{"workspaces":[]}' if phase == "complete" else None,
        "error_json": '{"code":"mcp_tool_error"}' if phase == "failed" else None,
        "duration_ms": 12 if phase != "started" else None,
    }
    payload.update(overrides)
    return McpActivityEvent(**payload)


class McpActivitySchemaTests(unittest.TestCase):
    def test_schema_rejects_unknown_sensitive_fields_and_enforces_bounds(self):
        base = activity_event("event-001").model_dump()
        with self.assertRaises(ValidationError):
            McpActivityEvent(**base, authorization="Bearer secret")

        correlated = McpActivityEvent.model_validate({**base, "correlation_id": "corr-1"})
        self.assertEqual(correlated.correlation_id, "corr-1")
        with self.assertRaises(ValidationError):
            McpActivityEvent.model_validate({**base, "correlation_id": "x" * 129})

        for field, value in (
            ("tool_name", "x" * 257),
            ("title", "x" * 161),
            ("summary", "x" * 501),
            ("arguments_json", "x" * 13_001),
            ("result_json", "x" * 13_001),
            ("error_json", "x" * 13_001),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                activity_event("event-002", **{field: value})


class McpActivityStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_deduplicates_and_truncates_newest_last(self):
        store = McpActivityStore(max_events=2, subscriber_queue_size=2)
        first = activity_event("event-001")
        duplicate_result = await store.ingest([first, first])
        self.assertEqual(duplicate_result, {"accepted": 1, "duplicates": 1, "dropped": 0})

        await store.ingest(
            [
                activity_event("event-002", "complete"),
                activity_event("event-003", "failed"),
            ]
        )
        snapshot = await store.snapshot()
        self.assertEqual(
            [event["event_id"] for event in snapshot["events"]], ["event-002", "event-003"]
        )
        self.assertEqual(snapshot["sequence"], 3)
        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["stream_health"]["event_capacity"], 2)

    async def test_subscriber_queue_is_bounded_and_non_blocking(self):
        store = McpActivityStore(max_events=8, subscriber_queue_size=1)
        queue = store.subscribe()
        await asyncio.wait_for(store.ingest([activity_event("event-001")]), timeout=0.2)
        await asyncio.wait_for(store.ingest([activity_event("event-002")]), timeout=0.2)

        self.assertEqual(queue.qsize(), 1)
        latest = queue.get_nowait()
        self.assertEqual(latest["event_id"], "event-002")
        snapshot = await store.snapshot()
        self.assertEqual(snapshot["stream_health"]["subscriber_count"], 1)
        self.assertGreaterEqual(snapshot["stream_health"]["slow_subscriber_drops"], 1)

        store.unsubscribe(queue)
        self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 0)

    async def test_snapshot_contains_only_bounded_activity_projection(self):
        store = McpActivityStore(max_events=4, subscriber_queue_size=2)
        await store.ingest([activity_event("event-001")])
        snapshot = await store.snapshot()
        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["sequence"], 1)
        self.assertEqual(snapshot["stream_health"]["event_capacity"], 4)
        self.assertEqual(snapshot["events"][0]["arguments_json"], '{"include_unavailable":false}')
        self.assertNotIn("authorization", str(snapshot).lower())


if __name__ == "__main__":
    unittest.main()
