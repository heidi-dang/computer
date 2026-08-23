import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.agent_service import AgentService


class ExecutionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_owned_command_sessions_kills_only_matching_message_and_waits(self):
        from cptr.utils import tools

        matching = SimpleNamespace(pid=101, returncode=None)
        unrelated = SimpleNamespace(pid=202, returncode=None)
        tools.command_sessions.clear()
        tools.command_sessions.update(
            {
                "owned": {
                    "proc": matching,
                    "message_id": "message-1",
                    "chat_id": "chat-1",
                    "user_id": "user-1",
                    "done": False,
                    "log_task": None,
                },
                "unrelated": {
                    "proc": unrelated,
                    "message_id": "message-2",
                    "chat_id": "chat-2",
                    "user_id": "user-1",
                    "done": False,
                    "log_task": None,
                },
            }
        )
        try:
            with patch("cptr.utils.tools._kill_process_group") as kill:
                await tools.cancel_owned_command_sessions("message-1", timeout=0.1)

            kill.assert_called_once_with(101, force=False)
            self.assertFalse(unrelated.returncode is not None)
        finally:
            tools.command_sessions.clear()

    async def test_cancel_owned_command_session_quiesces_real_process_group(self):
        from cptr.utils import tools

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        )
        session = {
            "proc": process,
            "message_id": "message-real",
            "chat_id": "chat-real",
            "user_id": "user-real",
            "done": False,
            "log_task": None,
        }
        tools.command_sessions["real"] = session
        try:
            self.assertTrue(await tools.cancel_owned_command_sessions("message-real", timeout=1))
            self.assertIsNotNone(process.returncode)
            self.assertTrue(session["done"])
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            tools.command_sessions.pop("real", None)

    async def test_agent_service_cancellation_quiesces_owned_process_before_finalize(self):
        from cptr.utils import tools

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            start_new_session=True,
        )
        tools.command_sessions["agent-owned"] = {
            "proc": process,
            "message_id": "message-owned",
            "chat_id": "chat-owned",
            "user_id": "user-owned",
            "done": False,
            "log_task": None,
        }
        task = SimpleNamespace(
            id="task-owned",
            user_id="user-owned",
            workspace_id="workspace-owned",
            chat_id="chat-owned",
            message_id="message-owned",
            status="RUNNING",
            prompt="owned work",
            model_id="model-owned",
            output=None,
            error=None,
            created_at=1,
            updated_at=1,
        )
        requested = SimpleNamespace(**{**task.__dict__, "status": "CANCEL_REQUESTED"})
        cancelled = SimpleNamespace(**{**task.__dict__, "status": "CANCELLED"})
        message = SimpleNamespace(
            id="message-owned",
            chat_id="chat-owned",
            done=True,
            content="cancelled",
            output=[],
            meta={"error": "cancelled"},
        )
        service = AgentService()
        try:
            with (
                patch.object(
                    service.store, "get", new=AsyncMock(side_effect=[task, requested, cancelled])
                ),
                patch.object(service.store, "request_cancel", new=AsyncMock(return_value=True)),
                patch.object(service.store, "invalidate_messages_for_task", new=AsyncMock()),
                patch.object(
                    service.store, "finalize_cancel", new=AsyncMock(return_value=True)
                ) as finalize,
                patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
            ):
                result = await service.cancel_task("task-owned", user_id="user-owned")

            self.assertTrue(result["cancelled"])
            self.assertIsNotNone(process.returncode)
            self.assertTrue(tools.command_sessions["agent-owned"]["done"])
            finalize.assert_awaited_once()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            tools.command_sessions.pop("agent-owned", None)


if __name__ == "__main__":
    unittest.main()
