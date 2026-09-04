import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers import control_stream
from cptr.services.live_events import LiveEventHub, LiveEventStore, command_target_key


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

    async def test_task_recovery_snapshot_returns_authorized_replay(self):
        hub = LiveEventHub(store=LiveEventStore())
        await hub.publish(
            user_id="user-1",
            target_key="task:task-1",
            task_id="task-1",
            event_type="terminal.chunk",
            payload={"text": "safe output", "stream": "stdout"},
        )
        request = SimpleNamespace(headers={}, query_params={"after": "0"})
        agent = SimpleNamespace(
            get_task=AsyncMock(return_value={"id": "task-1", "status": "RUNNING"})
        )
        with (
            patch.object(control_stream, "live_event_hub", hub),
            patch.object(control_stream, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(control_stream, "_services", return_value=(agent, SimpleNamespace())),
        ):
            snapshot = await control_stream.task_stream_snapshot(request, "task-1")
        self.assertEqual(snapshot["target"], "task")
        self.assertEqual(snapshot["snapshot"]["status"], "RUNNING")
        self.assertEqual(snapshot["replay"]["last_sequence"], 1)
        self.assertEqual(snapshot["replay"]["events"][0]["type"], "terminal.chunk")

    async def test_command_recovery_snapshot_is_workspace_owned_redacted_and_replayable(self):
        hub = LiveEventHub(store=LiveEventStore())
        target_key = command_target_key("ws-1", "cmd-1")
        await hub.publish(
            user_id="user-1",
            target_key=target_key,
            event_type="terminal.chunk",
            payload={"command_id": "cmd-1", "text": "safe output", "stream": "stdout"},
        )
        request = SimpleNamespace(headers={}, query_params={"after": "0"})
        user = AsyncMock(return_value="user-1")
        workspace_lookup = AsyncMock(return_value=SimpleNamespace(path="/tmp/workspace"))
        command_snapshot = AsyncMock(
            return_value={
                "command_id": "cmd-1",
                "status": "RUNNING",
                "exit_code": None,
                "output": "raw output must not bypass live redaction",
                "next_offset": 37,
            }
        )
        with (
            patch.object(control_stream, "live_event_hub", hub),
            patch.object(control_stream, "_user", new=user),
            patch.object(control_stream, "_workspace", new=workspace_lookup),
            patch.object(control_stream, "_command_snapshot", new=command_snapshot),
        ):
            snapshot = await control_stream.command_stream_snapshot(request, "ws-1", "cmd-1")

        user.assert_awaited_once_with(request, "command:execute")
        workspace_lookup.assert_awaited_once_with("user-1", "ws-1")
        self.assertEqual(snapshot["target"], "command")
        self.assertEqual(snapshot["snapshot"]["workspace_id"], "ws-1")
        self.assertEqual(snapshot["snapshot"]["command_id"], "cmd-1")
        self.assertNotIn("output", snapshot["snapshot"])
        self.assertEqual(snapshot["replay"]["target_key"], target_key)
        self.assertEqual(snapshot["replay"]["events"][0]["type"], "terminal.chunk")

    async def test_command_sse_stream_is_workspace_isolated_and_replays_live_events(self):
        hub = LiveEventHub(store=LiveEventStore())
        request = SimpleNamespace(
            headers={},
            query_params={"after": "0"},
            is_disconnected=AsyncMock(return_value=False),
        )
        workspace_lookup = AsyncMock(return_value=SimpleNamespace(path="/tmp/workspace-one"))
        command_snapshot = AsyncMock(
            return_value={
                "command_id": "cmd-1",
                "status": "RUNNING",
                "exit_code": None,
                "output": "must stay out of snapshot",
                "next_offset": 0,
            }
        )
        with (
            patch.object(control_stream, "live_event_hub", hub),
            patch.object(control_stream, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(control_stream, "_workspace", new=workspace_lookup),
            patch.object(control_stream, "_command_snapshot", new=command_snapshot),
        ):
            response = await control_stream.command_stream(request, "ws-1", "cmd-1")
            iterator = response.body_iterator.__aiter__()
            snapshot = await iterator.__anext__()
            self.assertIn('"target":"command"', snapshot)
            self.assertNotIn("must stay out of snapshot", snapshot)

            await hub.publish(
                user_id="user-1",
                target_key=command_target_key("ws-2", "cmd-1"),
                event_type="terminal.chunk",
                payload={"command_id": "cmd-1", "text": "wrong-workspace", "stream": "stdout"},
            )
            await hub.publish(
                user_id="user-1",
                target_key=command_target_key("ws-1", "cmd-1"),
                event_type="terminal.chunk",
                payload={"command_id": "cmd-1", "text": "right-workspace", "stream": "stdout"},
            )
            event = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            self.assertIn("right-workspace", event)
            self.assertNotIn("wrong-workspace", event)
            await iterator.aclose()

        workspace_lookup.assert_awaited_once_with("user-1", "ws-1")


if __name__ == "__main__":
    unittest.main()
