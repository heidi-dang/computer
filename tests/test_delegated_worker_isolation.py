import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from cptr.env import DIRECT_WORKER_MAX_PER_WORKSPACE
from cptr.routers.control import TaskExecutionPolicy
from cptr.services.agent_service import AgentService
from cptr.services.direct_coding_workers import CAPACITY_WORKER_STATUSES


class _FakeDb:
    def __init__(self, workspace):
        self.workspace = workspace

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, model, key):
        return self.workspace


class DelegatedWorkerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_bound_task_uses_exact_worktree_root_with_spaces(self):
        service = AgentService()
        service.store.create = AsyncMock()
        service.get_task = AsyncMock(return_value={"id": "task-result", "status": "RUNNING"})
        workspace = SimpleNamespace(
            id="workspace-1",
            user_id="user-1",
            path="/tmp/cross repo/canonical",
        )
        worker_root = Path("/tmp/cross repo/.cptr-worktrees/computer/worker-1")
        chat = SimpleNamespace(id="chat-1")
        user_message = SimpleNamespace(id="message-user")
        assistant_message = SimpleNamespace(id="message-assistant")

        with (
            patch(
                "cptr.services.agent_service.get_db",
                new=AsyncMock(return_value=_FakeDb(workspace)),
            ),
            patch(
                "cptr.services.agent_service.resolve_direct_worker_root",
                new=AsyncMock(return_value=worker_root),
            ) as resolve_root,
            patch("cptr.models.Chat.create", new=AsyncMock(return_value=chat)) as create_chat,
            patch(
                "cptr.models.ChatMessage.create",
                new=AsyncMock(side_effect=[user_message, assistant_message]),
            ),
            patch("cptr.models.Chat.update_current_message", new=AsyncMock()),
            patch(
                "cptr.utils.model_targets.resolve_model_target",
                new=AsyncMock(return_value=object()),
            ),
            patch("cptr.utils.chat_task.start_task", new=Mock()) as start_task,
            patch("cptr.services.live_events.safe_publish_task_event", new=AsyncMock()),
        ):
            result = await service.start_task(
                user_id="user-1",
                workspace_id="workspace-1",
                prompt="edit only the assigned worker",
                model_id="agent:hermes/model",
                execution_policy={
                    "isolation": "existing-worktree",
                    "worker_id": "worker-1",
                    "allow_file_writes": True,
                    "allow_commands": True,
                },
                request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            )

        self.assertEqual(result["status"], "RUNNING")
        resolve_root.assert_awaited_once_with(
            user_id="user-1",
            workspace_id="workspace-1",
            worker_id="worker-1",
        )
        self.assertEqual(start_task.call_args.kwargs["workspace"], str(worker_root))
        meta = create_chat.await_args.kwargs["meta"]
        self.assertEqual(meta["workspace"], str(worker_root))
        self.assertEqual(meta["canonical_workspace"], workspace.path)
        self.assertEqual(meta["worker_id"], "worker-1")
        self.assertEqual(meta["isolation"], "existing-worktree")

    async def test_existing_worktree_isolation_without_worker_id_fails_closed(self):
        service = AgentService()
        workspace = SimpleNamespace(
            id="workspace-1",
            user_id="user-1",
            path="/tmp/cross repo/canonical",
        )
        with patch(
            "cptr.services.agent_service.get_db",
            new=AsyncMock(return_value=_FakeDb(workspace)),
        ):
            with self.assertRaisesRegex(ValueError, "requires worker_id"):
                await service.start_task(
                    user_id="user-1",
                    workspace_id="workspace-1",
                    prompt="must be isolated",
                    model_id="agent:hermes/model",
                    execution_policy={"isolation": "existing-worktree"},
                    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
                )

    async def test_review_reads_worker_diff_instead_of_canonical_checkout(self):
        service = AgentService()
        service.get_task = AsyncMock(
            return_value={
                "id": "task-1",
                "workspace_id": "workspace-1",
                "chat_id": "chat-1",
                "status": "REVIEW_REQUIRED",
                "review": {"status": "REQUIRED"},
            }
        )
        service.get_diff = AsyncMock(return_value={"is_repo": True, "files": [{"path": "fix.py"}]})
        chat = SimpleNamespace(meta={"worker_id": "worker-1"})

        with patch("cptr.models.Chat.get_by_id", new=AsyncMock(return_value=chat)):
            result = await service.get_task_review("task-1", user_id="user-1")

        service.get_diff.assert_awaited_once_with(
            "workspace-1",
            user_id="user-1",
            worker_id="worker-1",
        )
        self.assertEqual(result["diff"]["files"][0]["path"], "fix.py")


class DelegatedExecutionPolicyTests(unittest.TestCase):
    def test_existing_worktree_fields_are_preserved(self):
        policy = TaskExecutionPolicy(
            isolation="existing-worktree",
            worker_id="worker-1",
        )
        dumped = policy.model_dump(exclude_none=True)
        self.assertEqual(dumped["isolation"], "existing-worktree")
        self.assertEqual(dumped["worker_id"], "worker-1")

    def test_unknown_execution_policy_fields_are_rejected_not_silently_ignored(self):
        with self.assertRaises(ValidationError):
            TaskExecutionPolicy(isolaton="existing-worktree")  # type: ignore[call-arg]

    def test_default_worker_capacity_supports_fifteen_parallel_mutation_lanes(self):
        if "CPTR_DIRECT_WORKER_MAX_PER_WORKSPACE" not in os.environ:
            self.assertGreaterEqual(DIRECT_WORKER_MAX_PER_WORKSPACE, 15)
        self.assertNotIn("INTEGRATED", CAPACITY_WORKER_STATUSES)
        self.assertTrue({"READY", "WORKING", "RUNNING"} <= CAPACITY_WORKER_STATUSES)


if __name__ == "__main__":
    unittest.main()
