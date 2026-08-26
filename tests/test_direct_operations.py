import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.models import Workspace
from cptr.models.users import User
from cptr.routers.coding import CommandRequest, start_workspace_command
from cptr.services.control_store import SqlSupervisorStore
from cptr.services.direct_executor import DirectExecutorManager, InvalidSshProfile
from cptr.services.direct_operations import (
    DirectOperationStore,
    IdempotencyConflict,
    WorkspaceBusy,
)
from cptr.utils.db import init_db


class DurableDirectOperationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.store = DirectOperationStore()
        self.user_id = await User.create(
            username=f"direct-operation-{uuid.uuid4().hex}@test.local",
            password_hash="not-used-in-test",
            created_at=int(time.time()),
        )
        self.tempdir = tempfile.TemporaryDirectory()
        workspace = await Workspace.upsert(
            self.user_id,
            self.tempdir.name,
            "direct-operation-test",
            {},
        )
        self.workspace_id = workspace.id

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_idempotent_create_replays_only_identical_request(self):
        request = {
            "kind": "WRITE_FILE",
            "path": "src/example.py",
            "content": "value = 1\n",
            "expected_revision": "MISSING",
        }
        first, replayed = await self.store.create_or_replay(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            kind="WRITE_FILE",
            request=request,
            idempotency_key="chatgpt-turn-1-write",
            expected_revision="MISSING",
        )
        second, replayed_second = await self.store.create_or_replay(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            kind="WRITE_FILE",
            request=request,
            idempotency_key="chatgpt-turn-1-write",
            expected_revision="MISSING",
        )

        self.assertFalse(replayed)
        self.assertTrue(replayed_second)
        self.assertEqual(first.id, second.id)

        with self.assertRaises(IdempotencyConflict):
            await self.store.create_or_replay(
                user_id=self.user_id,
                workspace_id=self.workspace_id,
                kind="WRITE_FILE",
                request={**request, "content": "value = 2\n"},
                idempotency_key="chatgpt-turn-1-write",
                expected_revision="MISSING",
            )

    async def test_fenced_workspace_lease_prevents_concurrent_owner(self):
        first = await self.store.acquire_workspace_lease(
            workspace_id=self.workspace_id,
            holder_type="DIRECT_OPERATION",
            holder_id="operation-one",
        )
        with self.assertRaises(WorkspaceBusy):
            await self.store.acquire_workspace_lease(
                workspace_id=self.workspace_id,
                holder_type="DIRECT_OPERATION",
                holder_id="operation-two",
            )
        await self.store.release_workspace_lease(
            workspace_id=self.workspace_id,
            holder_type="DIRECT_OPERATION",
            holder_id="operation-one",
            fencing_token=first.fencing_token,
        )
        second = await self.store.acquire_workspace_lease(
            workspace_id=self.workspace_id,
            holder_type="DIRECT_OPERATION",
            holder_id="operation-two",
        )
        self.assertGreater(second.fencing_token, first.fencing_token)

    async def test_autonomous_monitor_and_direct_operation_leases_fence_each_other(self):
        monitor_store = SqlSupervisorStore()
        direct_lease = await self.store.acquire_workspace_lease(
            workspace_id=self.workspace_id,
            holder_type="DIRECT_OPERATION",
            holder_id="direct-operation",
        )
        self.assertFalse(await monitor_store.claim_workspace(self.workspace_id, "monitor-one"))
        await self.store.release_workspace_lease(
            workspace_id=self.workspace_id,
            holder_type="DIRECT_OPERATION",
            holder_id="direct-operation",
            fencing_token=direct_lease.fencing_token,
        )
        self.assertTrue(await monitor_store.claim_workspace(self.workspace_id, "monitor-one"))
        with self.assertRaises(WorkspaceBusy):
            await self.store.acquire_workspace_lease(
                workspace_id=self.workspace_id,
                holder_type="DIRECT_OPERATION",
                holder_id="second-direct-operation",
            )
        await monitor_store.release_workspace(self.workspace_id, "monitor-one")

    async def test_restart_recovery_never_infers_success(self):
        operation, _ = await self.store.create_or_replay(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            kind="WRITE_FILE",
            request={"kind": "WRITE_FILE", "path": "a.py", "content": "x", "expected_revision": "MISSING"},
            idempotency_key="recovery-test",
            expected_revision="MISSING",
        )
        running = await self.store.transition(
            operation.id,
            expected_states={"REQUESTED"},
            state="RUNNING",
            event_type="STARTED",
        )
        self.assertIsNotNone(running)
        count = await self.store.reconcile_after_restart()
        recovered = await self.store.get(operation.id, self.user_id)
        self.assertGreaterEqual(count, 1)
        self.assertEqual(recovered.state, "ORPHANED")
        self.assertEqual(recovered.public_error_code, "RECOVERY_REQUIRED")

    async def test_cancel_has_durable_requested_then_terminal_state(self):
        operation, _ = await self.store.create_or_replay(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            kind="WRITE_FILE",
            request={"kind": "WRITE_FILE", "path": "a.py", "content": "x", "expected_revision": "MISSING"},
            idempotency_key="cancel-test",
            expected_revision="MISSING",
        )
        requested, applied = await self.store.request_cancel(
            operation.id,
            reason="user changed direction",
            idempotency_key="cancel-request-1",
        )
        self.assertTrue(applied)
        self.assertEqual(requested.state, "CANCEL_REQUESTED")
        replayed, replay_applied = await self.store.request_cancel(
            operation.id,
            reason="user changed direction",
            idempotency_key="cancel-request-1",
        )
        self.assertFalse(replay_applied)
        self.assertEqual(replayed.state, "CANCEL_REQUESTED")
        with self.assertRaises(IdempotencyConflict):
            await self.store.request_cancel(
                operation.id,
                reason="a different reason",
                idempotency_key="cancel-request-1",
            )
        cancelled = await self.store.complete_cancel(operation.id)
        self.assertEqual(cancelled.state, "CANCELLED")

    async def test_approval_decision_is_durable_idempotent_and_replays_without_transition(self):
        operation, _ = await self.store.create_or_replay(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            kind="RUN_CODE_BLOCK",
            request={"kind": "RUN_CODE_BLOCK", "language": "python", "code": "print(1)"},
            idempotency_key="approval-operation",
            expected_revision=None,
        )
        await self.store.create_approval(
            operation.id,
            request_digest=operation.request_digest,
            reason="code execution requested",
        )
        first, first_applied = await self.store.decide_approval(
            operation.id,
            approved=True,
            decided_by=self.user_id,
            idempotency_key="approval-decision-1",
        )
        replayed, replay_applied = await self.store.decide_approval(
            operation.id,
            approved=True,
            decided_by=self.user_id,
            idempotency_key="approval-decision-1",
        )
        self.assertTrue(first_applied)
        self.assertFalse(replay_applied)
        self.assertEqual(first.state, "QUEUED")
        self.assertEqual(replayed.state, "QUEUED")
        events = await self.store.list_events(operation.id)
        self.assertEqual(sum(item.event_type == "APPROVAL_DECIDED" for item in events), 1)

    async def test_compound_event_cursor_does_not_skip_same_millisecond_events(self):
        with patch("cptr.services.direct_operations.now_ms", return_value=42):
            operation, _ = await self.store.create_or_replay(
                user_id=self.user_id,
                workspace_id=self.workspace_id,
                kind="WRITE_FILE",
                request={
                    "kind": "WRITE_FILE",
                    "path": "cursor.py",
                    "content": "x",
                    "expected_revision": "MISSING",
                },
                idempotency_key="event-cursor",
                expected_revision="MISSING",
            )
            await self.store.transition(
                operation.id,
                expected_states={"REQUESTED"},
                state="SUCCEEDED",
                event_type="FILE_WRITTEN",
            )
        first_page = await self.store.list_events(operation.id, limit=1)
        cursor = f"{first_page[0].created_at}:{first_page[0].id}"
        second_page = await self.store.list_events(operation.id, cursor=cursor, limit=10)
        self.assertEqual(len(first_page), 1)
        self.assertEqual(len(second_page), 1)
        self.assertNotEqual(first_page[0].id, second_page[0].id)


class DirectSandboxRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_python_code_block_runs_in_namespace_and_only_writes_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = DirectExecutorManager(DirectOperationStore())
            result = await manager._run_code_block(
                {
                    "language": "python",
                    "code": (
                        "from pathlib import Path\n"
                        "Path('sandbox-output.txt').write_text('workspace-only')\n"
                        "print('host-home-visible=' + str(Path('/home').exists()))\n"
                    ),
                },
                temporary_directory,
                "sandbox-smoke",
            )

            self.assertEqual(result["executor"], "sandbox")
            self.assertEqual(result["exit_code"], 0)
            self.assertFalse(result["timed_out"])
            self.assertIn("host-home-visible=False", result["stdout"])
            self.assertEqual(
                Path(temporary_directory, "sandbox-output.txt").read_text(), "workspace-only"
            )
            javascript_result = await manager._run_code_block(
                {"language": "javascript", "code": "console.log('node-sandbox-ok')"},
                temporary_directory,
                "sandbox-node-smoke",
            )
            self.assertEqual(javascript_result["exit_code"], 0)
            self.assertIn("node-sandbox-ok", javascript_result["stdout"])


class DirectSshExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_ssh_uses_only_configured_profile_action_and_fixed_argv(self):
        class CompletedProcess:
            returncode = 0

            async def communicate(self):
                return b"ssh-output", b""

        with tempfile.TemporaryDirectory() as temporary_directory:
            identity_file = Path(temporary_directory, "id_ed25519")
            known_hosts_file = Path(temporary_directory, "known_hosts")
            identity_file.write_text("not-a-real-key")
            known_hosts_file.write_text("example.internal ssh-ed25519 test")
            profiles = {
                "production": {
                    "host": "example.internal",
                    "user": "deployer",
                    "port": 2222,
                    "identity_file": str(identity_file),
                    "known_hosts_file": str(known_hosts_file),
                    "actions": {"status": "systemctl is-system-running"},
                }
            }
            manager = DirectExecutorManager(DirectOperationStore())
            with (
                patch.dict(os.environ, {"CPTR_DIRECT_SSH_PROFILES_JSON": json.dumps(profiles)}),
                patch(
                    "cptr.services.direct_executor.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=CompletedProcess()),
                ) as spawn,
            ):
                result = await manager._run_ssh(
                    {"ssh_profile": "production", "ssh_action": "status"}, "ssh-smoke"
                )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["profile_id"], "production")
        self.assertEqual(result["action_id"], "status")
        argv = spawn.await_args.args
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-F", argv)
        self.assertIn("/dev/null", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("deployer@example.internal", argv)
        self.assertEqual(argv[-1], "systemctl is-system-running")
        self.assertNotIn(str(identity_file), result.values())
        self.assertNotIn(str(known_hosts_file), result.values())

    async def test_ssh_rejects_missing_profile_before_any_subprocess_is_started(self):
        manager = DirectExecutorManager(DirectOperationStore())
        with (
            patch.dict(os.environ, {"CPTR_DIRECT_SSH_PROFILES_JSON": "{}"}),
            patch(
                "cptr.services.direct_executor.asyncio.create_subprocess_exec", new=AsyncMock()
            ) as spawn,
            self.assertRaises(InvalidSshProfile),
        ):
            await manager._run_ssh(
                {"ssh_profile": "missing", "ssh_action": "status"}, "ssh-profile-missing"
            )

        spawn.assert_not_awaited()


class LegacyDirectCodingRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_shell_endpoint_is_retired_before_runner_is_reached(self):
        request = SimpleNamespace()
        with self.assertRaises(HTTPException) as retired:
            await start_workspace_command(
                request,
                "workspace",
                CommandRequest(command="python3 -c 'import os'"),
            )
        self.assertEqual(retired.exception.status_code, 410)


if __name__ == "__main__":
    unittest.main()


class DurableDirectOperationHttpFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from cptr.routers.direct_operations import router as direct_operations_router

        await init_db()
        self.user_id = await User.create(
            username=f"direct-http-{uuid.uuid4().hex}@test.local",
            password_hash="not-used-in-test",
            created_at=int(time.time()),
        )
        self.tempdir = tempfile.TemporaryDirectory()
        Path(self.tempdir.name, "example.py").write_text("value = 1\n", encoding="utf-8")
        self.workspace = await Workspace.upsert(
            self.user_id,
            self.tempdir.name,
            "direct-http-test",
            {},
        )
        self.token = f"direct-http-{uuid.uuid4().hex}"
        self.key = {
            "key_hash": __import__("hashlib").sha256(self.token.encode()).hexdigest(),
            "user_id": self.user_id,
            "scopes": ["direct:inspect", "direct:mutate", "direct:execute", "direct:approve"],
        }
        self.app = FastAPI()
        self.app.include_router(direct_operations_router)
        self.TestClient = TestClient

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_authenticated_revisioned_edit_is_idempotent_and_stale_writes_are_rejected(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        with (
            patch(
                "cptr.services.control_auth._get_api_keys",
                new=AsyncMock(return_value=[self.key]),
            ),
            self.TestClient(self.app) as client,
        ):
            read = client.post(
                f"/api/control/v2/workspaces/{self.workspace.id}/inspect/read",
                headers=headers,
                json={"path": "example.py"},
            )
            self.assertEqual(read.status_code, 200)
            revision = read.json()["revision"]

            payload = {
                "kind": "EDIT_FILE",
                "path": "example.py",
                "target": "1",
                "replacement": "2",
                "expected_revision": revision,
                "idempotency_key": "turn-1-edit",
            }
            edit = client.post(
                f"/api/control/v2/workspaces/{self.workspace.id}/operations",
                headers=headers,
                json=payload,
            )
            replay = client.post(
                f"/api/control/v2/workspaces/{self.workspace.id}/operations",
                headers=headers,
                json=payload,
            )
            stale = client.post(
                f"/api/control/v2/workspaces/{self.workspace.id}/operations",
                headers=headers,
                json={
                    "kind": "WRITE_FILE",
                    "path": "example.py",
                    "content": "stale\n",
                    "expected_revision": revision,
                    "idempotency_key": "turn-1-stale-write",
                },
            )
            action = client.post(
                f"/api/control/v2/workspaces/{self.workspace.id}/operations",
                headers=headers,
                json={
                    "kind": "RUN_ACTION",
                    "action": "typecheck",
                    "idempotency_key": "turn-1-action",
                },
            )

        self.assertEqual(edit.status_code, 200)
        self.assertEqual(edit.json()["state"], "SUCCEEDED")
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(Path(self.tempdir.name, "example.py").read_text(encoding="utf-8"), "value = 2\n")
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.json()["state"], "REJECTED")
        self.assertEqual(stale.json()["error_code"], "REVISION_CONFLICT")
        self.assertEqual(action.status_code, 200)
        self.assertEqual(action.json()["error_code"], "SANDBOX_EXECUTOR_UNAVAILABLE")

    async def test_code_block_and_ssh_require_durable_approval_before_any_executor_runs(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        with (
            patch(
                "cptr.services.control_auth._get_api_keys",
                new=AsyncMock(return_value=[self.key]),
            ),
            self.TestClient(self.app) as client,
        ):
            code_block = client.post(
                f"/api/control/v2/workspaces/{self.workspace.id}/operations",
                headers=headers,
                json={
                    "kind": "RUN_CODE_BLOCK",
                    "language": "python",
                    "code": "print('never run without approval')",
                    "idempotency_key": "turn-code-block",
                },
            )
            ssh_operation = client.post(
                f"/api/control/v2/workspaces/{self.workspace.id}/operations",
                headers=headers,
                json={
                    "kind": "SSH_EXECUTE",
                    "ssh_profile": "production",
                    "ssh_action": "status",
                    "idempotency_key": "turn-ssh",
                },
            )
            code_approval = client.post(
                f"/api/control/v2/operations/{code_block.json()['operation_id']}/approval",
                headers=headers,
                json={"approved": True, "idempotency_key": "turn-code-approve"},
            )
            ssh_approval = client.post(
                f"/api/control/v2/operations/{ssh_operation.json()['operation_id']}/approval",
                headers=headers,
                json={"approved": True, "idempotency_key": "turn-ssh-approve"},
            )

        self.assertEqual(code_block.status_code, 200)
        self.assertEqual(code_block.json()["state"], "WAITING_APPROVAL")
        self.assertEqual(ssh_operation.status_code, 200)
        self.assertEqual(ssh_operation.json()["state"], "WAITING_APPROVAL")
        self.assertEqual(code_approval.status_code, 200)
        self.assertEqual(code_approval.json()["state"], "REJECTED")
        self.assertEqual(code_approval.json()["error_code"], "SANDBOX_EXECUTOR_UNAVAILABLE")
        self.assertEqual(ssh_approval.status_code, 200)
        self.assertEqual(ssh_approval.json()["state"], "REJECTED")
        self.assertEqual(ssh_approval.json()["error_code"], "SANDBOX_EXECUTOR_UNAVAILABLE")


class DurableDirectOperationInputBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_command_field_is_rejected_by_structured_action_schema(self):
        from pydantic import ValidationError

        from cptr.routers.direct_operations import OperationRequest

        with self.assertRaises(ValidationError):
            OperationRequest(
                kind="RUN_ACTION",
                action="typecheck",
                idempotency_key="turn-action",
                command="python3 -c 'import os'",
            )
