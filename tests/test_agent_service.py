import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.agent_service import AgentService


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_task_reads_durable_message_state(self):
        service = AgentService()
        task = SimpleNamespace(
            id="task-1",
            user_id="user-1",
            workspace_id="workspace-1",
            chat_id="chat-1",
            message_id="message-1",
            status="RUNNING",
            prompt="do work",
            model_id="model-1",
            output=None,
            error=None,
            created_at=1,
            updated_at=1,
        )
        message = SimpleNamespace(
            id="message-1",
            chat_id="chat-1",
            done=True,
            content="finished output",
            output=[{"type": "message", "content": "finished output"}],
            meta=None,
        )

        with (
            patch.object(
                service.store,
                "get",
                new=AsyncMock(
                    side_effect=[
                        task,
                        SimpleNamespace(**{**task.__dict__, "status": "CANCELLED"}),
                    ]
                ),
            ),
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
            patch.object(service.store, "update", new=AsyncMock()) as update,
        ):
            result = await service.get_task("task-1", user_id="user-1")

        self.assertEqual(result["id"], "task-1")
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["output"], "finished output")
        update.assert_awaited_once()

    async def test_cancel_marks_task_cancelled_when_worker_exists(self):
        service = AgentService()
        task = SimpleNamespace(
            id="task-1",
            user_id="user-1",
            workspace_id="workspace-1",
            chat_id="chat-1",
            message_id="message-1",
            status="RUNNING",
            prompt="do work",
            model_id="model-1",
            output=None,
            error=None,
            created_at=1,
            updated_at=1,
        )
        message = SimpleNamespace(
            id="message-1",
            chat_id="chat-1",
            done=True,
            content="cancelled output",
            output=[],
            meta={"error": "cancelled"},
        )
        with (
            patch.object(
                service.store,
                "get",
                new=AsyncMock(
                    side_effect=[
                        task,
                        SimpleNamespace(**{**task.__dict__, "status": "CANCELLED"}),
                    ]
                ),
            ),
            patch("cptr.utils.chat_task.cancel_task", new=AsyncMock(return_value=True)),
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
            patch.object(service.store, "update", new=AsyncMock()) as update,
        ):
            result = await service.cancel_task("task-1", user_id="user-1")

        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(update.await_count, 2)


if __name__ == "__main__":
    unittest.main()
