import asyncio
import os
import unittest
from tempfile import NamedTemporaryFile

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.flowdeck.durable import (
    ApprovalStatus,
    DurableFlowDeck,
    LifecycleError,
    OperationStatus,
    RunStatus,
    StaleWriterError,
)
from cptr.models.base import Base


class DurableFlowDeckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with NamedTemporaryFile(suffix=".db", delete=False) as db_file:
            self.db_path = db_file.name
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_path}",
            connect_args={"timeout": 5},
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = 1_000
        self.store = DurableFlowDeck(self.sessions, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.db_path)

    async def _run_and_intent(self, request_key="request-1"):
        run, created = await self.store.create_run(
            request_key=request_key,
            owner="worker-a",
            workspace="/workspace",
        )
        self.assertTrue(created)
        await self.store.start_run(run.id, now=self.now)
        operation, created = await self.store.record_intent(
            run_id=run.id,
            idempotency_key="operation-1",
            capability="write_files",
            target="workspace.txt",
            reconcile_kind="file_hash",
            now=self.now,
        )
        self.assertTrue(created)
        return run, operation

    async def test_duplicate_request_and_intent_reuse_stable_ids(self):
        run, operation = await self._run_and_intent()
        duplicate_run, run_created = await self.store.create_run(
            request_key="request-1",
            owner="worker-a",
            workspace="/workspace",
        )
        duplicate_operation, operation_created = await self.store.record_intent(
            run_id=run.id,
            idempotency_key="operation-1",
            capability="write_files",
            target="workspace.txt",
            reconcile_kind="file_hash",
            now=self.now,
        )
        self.assertFalse(run_created)
        self.assertFalse(operation_created)
        self.assertEqual(run.id, duplicate_run.id)
        self.assertEqual(operation.id, duplicate_operation.id)

    async def test_approval_is_durable_but_never_execution_authority(self):
        _, operation = await self._run_and_intent()
        approval, created = await self.store.request_approval(
            operation_id=operation.id,
            capability="write_files",
            now=self.now,
        )
        self.assertTrue(created)
        await self.store.resolve_approval(
            approval.id,
            status=ApprovalStatus.APPROVED,
            resolved_by="human-reviewer",
            evidence={"source": "human", "authoritative": True},
            now=self.now + 1,
        )
        duplicate, duplicate_created = await self.store.request_approval(
            operation_id=operation.id,
            capability="write_files",
            now=self.now + 2,
        )
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.id, approval.id)
        self.assertEqual(duplicate.status, ApprovalStatus.APPROVED.value)
        refreshed_run, _ = await self.store.create_run(
            request_key="request-1",
            owner="worker-a",
            workspace="/workspace",
        )
        self.assertEqual(refreshed_run.status, RunStatus.RUNNING.value)

    async def test_intent_precedes_attempt_and_crash_becomes_unknown(self):
        run, operation = await self._run_and_intent()
        lease = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=100,
            now=self.now,
        )
        attempt = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now,
        )
        await self.store.mark_attempt_unknown(attempt.id, now=self.now + 1)

        with self.assertRaises(LifecycleError):
            await self.store.prepare_attempt(
                operation_id=operation.id,
                owner="worker-a",
                fencing_epoch=lease.epoch,
                now=self.now + 1,
            )

        status = await self.store.reconcile_operation(
            operation.id,
            outcome="succeeded",
            evidence={"source": "specialist", "authoritative": True},
            now=self.now + 2,
        )
        self.assertEqual(status, OperationStatus.MANUAL_REVIEW_REQUIRED)

    async def test_terminal_attempt_cannot_be_reclassified_as_unknown(self):
        run, operation = await self._run_and_intent()
        lease = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=100,
            now=self.now,
        )
        attempt = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now,
        )
        await self.store.finish_attempt(
            attempt.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            outcome="succeeded",
            evidence={
                "source": "runtime",
                "authoritative": True,
                "observation": "native_loop_return",
                "observed_outcome": "succeeded",
                "attempt_id": attempt.id,
            },
            now=self.now + 1,
        )
        with self.assertRaises(LifecycleError):
            await self.store.mark_attempt_unknown(attempt.id, now=self.now + 2)

    async def test_stale_physical_attempt_cannot_publish_success(self):
        run, operation = await self._run_and_intent()
        lease = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=100,
            now=self.now,
        )
        stale_attempt = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now,
        )
        await self.store.finish_attempt(
            stale_attempt.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            outcome="failed",
            evidence={
                "source": "runtime",
                "authoritative": True,
                "observation": "native_loop_return",
                "observed_outcome": "failed",
                "attempt_id": stale_attempt.id,
            },
            now=self.now + 1,
        )
        current_attempt = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now + 2,
        )
        with self.assertRaises(LifecycleError):
            await self.store.finish_attempt(
                stale_attempt.id,
                owner="worker-a",
                fencing_epoch=lease.epoch,
                outcome="succeeded",
                evidence={
                    "source": "runtime",
                    "authoritative": True,
                    "observation": "native_loop_return",
                    "observed_outcome": "succeeded",
                    "attempt_id": stale_attempt.id,
                },
                now=self.now + 3,
            )
        self.assertEqual(current_attempt.attempt_no, 2)

    async def test_manual_review_is_terminal_and_closes_run_safely(self):
        run, operation = await self._run_and_intent()
        lease = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=100,
            now=self.now,
        )
        attempt = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now,
        )
        await self.store.mark_attempt_unknown(attempt.id, now=self.now + 1)
        status = await self.store.reconcile_operation(
            operation.id,
            outcome=None,
            evidence=None,
            now=self.now + 2,
        )
        self.assertEqual(status, OperationStatus.MANUAL_REVIEW_REQUIRED)
        with self.assertRaises(LifecycleError):
            await self.store.reconcile_operation(
                operation.id,
                outcome="succeeded",
                evidence={
                    "source": "verifier",
                    "authoritative": True,
                    "observation": "verifier_check",
                    "observed_outcome": "succeeded",
                    "hash": "abc",
                },
                now=self.now + 3,
            )
        await self.store.complete_run(
            run.id,
            status=RunStatus.MANUAL_REVIEW_REQUIRED,
            now=self.now + 4,
        )

    async def test_authoritative_runtime_reconciliation_is_safe(self):
        run, operation = await self._run_and_intent()
        lease = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=100,
            now=self.now,
        )
        attempt = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now,
        )
        await self.store.mark_attempt_unknown(attempt.id, now=self.now + 1)
        status = await self.store.reconcile_operation(
            operation.id,
            outcome="succeeded",
            evidence={
                "source": "verifier",
                "authoritative": True,
                "observation": "verifier_check",
                "observed_outcome": "succeeded",
                "hash": "abc",
            },
            now=self.now + 2,
        )
        self.assertEqual(status, OperationStatus.SUCCEEDED)

    async def test_failed_attempt_can_retry_with_new_physical_identity(self):
        run, operation = await self._run_and_intent()
        lease = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=100,
            now=self.now,
        )
        first = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now,
        )
        await self.store.finish_attempt(
            first.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            outcome="failed",
            evidence={
                "source": "runtime",
                "authoritative": True,
                "observation": "native_loop_return",
                "observed_outcome": "failed",
                "attempt_id": first.id,
            },
            now=self.now + 1,
        )
        second = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now + 2,
        )
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.attempt_no, 2)

    async def test_specialist_claim_cannot_close_attempt(self):
        run, operation = await self._run_and_intent()
        lease = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=100,
            now=self.now,
        )
        attempt = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now,
        )
        with self.assertRaises(LifecycleError):
            await self.store.finish_attempt(
                attempt.id,
                owner="worker-a",
                fencing_epoch=lease.epoch,
                outcome="succeeded",
                evidence={
                    "source": "specialist",
                    "authoritative": True,
                    "observed_outcome": "succeeded",
                    "observation": "native_loop_return",
                    "attempt_id": attempt.id,
                    "specialist_claim": "I verified the mutation",
                },
                now=self.now + 1,
            )

    async def test_terminal_evidence_cannot_cross_durable_identity(self):
        run, operation = await self._run_and_intent()
        lease = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=100,
            now=self.now,
        )
        attempt = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now,
        )
        for field, value in (
            ("run_id", "other-run"),
            ("operation_id", "other-operation"),
            ("workspace", "/other-workspace"),
            ("owner", "other-user"),
            ("operation_fingerprint", "other-operation-fingerprint"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(LifecycleError):
                    await self.store.finish_attempt(
                        attempt.id,
                        owner="worker-a",
                        fencing_epoch=lease.epoch,
                        outcome="succeeded",
                        evidence={
                            "source": "verifier",
                            "authoritative": True,
                            "observation": "verifier_check",
                            "observed_outcome": "succeeded",
                            "attempt_id": attempt.id,
                            field: value,
                        },
                        now=self.now + 1,
                    )

    async def test_run_cannot_complete_while_operation_is_ambiguous(self):
        run, operation = await self._run_and_intent()
        lease = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=100,
            now=self.now,
        )
        attempt = await self.store.prepare_attempt(
            operation_id=operation.id,
            owner="worker-a",
            fencing_epoch=lease.epoch,
            now=self.now,
        )
        await self.store.mark_attempt_unknown(attempt.id, now=self.now + 1)
        with self.assertRaises(LifecycleError):
            await self.store.complete_run(run.id, status=RunStatus.SUCCEEDED, now=self.now + 2)

    async def test_workspace_lease_epoch_rejects_stale_writer(self):
        run, _ = await self._run_and_intent()
        first = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-a",
            ttl_ms=10,
            now=100,
        )
        self.assertEqual(first.epoch, 1)
        self.assertFalse(
            await self.store.heartbeat_workspace_lease(
                workspace="/workspace",
                owner="worker-a",
                epoch=1,
                ttl_ms=10,
                now=111,
            )
        )
        second = await self.store.acquire_workspace_lease(
            workspace="/workspace",
            run_id=run.id,
            owner="worker-b",
            ttl_ms=10,
            now=111,
        )
        self.assertEqual(second.epoch, 2)
        with self.assertRaises(StaleWriterError):
            await self.store.assert_workspace_fence(
                workspace="/workspace",
                run_id=run.id,
                owner="worker-a",
                epoch=1,
                now=112,
            )

    async def test_orphan_recovery_is_exclusive_and_fenced(self):
        run, _ = await self._run_and_intent()
        await self.store.heartbeat_run(run.id, now=100)
        self.assertEqual(await self.store.mark_orphaned(stale_before=101, now=102), [run.id])
        first = await self.store.acquire_recovery_lease(
            run_id=run.id,
            owner="recovery-a",
            ttl_ms=10,
            now=102,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(
            await self.store.acquire_recovery_lease(
                run_id=run.id,
                owner="recovery-b",
                ttl_ms=10,
                now=103,
            )
        )
        with self.assertRaises(StaleWriterError):
            await self.store.complete_recovery(
                run_id=run.id,
                owner="recovery-a",
                epoch=first.epoch,
                status=RunStatus.SUCCEEDED,
                now=113,
            )

    async def test_recovery_lease_can_be_reclaimed_after_expiry(self):
        run, _ = await self._run_and_intent()
        await self.store.heartbeat_run(run.id, now=100)
        await self.store.mark_orphaned(stale_before=101, now=102)
        first = await self.store.acquire_recovery_lease(
            run_id=run.id, owner="recovery-a", ttl_ms=10, now=102
        )
        second = await self.store.acquire_recovery_lease(
            run_id=run.id, owner="recovery-b", ttl_ms=10, now=113
        )
        self.assertEqual(second.epoch, first.epoch + 1)
        await self.store.complete_recovery(
            run_id=run.id,
            owner="recovery-b",
            epoch=second.epoch,
            status=RunStatus.MANUAL_REVIEW_REQUIRED,
            now=114,
        )

    async def test_sqlite_contention_deduplicates_same_request(self):
        async def create():
            return await self.store.create_run(
                request_key="contended",
                owner="worker-a",
                workspace="/workspace",
            )

        results = await asyncio.gather(*(create() for _ in range(8)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({run.id for run, _ in results}), 1)


if __name__ == "__main__":
    unittest.main()