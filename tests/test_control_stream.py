import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers import control_stream
from cptr.services.live_events import LiveEventHub, LiveEventStore


class ControlStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_stream_emits_snapshot_then_replayable_event(self):
        hub = LiveEventHub(store=LiveEventStore())
        request = SimpleNamespace(
            headers={},
            query_params={},
            is_disconnected=AsyncMock(return_value=False),
        )
        agent = SimpleNamespace(
            get_task=AsyncMock(
                return_value={
                    "id": "task-1",
                    "status": "RUNNING",
                    "prompt": "secret prompt",
                    "output": "raw worker output",
                    "raw_output": [{"type": "reasoning", "text": "private"}],
                }
            )
        )
        with (
            patch.object(control_stream, "live_event_hub", hub),
            patch.object(control_stream, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(control_stream, "_services", return_value=(agent, SimpleNamespace())),
        ):
            response = await control_stream.task_stream(request, "task-1")
            iterator = response.body_iterator.__aiter__()
            snapshot = await iterator.__anext__()
            self.assertIn('"target":"task"', snapshot)
            self.assertNotIn("secret prompt", snapshot)
            self.assertNotIn("raw worker output", snapshot)
            self.assertNotIn("reasoning", snapshot)
            await hub.publish(
                user_id="user-1",
                target_key="task:task-1",
                task_id="task-1",
                event_type="shell.stdout",
                payload={"text": "bounded output"},
            )
            event = await iterator.__anext__()
            self.assertIn("shell.stdout", event)
            self.assertIn('"sequence":1', event)
            await iterator.aclose()

    async def test_terminal_snapshot_closes_without_polling(self):
        request = SimpleNamespace(
            headers={},
            query_params={},
            is_disconnected=AsyncMock(return_value=False),
        )
        agent = SimpleNamespace(
            get_task=AsyncMock(return_value={"id": "task-1", "status": "COMPLETE"})
        )
        with (
            patch.object(control_stream, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(control_stream, "_services", return_value=(agent, SimpleNamespace())),
        ):
            response = await control_stream.task_stream(request, "task-1")
            iterator = response.body_iterator.__aiter__()
            snapshot = await iterator.__anext__()
            self.assertIn("COMPLETE", snapshot)
            with self.assertRaises(StopAsyncIteration):
                await iterator.__anext__()

    async def test_task_snapshot_redacts_host_paths_from_errors(self):
        request = SimpleNamespace(
            headers={},
            query_params={},
            is_disconnected=AsyncMock(return_value=False),
        )
        agent = SimpleNamespace(
            get_task=AsyncMock(
                return_value={
                    "id": "task-1",
                    "status": "FAILED",
                    "error": "failed at /home/heidi/private/workspace/file.py and C:\\Users\\heidi\\secret.txt",
                }
            )
        )
        with (
            patch.object(control_stream, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(control_stream, "_services", return_value=(agent, SimpleNamespace())),
        ):
            response = await control_stream.task_stream(request, "task-1")
            snapshot = await response.body_iterator.__aiter__().__anext__()
            self.assertNotIn("/home/heidi/private/workspace/file.py", snapshot)
            self.assertNotIn("C:\\Users\\heidi\\secret.txt", snapshot)
            self.assertIn("<workspace-path>", snapshot)


if __name__ == "__main__":
    unittest.main()
