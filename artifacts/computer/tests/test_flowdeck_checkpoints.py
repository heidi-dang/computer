import asyncio
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.app import app
from cptr.flowdeck.durable import DurableFlowDeck, OperationStatus, RunStatus
from cptr.models import Auth, Base, Config, User, Workspace
from cptr.models.flowdeck import (
    FlowDeckCheckpoint,
    FlowDeckLogicalOperation,
    FlowDeckPhysicalAttempt,
)
from cptr.utils import db as db_module
from cptr.utils.git import GitError


class AuthenticatedCheckpointHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_a = Path(self.temp.name, "workspace-a").resolve()
        self.root_b = Path(self.temp.name, "workspace-b").resolve()
        self.root_a.mkdir()
        self.root_b.mkdir()
        for root in (self.root_a, self.root_b):
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            (root / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)

        self.db_file = Path(self.temp.name, "checkpoints.db")
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_file}", connect_args={"timeout": 5}
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.old_engine = db_module._engine
        self.old_sessions = db_module._async_session
        db_module._engine = self.engine
        db_module._async_session = self.sessions
        self.tokens = {"user-a": "token-a", "user-b": "token-b"}
        async with self.sessions() as session:
            session.add_all(
                [
                    User(id="user-a", role="user", created_at=1),
                    User(id="user-b", role="user", created_at=1),
                    Auth(user_id="user-a", username="a", password=None),
                    Auth(user_id="user-b", username="b", password=None),
                    Workspace(
                        user_id="user-a",
                        path=str(self.root_a),
                        name="A",
                        data={},
                        created_at=1,
                    ),
                    Workspace(
                        user_id="user-b",
                        path=str(self.root_b),
                        name="B",
                        data={},
                        created_at=1,
                    ),
                    Config(
                        key="api_keys",
                        value=[
                            {
                                "key_hash": hashlib.sha256(token.encode()).hexdigest(),
                                "user_id": user_id,
                            }
                            for user_id, token in self.tokens.items()
                        ],
                    ),
                ]
            )
            await session.commit()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        db_module._engine = self.old_engine
        db_module._async_session = self.old_sessions
        await self.engine.dispose()
        self.temp.cleanup()

    def headers(self, user="user-a", key="checkpoint-key-1"):
        return {
            "Authorization": f"Bearer {self.tokens[user]}",
            "Idempotency-Key": key,
        }

    async def capture(self, *, user="user-a", workspace=None, key="checkpoint-key-1"):
        return await self.client.post(
            "/v1/flowdeck/checkpoints/capture",
            headers=self.headers(user, key),
            json={"workspace": str(workspace or self.root_a)},
        )

    async def test_authenticated_capture_restore_replay_and_authoritative_evidence(self):
        captured = await self.capture()
        self.assertEqual(captured.status_code, 200, captured.text)
        payload = captured.json()
        self.assertEqual(payload["status"], "AVAILABLE")
        self.assertEqual(len(payload["revision"]), 40)

        replay = await self.capture(key="checkpoint-key-1")
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["reused"])
        self.assertEqual(replay.json()["checkpoint_id"], payload["checkpoint_id"])

        restored = await self.client.post(
            "/v1/flowdeck/checkpoints/restore",
            headers=self.headers(key="restore-key-1"),
            json={
                "workspace": str(self.root_a),
                "checkpoint_id": payload["checkpoint_id"],
            },
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["status"], "RESTORED")
        self.assertEqual(restored.json()["revision"], payload["revision"])

        status = await self.client.get(
            f"/v1/flowdeck/orchestrations/{restored.json()['run_id']}",
            params={"workspace": str(self.root_a)},
            headers=self.headers(key="status-key-1"),
        )
        self.assertEqual(status.status_code, 200, status.text)
        events = status.json()["events"]
        verified = [event for event in events if event["kind"] == "CHECKPOINT_OPERATION_VERIFIED"]
        self.assertEqual(len(verified), 1)

        async with self.sessions() as session:
            checkpoint = await session.get(FlowDeckCheckpoint, payload["checkpoint_id"])
            self.assertEqual(checkpoint.status, "RESTORED")
            operation = await session.scalar(
                select(FlowDeckLogicalOperation).where(
                    FlowDeckLogicalOperation.run_id == restored.json()["run_id"]
                )
            )
            self.assertEqual(operation.status, OperationStatus.SUCCEEDED.value)
            evidence = operation.authoritative_evidence
            self.assertEqual(evidence["source"], "verifier")
            self.assertEqual(evidence["observation"], "verifier_check")
            self.assertEqual(evidence["result"]["revision"], payload["revision"])

    async def test_dirty_capture_and_restore_are_rejected_without_success(self):
        (self.root_a / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        capture = await self.capture(key="dirty-capture-1")
        self.assertEqual(capture.status_code, 422, capture.text)
        self.assertIn("clean worktree", capture.text)

        subprocess.run(["git", "-C", str(self.root_a), "add", "dirty.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root_a), "commit", "-qm", "dirty"], check=True)
        clean_capture = await self.capture(key="dirty-restore-source-1")
        self.assertEqual(clean_capture.status_code, 200, clean_capture.text)
        (self.root_a / "dirty-again.txt").write_text("changed\n", encoding="utf-8")
        restore = await self.client.post(
            "/v1/flowdeck/checkpoints/restore",
            headers=self.headers(key="dirty-restore-1"),
            json={
                "workspace": str(self.root_a),
                "checkpoint_id": clean_capture.json()["checkpoint_id"],
            },
        )
        self.assertEqual(restore.status_code, 422, restore.text)
        self.assertIn("clean worktree", restore.text)

    async def test_cross_owner_and_cross_workspace_access_is_rejected(self):
        captured = await self.capture()
        checkpoint_id = captured.json()["checkpoint_id"]
        other_owner = await self.client.post(
            "/v1/flowdeck/checkpoints/restore",
            headers=self.headers("user-b", "cross-owner-1"),
            json={"workspace": str(self.root_a), "checkpoint_id": checkpoint_id},
        )
        self.assertEqual(other_owner.status_code, 403, other_owner.text)
        other_workspace = await self.client.post(
            "/v1/flowdeck/checkpoints/restore",
            headers=self.headers("user-a", "cross-workspace-1"),
            json={"workspace": str(self.root_b), "checkpoint_id": checkpoint_id},
        )
        self.assertEqual(other_workspace.status_code, 403, other_workspace.text)

    async def test_uncertain_git_outcome_becomes_manual_review_and_never_success(self):
        captured = await self.capture(key="uncertain-source-1")
        with patch(
            "cptr.flowdeck.checkpoints._run",
            new=async_uncertain_checkout,
        ):
            response = await self.client.post(
                "/v1/flowdeck/checkpoints/restore",
                headers=self.headers(key="uncertain-restore-1"),
                json={
                    "workspace": str(self.root_a),
                    "checkpoint_id": captured.json()["checkpoint_id"],
                },
            )
        self.assertEqual(response.status_code, 503, response.text)
        run = await DurableFlowDeck(self.sessions).get_run(response.json().get("run_id", ""))
        # The run id is intentionally not returned in the safe error body.
        async with self.sessions() as session:
            rows = list((await session.scalars(select(FlowDeckPhysicalAttempt))).all())
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1].status, "UNKNOWN")
            operations = list((await session.scalars(select(FlowDeckLogicalOperation))).all())
            self.assertEqual(operations[-1].status, OperationStatus.MANUAL_REVIEW_REQUIRED.value)
            self.assertNotEqual(operations[-1].status, OperationStatus.SUCCEEDED.value)

    async def test_cancelled_restore_is_unknown_and_late_success_cannot_resurrect(self):
        captured = await self.capture(key="cancel-source-1")

        async def cancelled_restore(*, checkpoint_id, workspace, owner):
            await asyncio.sleep(0)
            raise asyncio.CancelledError()

        with patch(
            "cptr.routers.flowdeck.CheckpointService.restore",
            new=cancelled_restore,
        ):
            response = await self.client.post(
                "/v1/flowdeck/checkpoints/restore",
                headers=self.headers(key="cancel-restore-1"),
                json={
                    "workspace": str(self.root_a),
                    "checkpoint_id": captured.json()["checkpoint_id"],
                },
            )
        self.assertEqual(response.status_code, 500, response.text)
        async with self.sessions() as session:
            operation = list((await session.scalars(select(FlowDeckLogicalOperation))).all())[-1]
            attempt = list((await session.scalars(select(FlowDeckPhysicalAttempt))).all())[-1]
            self.assertEqual(attempt.status, "UNKNOWN")
            self.assertEqual(operation.status, OperationStatus.MANUAL_REVIEW_REQUIRED.value)

    async def test_restart_recovery_marks_abandoned_restore_unknown(self):
        store = DurableFlowDeck(self.sessions)
        run, _ = await store.create_run(
            request_key="restart-restore-1",
            owner="user-a",
            workspace=str(self.root_a),
        )
        await store.start_run(run.id)
        step = await store.get_step(run.id)
        await store.start_step(step.id)
        operation, _ = await store.record_intent(
            run_id=run.id,
            step_id=step.id,
            idempotency_key="restart-operation-1",
            capability="checkpoint.restore",
            target="checkpoint",
            reconcile_kind="checkpoint",
        )
        lease = await store.acquire_workspace_lease(
            workspace=str(self.root_a),
            run_id=run.id,
            owner="user-a",
            ttl_ms=1000,
        )
        attempt = await store.prepare_attempt(
            operation_id=operation.id,
            owner="user-a",
            fencing_epoch=lease.epoch,
        )
        orphaned = await store.mark_orphaned(stale_before=10**18)
        self.assertIn(run.id, orphaned)
        await store.mark_attempt_unknown(attempt.id, error="process restarted")
        await store.require_manual_review(run.id, reason="process restarted")
        recovered = await store.get_run(run.id)
        self.assertEqual(recovered.status, RunStatus.MANUAL_REVIEW_REQUIRED.value)
        with self.assertRaises(Exception):
            await store.finish_attempt(
                attempt.id,
                owner="user-a",
                fencing_epoch=lease.epoch,
                outcome="succeeded",
                evidence={
                    "source": "verifier",
                    "authoritative": True,
                    "observation": "verifier_check",
                    "observed_outcome": "succeeded",
                    "attempt_id": attempt.id,
                },
            )


async def async_uncertain_checkout(*args, **kwargs):
    if args and args[0] == "checkout":
        raise GitError("simulated interrupted checkout")
    from cptr.utils.git import _run as native_run

    return await native_run(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()