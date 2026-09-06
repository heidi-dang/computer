import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import cptr.routers.control as control
from cptr.routers import gateway


class ControlMemoryBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_memory_read_is_owner_bound_read_only_and_scoped(self):
        endpoint = getattr(control, "read_memory", None)
        request_model = getattr(control, "MemoryReadRequest", None)
        self.assertTrue(callable(endpoint), "Control API must expose read_memory")
        self.assertIsNotNone(request_model, "Control API must define MemoryReadRequest")

        request = SimpleNamespace(state=SimpleNamespace())
        body = request_model(
            action="search",
            workspace_id="ws-1",
            query="deployment procedure",
            limit=8,
            include_historical=False,
        )
        adapter = SimpleNamespace(
            call_tool=AsyncMock(
                return_value={
                    "results": [
                        {
                            "memory_id": "mem-1",
                            "scope": "workspace",
                            "kind": "procedure",
                            "text": "Run verification before restart.",
                            "score": 0.95,
                            "reason": "hybrid retrieval",
                            "confidence": 0.98,
                            "trust_level": "verified_system_fact",
                            "verification_stale": False,
                        }
                    ]
                }
            ),
            service=SimpleNamespace(record_event=AsyncMock(return_value="event-1")),
        )
        workspace = SimpleNamespace(path="/repo")
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")) as require_user,
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=workspace)) as ensure_workspace,
            patch("cptr.routers.control.MemoryMcpAdapter", return_value=adapter) as adapter_factory,
        ):
            result = await endpoint(request, body)

        require_user.assert_awaited_once_with(request, "memory:read")
        ensure_workspace.assert_awaited_once_with("user-1", "ws-1")
        adapter_factory.assert_called_once_with(
            user_id="user-1",
            workspace="/repo",
            allow_mutations=False,
        )
        adapter.call_tool.assert_awaited_once_with(
            "memory.search",
            {"query": "deployment procedure", "limit": 8, "include_historical": False},
        )
        self.assertEqual(result["action"], "search")
        self.assertEqual(result["workspace_id"], "ws-1")
        self.assertEqual(result["result"]["results"][0]["memory_id"], "mem-1")
        adapter.service.record_event.assert_awaited_once()
        event = adapter.service.record_event.await_args.kwargs
        self.assertEqual(event["user_id"], "user-1")
        self.assertEqual(event["workspace"], "/repo")
        self.assertEqual(event["event_type"], "recall")
        self.assertEqual(event["payload"]["items"][0]["node_id"], "mem-1")

    async def test_control_memory_read_rejects_mutation_actions(self):
        request_model = getattr(control, "MemoryReadRequest", None)
        self.assertIsNotNone(request_model, "Control API must define MemoryReadRequest")
        with self.assertRaises(Exception):
            request_model(action="forget", memory_id="mem-1")

    def test_memory_read_scope_is_supported_and_default_for_new_control_keys(self):
        self.assertIn("memory:read", gateway.ALLOWED_CONTROL_SCOPES)
        self.assertIn("memory:read", gateway.DEFAULT_CONTROL_SCOPES)


if __name__ == "__main__":
    unittest.main()
