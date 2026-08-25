import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from cptr.services.agent_service import AgentService


class ExecutionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_interrupts_active_turn_and_keeps_control_task_running(self):
        from cptr.services.agent_service import AgentService

        task = SimpleNamespace(
            id="task-control",
            user_id="user-control",
            chat_id="chat-control",
            message_id="message-active",
        )
        control = SimpleNamespace(
            id="control-1",
            chat_message_id="queued-message",
            status="DELIVERED",
        )

        class Store:
            async def get(self, task_id):
                return task

            async def enqueue_message(self, **kwargs):
                return control

            async def get_message(self, message_id):
                return control

        service = AgentService(store=Store())
        with (
            patch("cptr.utils.chat_task.is_running", side_effect=[True, False]),
            patch("cptr.utils.chat_task.control_setup_ready", return_value=True),
            patch("cptr.utils.chat_task.get_active_chat_ids", return_value=set()),
            patch(
                "cptr.utils.chat_task.interrupt_for_control",
                new=AsyncMock(return_value=True),
            ) as interrupt,
            patch(
                "cptr.utils.chat_task.process_pending_chat_inputs",
                new=AsyncMock(),
            ) as process_pending,
            patch(
                "cptr.utils.identity.internal_request_for_user",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "cptr.models.Chat.get_by_id",
                new=AsyncMock(return_value=SimpleNamespace(meta={"workspace": "workspace-1"})),
            ),
        ):
            result = await service.send_message(
                "task-control",
                user_id="user-control",
                content="continue the assigned change",
            )

        interrupt.assert_awaited_once_with("message-active", timeout=ANY)
        process_pending.assert_awaited_once()
        self.assertEqual(result["delivery_status"], "DELIVERED")

    async def test_control_interrupt_stops_only_active_turn_without_terminal_cancel(self):
        from cptr.utils import chat_task

        async def active_turn():
            await asyncio.Event().wait()

        task = asyncio.create_task(active_turn())
        chat_task._tasks["message-control"] = task
        chat_task._task_chat["message-control"] = "chat-control"
        try:
            with patch(
                "cptr.utils.tools.cancel_owned_command_sessions",
                new=AsyncMock(return_value=True),
            ):
                self.assertTrue(
                    await chat_task.interrupt_for_control("message-control", timeout=0.2)
                )
            self.assertTrue(task.done())
            self.assertFalse(chat_task.is_cancel_requested("message-control"))
            self.assertTrue(chat_task.is_control_interrupt_requested("message-control"))
        finally:
            chat_task._tasks.pop("message-control", None)
            chat_task._task_chat.pop("message-control", None)
            chat_task._control_interrupt_requested.discard("message-control")

    async def test_control_interrupt_quiesces_owned_delayed_process_before_resume(self):
        from cptr.utils import chat_task
        from cptr.utils import tools

        with tempfile.TemporaryDirectory() as directory:
            marker = f"{directory}/late-marker.txt"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                (
                    "import time; time.sleep(2); "
                    f"open({marker!r}, 'w', encoding='utf-8').write('late')"
                ),
                start_new_session=True,
            )
            tools.command_sessions["control-owned"] = {
                "proc": process,
                "message_id": "message-control-process",
                "chat_id": "chat-control",
                "user_id": "user-control",
                "done": False,
                "log_task": None,
            }

            async def active_turn():
                await asyncio.Event().wait()

            task = asyncio.create_task(active_turn())
            chat_task._tasks["message-control-process"] = task
            try:
                self.assertTrue(
                    await chat_task.interrupt_for_control("message-control-process", timeout=1)
                )
                await asyncio.sleep(2.2)
                self.assertIsNotNone(process.returncode)
                self.assertFalse(os.path.exists(marker))
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                tools.command_sessions.pop("control-owned", None)
                chat_task._tasks.pop("message-control-process", None)
                chat_task._control_interrupt_requested.discard("message-control-process")

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
