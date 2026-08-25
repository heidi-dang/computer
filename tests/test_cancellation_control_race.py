import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.agent_service import AgentService
from cptr.services.supervisor import (
    AutonomousSupervisor,
    Decision,
    InMemorySupervisorStore,
    MonitorStatus,
)


INVALIDATION_SCRIPT = r"""
import asyncio
import json
import os

from cptr.models import Chat, ChatMessage, ControlTask, User, Workspace
from cptr.services.control_store import ControlTaskStore
from cptr.utils.db import get_db, init_db


async def main():
    await init_db()
    user_id = await User.create("cancel-test", "password-hash", role="user", created_at=1)
    workspace = await Workspace.upsert(user_id, os.environ["CANCEL_WORKSPACE"], "cancel", {})
    chat = await Chat.create(user_id=user_id, title="cancel", meta={}, created_at=1)
    queued = await ChatMessage.create(
        chat_id=chat.id,
        role="user",
        content="STEERING_MARKER_CANCEL",
        meta={"queued": True},
        created_at=1,
    )
    async with await get_db() as db:
        db.add(
            ControlTask(
                id="task_cancel",
                user_id=user_id,
                workspace_id=workspace.id,
                chat_id=chat.id,
                message_id="worker-message",
                status="RUNNING",
                prompt="worker",
                model_id="model",
                created_at=1,
                updated_at=1,
            )
        )
        await db.commit()
    store = ControlTaskStore()
    control = await store.enqueue_message(
        task_id="task_cancel",
        user_id=user_id,
        chat_id=chat.id,
        content="STEERING_MARKER_CANCEL",
        dedupe_key="cancel-race",
        chat_message_id=queued.id,
        now=2,
    )
    invalidated = await store.invalidate_messages_for_task("task_cancel", now=3)
    current = await store.get_message(control.id)
    queued_row = await ChatMessage.get_by_id(queued.id)
    print(
        json.dumps(
            {
                "invalidated": invalidated,
                "status": current.status,
                "queued": (queued_row.meta or {}).get("queued"),
                "delivery_status": (queued_row.meta or {}).get("delivery_status"),
            }
        )
    )


asyncio.run(main())
"""


class AcceptingDirector:
    async def evaluate(self, **kwargs):
        return Decision(scope_satisfied=True)

    async def diagnose(self, **kwargs):
        return Decision(next_assignment="repair")

    async def plan_next_action(self, **kwargs):
        return Decision(next_assignment="repair")

    async def final_gate(self, **kwargs):
        return Decision(goal_satisfied=True)


class CancelAwareAgent:
    def __init__(self):
        self.started = []
        self.tasks = {}
        self.cancelled = []

    async def start_task(self, *, workspace_id, prompt, model_id, idempotency_key=None, **kwargs):
        task_id = f"task_{len(self.started) + 1}"
        self.started.append((task_id, prompt, idempotency_key))
        self.tasks[task_id] = {"id": task_id, "status": "COMPLETE", "output": "worker finished"}
        return self.tasks[task_id]

    async def get_task(self, task_id, **kwargs):
        return self.tasks[task_id]

    async def get_output(self, task_id, **kwargs):
        return {"task_id": task_id, "content": self.tasks[task_id]["output"]}

    async def get_diff(self, workspace_id, **kwargs):
        return {
            "files": ["src/example.py"],
            "patch": "diff --git a/src/example.py b/src/example.py",
        }

    async def cancel_task(self, task_id, **kwargs):
        self.cancelled.append(task_id)
        self.tasks[task_id]["status"] = "CANCELLED"
        return {"id": task_id, "status": "CANCELLED", "cancelled": True}


class CancellationControlRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_delivery_continues_until_cancel_reaches_cptr(self):
        from cptr.utils.chat_task import process_pending_chat_inputs

        queued = SimpleNamespace(
            id="chat-message-1",
            parent_id=None,
            role="user",
            content="STEERING_MARKER_1",
            model="model-1",
            meta={
                "queued": True,
                "control_message_id": "control-message-1",
                "control_task_id": "task-1",
            },
        )
        chat = SimpleNamespace(id="chat-1", meta={"last_model": "model-1"})
        combined = SimpleNamespace(id="combined-1")
        assistant = SimpleNamespace(id="assistant-1")
        control_store = SimpleNamespace(
            update_message=AsyncMock(),
            repoint_task_message=AsyncMock(),
        )

        with (
            patch("cptr.utils.chat_task.get_active_chat_ids", return_value=set()),
            patch(
                "cptr.models.ChatMessage.get_all_by_chat",
                new=AsyncMock(return_value=[queued]),
            ),
            patch(
                "cptr.models.ChatMessage.create",
                new=AsyncMock(side_effect=[combined, assistant]),
            ),
            patch("cptr.models.ChatMessage.update", new=AsyncMock()) as update_message,
            patch("cptr.models.Chat.get_by_id", new=AsyncMock(return_value=chat)),
            patch("cptr.models.Chat.update_current_message", new=AsyncMock()),
            patch(
                "cptr.utils.model_targets.resolve_model_target",
                new=AsyncMock(return_value=object()),
            ),
            patch("cptr.utils.chat_task.emit_to_user", new=AsyncMock()),
            patch("cptr.utils.chat_task.start_task") as start_task,
            patch("cptr.services.control_store.ControlTaskStore", return_value=control_store),
        ):
            await process_pending_chat_inputs(object(), "chat-1", "user-1", "/disposable")

        start_task.assert_called_once()
        self.assertEqual(start_task.call_args.kwargs["message_id"], "assistant-1")
        delivered_meta = update_message.await_args.kwargs["meta"]
        self.assertEqual(delivered_meta["delivery_status"], "DELIVERED")
        control_store.update_message.assert_awaited_once()
        self.assertEqual(control_store.update_message.await_args.kwargs["status"], "DELIVERED")

    def test_invalidate_messages_for_task_cancels_durable_control_rows(self):
        with (
            tempfile.TemporaryDirectory() as data_dir,
            tempfile.TemporaryDirectory() as workspace_dir,
        ):
            env = {**os.environ, "CPTR_DATA_DIR": data_dir, "CANCEL_WORKSPACE": workspace_dir}
            result = subprocess.run(
                [sys.executable, "-c", INVALIDATION_SCRIPT],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["invalidated"], 1)
        self.assertEqual(payload["status"], "CANCELLED")
        self.assertFalse(payload["queued"])
        self.assertEqual(payload["delivery_status"], "CANCELLED")

    async def test_direct_cancel_invalidates_controls_before_owned_execution_quiesces(self):
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
        events = []

        async def invalidate(task_id, *, now):
            events.append(("invalidate", task_id))
            return 1

        async def cancel_turn(message_id, *, timeout):
            events.append(("cancel", message_id))
            return True

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
            patch.object(service.store, "request_cancel", new=AsyncMock(return_value=True)),
            patch.object(
                service.store,
                "invalidate_messages_for_task",
                new=AsyncMock(side_effect=invalidate),
            ),
            patch.object(service.store, "finalize_cancel", new=AsyncMock(return_value=True)),
            patch("cptr.utils.chat_task.cancel_task", new=AsyncMock(side_effect=cancel_turn)),
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
            patch.object(service.store, "update", new=AsyncMock()),
        ):
            result = await service.cancel_task("task-1", user_id="user-1")

        self.assertEqual(events, [("invalidate", "task-1"), ("cancel", "message-1")])
        self.assertEqual(result["status"], "CANCELLED")
        self.assertTrue(result["cancelled"])

    async def test_terminal_cancelled_controls_are_not_replayed(self):
        from cptr.utils.chat_task import process_pending_chat_inputs

        cancelled = SimpleNamespace(
            id="chat-message-cancelled",
            parent_id=None,
            role="user",
            content="STEERING_MARKER_CANCELLED",
            model="model-1",
            meta={
                "queued": True,
                "delivery_status": "CANCELLED",
                "control_message_id": "control-message-1",
                "control_task_id": "task-1",
            },
        )

        with (
            patch("cptr.utils.chat_task.get_active_chat_ids", return_value=set()),
            patch(
                "cptr.models.ChatMessage.get_all_by_chat",
                new=AsyncMock(return_value=[cancelled]),
            ),
            patch("cptr.models.ChatMessage.create", new=AsyncMock()) as create_message,
            patch("cptr.utils.chat_task.start_task") as start_task,
        ):
            await process_pending_chat_inputs(object(), "chat-1", "user-1", "/disposable")

        create_message.assert_not_awaited()
        start_task.assert_not_called()

    async def test_autonomous_cancel_is_terminal_and_does_not_start_replacement_worker(self):
        store = InMemorySupervisorStore()
        agent = CancelAwareAgent()
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=AcceptingDirector())
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Cancel safely",
            acceptance_criteria=["The active worker is cancelled without replay"],
            model_id="model-1",
        )

        await supervisor.run_once(monitor.monitor_id)
        state = await store.get_monitor(monitor.monitor_id)
        state.scopes[0].steering_requests.append(
            {"control_message_id": "control-1", "intended_task_id": "task_1"}
        )
        await store.save_monitor(state)

        cancelled = await supervisor.cancel(monitor.monitor_id)
        rerun = await supervisor.run_once(monitor.monitor_id)

        self.assertEqual(cancelled.status, MonitorStatus.CANCELLED)
        self.assertEqual(rerun.status, MonitorStatus.CANCELLED)
        self.assertEqual(agent.cancelled, ["task_1"])
        self.assertEqual(len(agent.started), 1)


if __name__ == "__main__":
    unittest.main()
