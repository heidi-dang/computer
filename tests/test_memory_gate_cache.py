import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.memory import gate
from cptr.memory.domain import MemoryContextBundle


class MemoryGateCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gate._gate_cache.clear()

    @staticmethod
    def request():
        return SimpleNamespace(
            method="POST",
            url=SimpleNamespace(path="/api/control/v1/workspaces/ws-1/coding/write"),
            state=SimpleNamespace(),
        )

    @staticmethod
    def settings():
        return {
            "enabled": True,
            "required_for_execution": True,
            "context_char_limit": 9000,
            "canonical_char_limit": 3000,
            "verification_ttl_seconds": 86400,
        }

    async def test_fast_gate_reuses_prepared_context_after_version_revalidation(self):
        bundle = MemoryContextBundle(
            context_id="memctx-1",
            status="ready",
            memory_version=7,
            rendered="cached",
        )
        store = SimpleNamespace(namespace_version=AsyncMock(return_value=7))
        service = SimpleNamespace(store=store, prepare_context=AsyncMock(return_value=bundle))
        request = self.request()

        with (
            patch.object(gate, "_workspace_path_for_request", new=AsyncMock(return_value="/repo")),
            patch.object(gate, "get_memory_service", return_value=service),
            patch("cptr.utils.memory.get_memory_settings", new=AsyncMock(return_value=self.settings())),
        ):
            first = await gate.require_control_action_memory(
                request, user_id="user-1", required_scope="coding:write"
            )
            second = await gate.require_control_action_memory(
                request, user_id="user-1", required_scope="coding:write"
            )

        self.assertIs(first, bundle)
        self.assertIs(second, bundle)
        self.assertEqual(service.prepare_context.await_count, 1)
        self.assertEqual(store.namespace_version.await_count, 2)
        self.assertEqual(request.state.memory_context_id, "memctx-1")
        self.assertEqual(request.state.memory_version, 7)
        self.assertEqual(request.state.memory_status, "ready")

    async def test_fast_gate_refreshes_when_namespace_version_changes(self):
        first_bundle = MemoryContextBundle("memctx-1", "ready", 7, "first")
        second_bundle = MemoryContextBundle("memctx-2", "ready", 8, "second")
        store = SimpleNamespace(namespace_version=AsyncMock(side_effect=[7, 8]))
        service = SimpleNamespace(
            store=store,
            prepare_context=AsyncMock(side_effect=[first_bundle, second_bundle]),
        )

        with (
            patch.object(gate, "_workspace_path_for_request", new=AsyncMock(return_value="/repo")),
            patch.object(gate, "get_memory_service", return_value=service),
            patch("cptr.utils.memory.get_memory_settings", new=AsyncMock(return_value=self.settings())),
        ):
            await gate.require_control_action_memory(
                self.request(), user_id="user-1", required_scope="command:execute"
            )
            refreshed = await gate.require_control_action_memory(
                self.request(), user_id="user-1", required_scope="command:execute"
            )

        self.assertIs(refreshed, second_bundle)
        self.assertEqual(service.prepare_context.await_count, 2)

    async def test_fast_gate_remains_fail_closed_when_version_revalidation_fails(self):
        store = SimpleNamespace(namespace_version=AsyncMock(side_effect=RuntimeError("db unavailable")))
        service = SimpleNamespace(store=store, prepare_context=AsyncMock())

        with (
            patch.object(gate, "_workspace_path_for_request", new=AsyncMock(return_value="/repo")),
            patch.object(gate, "get_memory_service", return_value=service),
            patch("cptr.utils.memory.get_memory_settings", new=AsyncMock(return_value=self.settings())),
        ):
            with self.assertRaises(RuntimeError):
                await gate.require_control_action_memory(
                    self.request(), user_id="user-1", required_scope="coding:write"
                )

        service.prepare_context.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
