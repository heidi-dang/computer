import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.agent_service import AgentService


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_existing_task_uses_shared_chat_task_boundary(self):
        service = AgentService()
        with patch("cptr.utils.chat_task.start_task") as start_task:
            result = await service.start_existing_task(
                request=object(),
                message_id="message-1",
                chat_id="chat-1",
                user_id="user-1",
                workspace="/workspace",
                target=object(),
                output_queue=object(),
            )

        self.assertEqual(
            result, {"chat_id": "chat-1", "message_id": "message-1", "status": "RUNNING"}
        )
        start_task.assert_called_once()
        self.assertEqual(start_task.call_args.kwargs["message_id"], "message-1")
        self.assertEqual(start_task.call_args.kwargs["chat_id"], "chat-1")

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

    async def test_get_task_exposes_bounded_control_delivery_records_without_content(self):
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
            output="visible output",
            error=None,
            created_at=1,
            updated_at=1,
        )
        message = SimpleNamespace(
            id="message-1",
            chat_id="chat-1",
            done=False,
            content="visible output",
            output=[],
            meta=None,
        )
        control_rows = [
            SimpleNamespace(
                id=f"control-{index}",
                status="CONSUMED",
                chat_message_id=f"queued-{index}",
                target_message_id="message-1",
                monitor_id="monitor-1",
                scope_id="scope-1",
                intended_message_id="message-1",
                consumed_task_id="task-1",
                consumed_message_id=f"worker-{index}",
                created_at=index,
                updated_at=index,
                delivered_at=index + 100,
                consumed_at=index + 200,
                content="/home/shacker/secret TOKEN",
                dedupe_key="secret-dedupe",
            )
            for index in range(25)
        ]

        class FakeResult:
            def scalars(self):
                return self

            def all(self):
                return control_rows[:21]

        class FakeDb:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, statement):
                return FakeResult()

        with (
            patch.object(service.store, "get", new=AsyncMock(return_value=task)),
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
            patch("cptr.utils.chat_task.is_running", return_value=True),
            patch.object(service.store, "update", new=AsyncMock()),
            patch("cptr.services.agent_service.get_db", new=AsyncMock(return_value=FakeDb())),
        ):
            result = await service.get_task("task-1", user_id="user-1")

        self.assertEqual(len(result["control_messages"]), 20)
        self.assertTrue(result["control_messages_truncated"])
        first = result["control_messages"][0]
        self.assertEqual(first["id"], "control-0")
        self.assertEqual(first["status"], "CONSUMED")
        self.assertEqual(first["intended_message_id"], "message-1")
        self.assertEqual(first["consumed_task_id"], "task-1")
        self.assertEqual(first["consumed_message_id"], "worker-0")
        self.assertEqual(first["delivered_at"], 100)
        self.assertEqual(first["consumed_at"], 200)
        self.assertNotIn("content", first)
        self.assertNotIn("dedupe_key", first)
        self.assertNotIn("secret", str(result["control_messages"]).lower())

    async def test_get_output_includes_control_delivery_records(self):
        service = AgentService()
        with patch.object(
            service,
            "get_task",
            new=AsyncMock(
                return_value={
                    "id": "task-1",
                    "status": "RUNNING",
                    "output": "visible",
                    "raw_output": [],
                    "control_messages": [
                        {
                            "id": "control-1",
                            "status": "DELIVERED",
                            "consumed_task_id": None,
                            "consumed_message_id": None,
                            "consumed_at": None,
                        }
                    ],
                    "control_messages_truncated": False,
                }
            ),
        ):
            result = await service.get_output("task-1", user_id="user-1")

        self.assertEqual(result["control_messages"][0]["id"], "control-1")
        self.assertEqual(result["control_messages"][0]["status"], "DELIVERED")
        self.assertFalse(result["control_messages_truncated"])

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
                        SimpleNamespace(**{**task.__dict__, "status": "CANCEL_REQUESTED"}),
                        SimpleNamespace(**{**task.__dict__, "status": "CANCELLED"}),
                    ]
                ),
            ),
            patch.object(
                service.store,
                "request_cancel",
                new=AsyncMock(return_value=True),
            ) as request_cancel,
            patch.object(service.store, "invalidate_messages_for_task", new=AsyncMock()),
            patch.object(service.store, "finalize_cancel", new=AsyncMock(return_value=True)),
            patch("cptr.utils.chat_task.cancel_task", new=AsyncMock(return_value=True)),
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
            patch.object(service.store, "update", new=AsyncMock()),
        ):
            result = await service.cancel_task("task-1", user_id="user-1")

        self.assertEqual(result["status"], "CANCELLED")
        self.assertTrue(result["cancelled"])
        request_cancel.assert_awaited_once()

    async def test_send_message_stays_queued_while_target_worker_is_running(self):
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
        queued = SimpleNamespace(
            id="control-message-1",
            chat_message_id="message-2",
            status="QUEUED",
        )
        with (
            patch.object(service.store, "get", new=AsyncMock(return_value=task)),
            patch.object(service.store, "enqueue_message", new=AsyncMock(return_value=queued)),
            patch("cptr.utils.chat_task.is_running", return_value=True),
            patch("cptr.utils.chat_task.process_pending_chat_inputs", new=AsyncMock()) as process,
        ):
            result = await service.send_message(
                "task-1",
                user_id="user-1",
                content="STEERING_MARKER_1",
                idempotency_key="steer-1",
            )

        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(result["delivery_status"], "QUEUED")
        process.assert_not_awaited()

    async def test_cancel_reports_when_completion_wins_terminal_race(self):
        service = AgentService()
        running = SimpleNamespace(
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
        complete = SimpleNamespace(**{**running.__dict__, "status": "COMPLETE"})
        message = SimpleNamespace(
            id="message-1",
            chat_id="chat-1",
            done=True,
            content="finished",
            output=[],
            meta=None,
        )
        with (
            patch.object(
                service.store,
                "get",
                new=AsyncMock(side_effect=[running, complete, complete]),
            ),
            patch.object(
                service.store,
                "request_cancel",
                new=AsyncMock(return_value=False),
            ),
            patch("cptr.utils.chat_task.cancel_task", new=AsyncMock()) as cancel,
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
        ):
            result = await service.cancel_task("task-1", user_id="user-1")

        self.assertEqual(result["status"], "COMPLETE")
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["cancel_race"], "completion_won")
        cancel.assert_not_awaited()

    async def test_cancelled_task_cannot_become_running_before_worker_flushes(self):
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
        cancelled = SimpleNamespace(**{**task.__dict__, "status": "CANCELLED"})
        message = SimpleNamespace(
            id="message-1",
            chat_id="chat-1",
            done=False,
            content="",
            output=[],
            meta=None,
        )
        with (
            patch.object(
                service.store,
                "get",
                new=AsyncMock(
                    side_effect=[
                        task,
                        SimpleNamespace(**{**task.__dict__, "status": "CANCEL_REQUESTED"}),
                        cancelled,
                    ]
                ),
            ),
            patch.object(service.store, "request_cancel", new=AsyncMock(return_value=True)),
            patch.object(service.store, "invalidate_messages_for_task", new=AsyncMock()),
            patch.object(service.store, "finalize_cancel", new=AsyncMock(return_value=True)),
            patch("cptr.utils.chat_task.cancel_task", new=AsyncMock(return_value=True)),
            patch("cptr.utils.chat_task.is_running", return_value=True),
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
            patch.object(service.store, "update", new=AsyncMock()),
        ):
            result = await service.cancel_task("task-1", user_id="user-1")

        self.assertEqual(result["status"], "CANCELLED")

    async def test_cancel_reports_blocker_when_owned_execution_does_not_quiesce(self):
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
        requested = SimpleNamespace(**{**task.__dict__, "status": "CANCEL_REQUESTED"})
        message = SimpleNamespace(
            id="message-1",
            chat_id="chat-1",
            done=False,
            content="partial",
            output=[],
            meta={"error": "owned execution still active"},
        )
        with (
            patch.object(
                service.store, "get", new=AsyncMock(side_effect=[task, requested, requested])
            ),
            patch.object(service.store, "request_cancel", new=AsyncMock(return_value=True)),
            patch.object(service.store, "invalidate_messages_for_task", new=AsyncMock()),
            patch("cptr.utils.chat_task.cancel_task", new=AsyncMock(return_value=False)),
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
        ):
            result = await service.cancel_task("task-1", user_id="user-1")

        self.assertEqual(result["status"], "CANCEL_REQUESTED")
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["cancellation_status"], "BLOCKED")

    async def test_reconciles_disappeared_worker_after_restart(self):
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
            done=False,
            content="partial output",
            output=[],
            meta=None,
        )
        with (
            patch.object(service.store, "get", new=AsyncMock(return_value=task)),
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
            patch("cptr.utils.chat_task.is_running", return_value=False),
            patch("cptr.models.ChatMessage.update", new=AsyncMock()) as message_update,
            patch.object(service.store, "update", new=AsyncMock()) as update,
        ):
            result = await service.get_task("task-1", user_id="user-1")

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"], "interrupted by CPTR restart")
        message_update.assert_awaited_once()
        self.assertTrue(
            any(call.kwargs.get("status") == "FAILED" for call in update.await_args_list)
        )

    async def test_control_interrupt_is_not_classified_as_restart(self):
        service = AgentService()
        task = SimpleNamespace(
            id="task-control-race",
            user_id="user-1",
            workspace_id="workspace-1",
            chat_id="chat-1",
            message_id="message-1",
            status="RUNNING",
            prompt="continue the assigned work",
            model_id="model-1",
            output="baseline",
            error=None,
            created_at=1,
            updated_at=1,
        )
        message = SimpleNamespace(
            id="message-1",
            chat_id="chat-1",
            done=False,
            content="baseline",
            output=[],
            meta={"interrupted_for_control": True},
        )

        class Store:
            async def get(self, task_id):
                return task

            async def has_pending_control(self, task_id):
                return True

            async def update(self, *args, **kwargs):
                raise AssertionError("control interruption must not be finalized as restart")

        service.store = Store()
        with (
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
            patch("cptr.utils.chat_task.is_running", return_value=False),
        ):
            result = await service.get_task("task-control-race", user_id="user-1")

        self.assertEqual(result["status"], "RUNNING")
        self.assertNotIn("interrupted by CPTR restart", str(result.get("error")))

    async def test_non_git_diff_returns_bounded_diagnostic(self):
        service = AgentService()
        workspace = SimpleNamespace(id="workspace-1", user_id="user-1", path="/disposable/non-git")

        class FakeDb:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, model, key):
                return workspace

        with (
            patch("cptr.services.agent_service.get_db", new=AsyncMock(return_value=FakeDb())),
            patch("cptr.utils.git.is_repo", new=AsyncMock(return_value=False)),
            patch("cptr.utils.git.diff", new=AsyncMock()) as diff,
        ):
            result = await service.get_diff("workspace-1", user_id="user-1")

        self.assertEqual(
            result, {"is_repo": False, "files": [], "diagnostic": "not a git repository"}
        )
        diff.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
