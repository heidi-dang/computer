import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.action_traces import ActionTraceStore
from cptr.utils.tools import _queue_command_session_event


class ActionTraceStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_trace_links_layers_and_late_cleanup_by_entity(self):
        store = ActionTraceStore(max_traces=8, max_stages_per_trace=12)

        await store.append(
            owner_id="user-1",
            trace_id="trace-1",
            layer="chatgpt",
            name="chatgpt.request",
            status="started",
            timestamp_ms=100,
            request_id="request-1",
            tool_name="cptr_code_run_command",
            dedupe_key="traffic-request-started",
        )
        await store.append(
            owner_id="user-1",
            trace_id="trace-1",
            layer="mcp",
            name="mcp.tool.started",
            status="started",
            timestamp_ms=110,
            tool_name="cptr_code_run_command",
            dedupe_key="traffic-tool-started",
        )
        self.assertTrue(
            await store.link_entity(
                owner_id="user-1",
                trace_id="trace-1",
                entity_type="command",
                entity_id="cmd-1",
            )
        )
        await store.append_for_entity(
            owner_id="user-1",
            entity_type="command",
            entity_id="cmd-1",
            layer="cleanup",
            name="command.cleanup",
            status="ok",
            timestamp_ms=200,
        )

        detail = await store.get(owner_id="user-1", trace_id="trace-1")
        assert detail is not None
        self.assertEqual(detail["trace_id"], "trace-1")
        self.assertEqual(detail["tool_name"], "cptr_code_run_command")
        self.assertEqual(detail["layers"], ["chatgpt", "mcp", "command", "cleanup"])
        self.assertEqual(detail["entities"]["command"], ["cmd-1"])
        self.assertEqual(detail["stages"][-1]["name"], "command.cleanup")
        self.assertNotIn("payload", detail["stages"][-1])
        self.assertNotIn("arguments", detail["stages"][-1])

    async def test_owner_isolation_and_entity_link_is_first_trace_wins(self):
        store = ActionTraceStore(max_traces=8, max_stages_per_trace=8)
        await store.append(
            owner_id="user-a",
            trace_id="trace-a",
            layer="backend",
            name="backend.request",
            status="ok",
            timestamp_ms=10,
        )
        first = await store.link_entity(
            owner_id="user-a",
            trace_id="trace-a",
            entity_type="command",
            entity_id="cmd-shared",
        )
        second = await store.link_entity(
            owner_id="user-a",
            trace_id="trace-new",
            entity_type="command",
            entity_id="cmd-shared",
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIsNone(await store.get(owner_id="user-b", trace_id="trace-a"))
        self.assertEqual(
            await store.resolve_entity(
                owner_id="user-a", entity_type="command", entity_id="cmd-shared"
            ),
            "trace-a",
        )
        self.assertIsNone(
            await store.resolve_entity(
                owner_id="user-b", entity_type="command", entity_id="cmd-shared"
            )
        )

    async def test_duplicate_sources_and_stage_history_are_bounded(self):
        store = ActionTraceStore(max_traces=2, max_stages_per_trace=3)
        for index in range(5):
            await store.append(
                owner_id="user-1",
                trace_id="trace-1",
                layer="backend",
                name=f"backend.{index}",
                status="ok",
                timestamp_ms=index,
                dedupe_key="same" if index < 2 else f"event-{index}",
            )
        detail = await store.get(owner_id="user-1", trace_id="trace-1")
        assert detail is not None
        self.assertEqual(len(detail["stages"]), 3)
        self.assertEqual(
            [stage["name"] for stage in detail["stages"]], ["backend.2", "backend.3", "backend.4"]
        )

        await store.append(
            owner_id="user-1",
            trace_id="trace-2",
            layer="mcp",
            name="mcp.request",
            status="ok",
            timestamp_ms=20,
        )
        await store.append(
            owner_id="user-1",
            trace_id="trace-3",
            layer="mcp",
            name="mcp.request",
            status="ok",
            timestamp_ms=30,
        )
        self.assertIsNone(await store.get(owner_id="user-1", trace_id="trace-1"))
        summaries = await store.summaries(owner_id="user-1", limit=10)
        self.assertEqual([item["trace_id"] for item in summaries["traces"]], ["trace-3", "trace-2"])
        self.assertTrue(all("stages" not in item for item in summaries["traces"]))

    async def test_mcp_projection_reuses_correlation_and_dedupes_existing_events(self):
        store = ActionTraceStore(max_traces=8, max_stages_per_trace=12)
        traffic = SimpleNamespace(
            event_id="traffic-1",
            event_type="tool_started",
            correlation_id="corr-1",
            request_id="req-1",
            session_id="session-1",
            tool_name="cptr_code_run_command",
            timestamp_ms=100,
            duration_ms=None,
            error_code=None,
        )
        backend = SimpleNamespace(
            kind="latency",
            event_id="diag-1",
            correlation_id="corr-1",
            edge_id="cptr-mcp-cptr-backend",
            status="ok",
            timestamp_ms=125,
            duration_ms=25,
            request_id="req-1",
            tool_name="cptr_code_run_command",
        )

        self.assertEqual(await store.observe_mcp_traffic(owner_id="user-1", events=[traffic]), 1)
        self.assertEqual(await store.observe_mcp_traffic(owner_id="user-1", events=[traffic]), 0)
        self.assertEqual(
            await store.observe_mcp_diagnostics(owner_id="user-1", events=[backend]), 1
        )
        detail = await store.get(owner_id="user-1", trace_id="corr-1")
        assert detail is not None
        self.assertEqual(detail["request_id"], "req-1")
        self.assertEqual(detail["mcp_session_id"], "session-1")
        self.assertEqual(detail["layers"], ["mcp", "backend"])
        self.assertEqual(
            [stage["name"] for stage in detail["stages"]], ["mcp.tool.started", "backend.request"]
        )

    async def test_command_event_projection_is_payload_free_and_keeps_original_trace(self):
        session = {
            "trace_id": "trace-command",
            "trace_request_id": "req-command",
            "trace_tool_name": "cptr_code_run_command",
            "trace_workspace_id": "ws-1",
            "user_id": "user-1",
            "command_session_id": "cmd-1",
        }
        with patch(
            "cptr.services.action_traces.action_trace_store.append",
            new=AsyncMock(return_value=True),
        ) as append_trace:
            await _queue_command_session_event(
                session,
                "command.completed",
                {"status": "COMPLETE", "exit_code": 0, "command": "must-not-leak"},
            )

        kwargs = append_trace.await_args.kwargs
        self.assertEqual(kwargs["trace_id"], "trace-command")
        self.assertEqual(kwargs["entity_id"], "cmd-1")
        self.assertEqual(kwargs["status"], "ok")
        self.assertNotIn("command", kwargs)
        self.assertNotIn("payload", kwargs)

    async def test_bounded_stage_eviction_preserves_full_trace_duration(self):
        store = ActionTraceStore(max_traces=4, max_stages_per_trace=2)
        for timestamp in (100, 200, 300):
            await store.append(
                owner_id="user-1",
                trace_id="trace-duration",
                layer="backend",
                name=f"backend.{timestamp}",
                status="ok",
                timestamp_ms=timestamp,
            )
        summary = (await store.summaries(owner_id="user-1"))["traces"][0]
        self.assertEqual(summary["started_at_ms"], 100)
        self.assertEqual(summary["updated_at_ms"], 300)
        self.assertEqual(summary["duration_ms"], 200)
        self.assertEqual(summary["stage_count"], 2)

    async def test_terminal_status_and_duration_are_derived_from_stages(self):
        store = ActionTraceStore(max_traces=4, max_stages_per_trace=8)
        await store.append(
            owner_id="user-1",
            trace_id="trace-error",
            layer="chatgpt",
            name="chatgpt.request",
            status="started",
            timestamp_ms=100,
        )
        await store.append(
            owner_id="user-1",
            trace_id="trace-error",
            layer="backend",
            name="backend.request",
            status="error",
            timestamp_ms=145,
            duration_ms=20,
            error_code="backend_unavailable",
        )
        summary = (await store.summaries(owner_id="user-1"))["traces"][0]
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["duration_ms"], 45)
        self.assertEqual(summary["error_code"], "backend_unavailable")


if __name__ == "__main__":
    unittest.main()
