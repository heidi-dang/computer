import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


SEED_SCRIPT = r"""
import asyncio
import os
from cptr.models import Chat, ControlTask, User, Workspace
from cptr.services.control_store import ControlTaskStore
from cptr.utils.db import init_db, get_db

async def main():
    await init_db()
    user_id = await User.create("delivery-test", "password-hash", role="user", created_at=1)
    workspace = await Workspace.upsert(user_id, os.environ["DELIVERY_WORKSPACE"], "delivery", {})
    chat = await Chat.create(user_id=user_id, title="delivery", meta={}, created_at=1)
    async with await get_db() as db:
        db.add(ControlTask(
            id="task_delivery",
            user_id=user_id,
            workspace_id=workspace.id,
            chat_id=chat.id,
            message_id="worker-message",
            status="RUNNING",
            prompt="worker",
            model_id="model",
            created_at=1,
            updated_at=1,
        ))
        await db.commit()
    store = ControlTaskStore()
    first = await store.enqueue_message(
        task_id="task_delivery",
        user_id=user_id,
        chat_id=chat.id,
        content="STEERING_MARKER_1",
        dedupe_key="retry-1",
        chat_message_id="chat-message-1",
        now=2,
    )
    second = await store.enqueue_message(
        task_id="task_delivery",
        user_id=user_id,
        chat_id=chat.id,
        content="STEERING_MARKER_1",
        dedupe_key="retry-1",
        chat_message_id="chat-message-duplicate",
        now=3,
    )
    assert first.id == second.id
    assert second.chat_message_id == "chat-message-1"
    assert await store.has_pending_control("task_delivery")
    await store.update_message(first.id, status="DELIVERED", target_message_id="worker-message", delivered_at=4, updated_at=4)
    await store.update_message(first.id, status="CONSUMED", consumed_at=5, updated_at=5)
    assert not await store.has_pending_control("task_delivery")
    current = await store.get_message(first.id)
    assert current.status == "CONSUMED"
    print(first.id)

asyncio.run(main())
"""

GENERATION_FENCE_SCRIPT = r"""
import asyncio
import os
from cptr.models import Chat, ControlTask, User, Workspace
from cptr.services.control_store import ControlTaskStore
from cptr.utils.db import init_db, get_db

async def main():
    await init_db()
    user_id = await User.create("generation-fence", "password-hash", role="user", created_at=1)
    workspace = await Workspace.upsert(user_id, os.environ["DELIVERY_WORKSPACE"], "delivery", {})
    chat = await Chat.create(user_id=user_id, title="delivery", meta={}, created_at=1)
    async with await get_db() as db:
        db.add(ControlTask(
            id="task_generation",
            user_id=user_id,
            workspace_id=workspace.id,
            chat_id=chat.id,
            message_id="worker-generation-0",
            status="RUNNING",
            prompt="worker",
            model_id="model",
            created_at=1,
            updated_at=1,
        ))
        await db.commit()
    store = ControlTaskStore()
    message = await store.enqueue_message(
        task_id="task_generation",
        user_id=user_id,
        chat_id=chat.id,
        content="STEERING_GENERATION_FENCE",
        dedupe_key="retry-generation",
        chat_message_id="chat-message-generation",
        now=2,
        intended_message_id="worker-generation-1",
    )
    assert await store.update_message(
        message.id,
        status="DELIVERED",
        target_message_id="worker-generation-1",
        delivered_at=3,
        updated_at=3,
    )
    wrong = await store.consume_message(
        message.id,
        task_id="task_generation",
        message_id_for_run="worker-generation-2",
        now=4,
    )
    current = await store.get_message(message.id)
    assert wrong is False
    assert current.status == "DELIVERED"
    assert current.consumed_message_id is None
    right = await store.consume_message(
        message.id,
        task_id="task_generation",
        message_id_for_run="worker-generation-1",
        now=5,
    )
    assert right is True
    current = await store.get_message(message.id)
    assert current.status == "CONSUMED"
    assert current.consumed_message_id == "worker-generation-1"

asyncio.run(main())
"""


class ControlMessageDeliveryTests(unittest.TestCase):
    def test_delivery_record_is_idempotent_and_survives_state_transitions(self):
        with (
            tempfile.TemporaryDirectory() as data_dir,
            tempfile.TemporaryDirectory() as workspace_dir,
        ):
            env = {**os.environ, "CPTR_DATA_DIR": data_dir, "DELIVERY_WORKSPACE": workspace_dir}
            result = subprocess.run(
                [sys.executable, "-c", SEED_SCRIPT],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertTrue(result.stdout.strip())
            with sqlite3.connect(os.path.join(data_dir, "app.db")) as connection:
                status, target, consumed_at = connection.execute(
                    "select status, target_message_id, consumed_at from control_messages"
                ).fetchone()
            self.assertEqual(status, "CONSUMED")
            self.assertEqual(target, "worker-message")
            self.assertEqual(consumed_at, 5)

    def test_control_message_can_only_be_consumed_by_delivered_generation(self):
        with (
            tempfile.TemporaryDirectory() as data_dir,
            tempfile.TemporaryDirectory() as workspace_dir,
        ):
            env = {**os.environ, "CPTR_DATA_DIR": data_dir, "DELIVERY_WORKSPACE": workspace_dir}
            result = subprocess.run(
                [sys.executable, "-c", GENERATION_FENCE_SCRIPT],
                check=False,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_idle_chat_delivers_queued_message_to_one_continuation_worker(self):
        import asyncio

        from cptr.utils.chat_task import process_pending_chat_inputs

        async def exercise():
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

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
