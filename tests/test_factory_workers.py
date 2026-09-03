import asyncio
import unittest
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.direct_coding_workers import DirectCodingWorkerError
from cptr.services.factory_domain import FactoryActor, FactoryState
from cptr.services.factory_store import SqlFactoryStore
from cptr.services.factory_workers import (
    FactoryWorkerAssignmentMode,
    FactoryWorkerAssignmentStatus,
    FactoryWorkerController,
    FactoryWorkerError,
    SqlFactoryWorkerStore,
)


class _WorkerService:
    def __init__(self):
        self.workers = {}
        self.create_error = None
        self.closed = []

    async def create(self, *, user_id, workspace, name, responsibility, repo_path):
        if self.create_error is not None:
            raise self.create_error
        worker_id = f"dcw_{len(self.workers) + 1}"
        summary = {
            "worker_id": worker_id,
            "workspace_id": workspace.id,
            "status": "READY",
            "branch": f"cptr/direct/{worker_id}",
            "base_revision": "rev-1",
            "active_command_ids": [],
            "changed_file_count": 0,
        }
        self.workers[worker_id] = dict(summary)
        return summary

    async def get(self, *, user_id, workspace_id, worker_id):
        value = self.workers.get(worker_id)
        if value is None:
            raise DirectCodingWorkerError("DIRECT_WORKER_NOT_FOUND", "not found", status_code=404)
        return dict(value)

    async def close(self, *, user_id, workspace, worker_id, discard_changes=False):
        value = self.workers[worker_id]
        if value.get("changed_file_count") and not discard_changes:
            raise DirectCodingWorkerError(
                "DIRECT_WORKER_UNINTEGRATED_CHANGES",
                "worker has unintegrated changes",
            )
        self.closed.append((worker_id, discard_changes))
        value["status"] = "CLOSED"
        value["active_command_ids"] = []
        return {"worker_id": worker_id, "workspace_id": workspace.id, "status": "CLOSED"}


class _CommandController:
    def __init__(self, outcomes=None):
        self.outcomes = dict(outcomes or {})
        self.calls = []

    async def terminate_and_wait(self, *, user_id, command_id, timeout_ms):
        self.calls.append((user_id, command_id, timeout_ms))
        return self.outcomes.get(command_id, True)


class FactoryWorkerControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.factory_store = SqlFactoryStore(session_factory=self.sessions)
        self.assignment_store = SqlFactoryWorkerStore(session_factory=self.sessions)
        self.run = await self.factory_store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="phase six",
            acceptance_criteria=["worker ownership is durable"],
            policy={},
            budget={},
            model_id=None,
            idempotency_key="workers-run",
        )
        self.cycle = await self.factory_store.create_cycle(
            self.run.id,
            base_revision="rev-1",
            base_fingerprint="fp-1",
            idempotency_key="workers-cycle",
        )
        self.workspace = SimpleNamespace(id="workspace-1", path="/tmp/factory-workspace")
        self.worker_service = _WorkerService()
        self.command_controller = _CommandController()
        self.controller = FactoryWorkerController(
            store=self.assignment_store,
            worker_service=self.worker_service,
            workspace_loader=self._workspace_loader,
            command_controller=self.command_controller,
            max_read_only_assignments=2,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _workspace_loader(self, *, user_id, workspace_id):
        if user_id != "user-1" or workspace_id != "workspace-1":
            raise KeyError("workspace not found")
        return self.workspace

    async def test_dirty_base_error_from_direct_worker_is_fail_closed_and_not_persisted(self):
        self.worker_service.create_error = DirectCodingWorkerError(
            "DIRECT_WORKER_DIRTY_BASE",
            "source repository must be clean",
        )

        with self.assertRaises(DirectCodingWorkerError) as caught:
            await self.controller.create_mutation_worker(
                self.run,
                self.cycle,
                repo_path=".",
                scope=("src",),
                name="implementation",
            )

        self.assertEqual(caught.exception.code, "DIRECT_WORKER_DIRTY_BASE")
        self.assertEqual(await self.assignment_store.list_for_run(self.run.id), [])

    async def test_cross_workspace_worker_reference_is_rejected_before_assignment(self):
        self.worker_service.workers["dcw_other"] = {
            "worker_id": "dcw_other",
            "workspace_id": "workspace-2",
            "status": "READY",
            "branch": "cptr/direct/dcw_other",
            "base_revision": "rev-1",
            "active_command_ids": [],
            "changed_file_count": 0,
        }

        with self.assertRaises(FactoryWorkerError) as caught:
            await self.controller.assign_mutation(
                self.run,
                self.cycle,
                worker_id="dcw_other",
                repo_path=".",
                scope=("src",),
            )

        self.assertEqual(caught.exception.code, "FACTORY_WORKER_CROSS_WORKSPACE")
        self.assertEqual(await self.assignment_store.list_for_run(self.run.id), [])

    async def test_overlapping_mutation_scopes_are_serialized_but_read_only_can_overlap(self):
        first = await self.controller.create_mutation_worker(
            self.run,
            self.cycle,
            repo_path=".",
            scope=("src",),
            name="writer-one",
        )
        read_one = await self.controller.assign_read_only(
            self.run,
            self.cycle,
            owner_key="audit-1",
            scope=("src/api",),
        )
        read_two = await self.controller.assign_read_only(
            self.run,
            self.cycle,
            owner_key="audit-2",
            scope=("src",),
        )
        self.worker_service.workers["dcw_2"] = {
            "worker_id": "dcw_2",
            "workspace_id": "workspace-1",
            "status": "READY",
            "branch": "cptr/direct/dcw_2",
            "base_revision": "rev-1",
            "active_command_ids": [],
            "changed_file_count": 0,
        }

        with self.assertRaises(FactoryWorkerError) as caught:
            await self.controller.assign_mutation(
                self.run,
                self.cycle,
                worker_id="dcw_2",
                repo_path=".",
                scope=("src/api",),
            )

        self.assertEqual(caught.exception.code, "FACTORY_WORKER_SCOPE_CONFLICT")
        self.assertEqual(first.mode, FactoryWorkerAssignmentMode.MUTATION.value)
        self.assertEqual(read_one.mode, FactoryWorkerAssignmentMode.READ_ONLY.value)
        self.assertEqual(read_two.mode, FactoryWorkerAssignmentMode.READ_ONLY.value)

        await self.assignment_store.set_status(first.id, FactoryWorkerAssignmentStatus.QUIESCENT)
        released = await self.controller.assign_mutation(
            self.run,
            self.cycle,
            worker_id="dcw_2",
            repo_path=".",
            scope=("src/api",),
        )
        self.assertEqual(released.worker_id, "dcw_2")

        with self.assertRaises(FactoryWorkerError) as read_limit:
            await self.controller.assign_read_only(
                self.run,
                self.cycle,
                owner_key="audit-3",
                scope=("tests",),
            )
        self.assertEqual(read_limit.exception.code, "FACTORY_WORKER_READ_ONLY_LIMIT")

    async def test_terminal_writer_is_discoverable_until_quiescent_then_releases_scope(self):
        assignment = await self.controller.create_mutation_worker(
            self.run,
            self.cycle,
            repo_path=".",
            scope=("src",),
            name="terminal-writer",
        )
        await self.factory_store.transition(
            self.run.id,
            to_state=FactoryState.RECOVERING,
            actor=FactoryActor.SYSTEM,
            reason="recover",
            idempotency_key="terminal-writer-recover",
        )
        await self.factory_store.transition(
            self.run.id,
            to_state=FactoryState.BASELINING,
            actor=FactoryActor.SYSTEM,
            reason="baseline",
            idempotency_key="terminal-writer-baseline",
        )
        await self.factory_store.transition(
            self.run.id,
            to_state=FactoryState.BLOCKED,
            actor=FactoryActor.SYSTEM,
            reason="blocked",
            idempotency_key="terminal-writer-blocked",
        )

        self.assertEqual(
            await self.assignment_store.list_terminal_blocking_run_ids(), [self.run.id]
        )
        await self.assignment_store.set_status(
            assignment.id, FactoryWorkerAssignmentStatus.QUIESCENT
        )
        self.assertEqual(await self.assignment_store.list_terminal_blocking_run_ids(), [])

    async def test_read_only_limit_is_transactionally_enforced_under_concurrency(self):
        results = await asyncio.gather(
            *(
                self.controller.assign_read_only(
                    self.run,
                    self.cycle,
                    owner_key=f"audit-race-{index}",
                    scope=(f"docs/{index}",),
                )
                for index in range(3)
            ),
            return_exceptions=True,
        )

        successes = [item for item in results if not isinstance(item, Exception)]
        failures = [item for item in results if isinstance(item, FactoryWorkerError)]
        self.assertEqual(len(successes), 2)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].code, "FACTORY_WORKER_READ_ONLY_LIMIT")

    async def test_concurrent_overlapping_mutations_allow_only_one_owner(self):
        for worker_id in ("dcw_race_a", "dcw_race_b"):
            self.worker_service.workers[worker_id] = {
                "worker_id": worker_id,
                "workspace_id": "workspace-1",
                "status": "READY",
                "branch": f"cptr/direct/{worker_id}",
                "base_revision": "rev-1",
                "active_command_ids": [],
                "changed_file_count": 0,
            }
        results = await asyncio.gather(
            self.controller.assign_mutation(
                self.run,
                self.cycle,
                worker_id="dcw_race_a",
                repo_path=".",
                scope=("src",),
            ),
            self.controller.assign_mutation(
                self.run,
                self.cycle,
                worker_id="dcw_race_b",
                repo_path=".",
                scope=("src/api",),
            ),
            return_exceptions=True,
        )

        successes = [item for item in results if not isinstance(item, Exception)]
        failures = [item for item in results if isinstance(item, FactoryWorkerError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].code, "FACTORY_WORKER_SCOPE_CONFLICT")

    async def test_restart_reconciliation_uses_durable_assignment_and_worker_state(self):
        assignment = await self.controller.create_mutation_worker(
            self.run,
            self.cycle,
            repo_path=".",
            scope=("src/service.py",),
            name="restartable",
        )
        self.worker_service.workers[assignment.worker_id]["status"] = "RUNNING"
        self.worker_service.workers[assignment.worker_id]["active_command_ids"] = ["cmd-1"]

        restarted = FactoryWorkerController(
            store=SqlFactoryWorkerStore(session_factory=self.sessions),
            worker_service=self.worker_service,
            workspace_loader=self._workspace_loader,
            command_controller=_CommandController(),
        )
        records = await restarted.reconcile(self.run)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].id, assignment.id)
        self.assertEqual(records[0].worker_id, assignment.worker_id)
        self.assertEqual(records[0].status, FactoryWorkerAssignmentStatus.ACTIVE.value)

        del self.worker_service.workers[assignment.worker_id]
        records = await restarted.reconcile(self.run)
        self.assertEqual(records[0].status, FactoryWorkerAssignmentStatus.MISSING.value)

    async def test_cancel_requires_owned_commands_to_be_quiescent_before_marking_assignment(self):
        first = await self.controller.create_mutation_worker(
            self.run,
            self.cycle,
            repo_path=".",
            scope=("src",),
            name="cancel-me",
        )
        self.worker_service.workers[first.worker_id]["status"] = "RUNNING"
        self.worker_service.workers[first.worker_id]["active_command_ids"] = ["cmd-a", "cmd-b"]
        self.command_controller.outcomes = {"cmd-a": True, "cmd-b": False}

        blocked = await self.controller.cancel_run(self.run, timeout_ms=250)
        persisted = await self.assignment_store.get(first.id)

        self.assertFalse(blocked.quiescent)
        self.assertEqual(blocked.failed_command_ids, ("cmd-b",))
        self.assertEqual(persisted.status, FactoryWorkerAssignmentStatus.CANCELLING.value)

        self.worker_service.workers[first.worker_id]["active_command_ids"] = []
        self.command_controller.outcomes["cmd-b"] = True
        quiescent = await self.controller.cancel_run(self.run, timeout_ms=250)
        persisted = await self.assignment_store.get(first.id)

        self.assertTrue(quiescent.quiescent)
        self.assertEqual(persisted.status, FactoryWorkerAssignmentStatus.QUIESCENT.value)

    async def test_cleanup_preserves_unintegrated_changes_unless_discard_is_explicit(self):
        assignment = await self.controller.create_mutation_worker(
            self.run,
            self.cycle,
            repo_path=".",
            scope=("src",),
            name="cleanup",
        )
        await self.assignment_store.set_status(
            assignment.id,
            FactoryWorkerAssignmentStatus.QUIESCENT,
        )
        self.worker_service.workers[assignment.worker_id]["changed_file_count"] = 1

        with self.assertRaises(DirectCodingWorkerError) as caught:
            await self.controller.cleanup(self.run, assignment.id)
        self.assertEqual(caught.exception.code, "DIRECT_WORKER_UNINTEGRATED_CHANGES")
        self.assertEqual(
            (await self.assignment_store.get(assignment.id)).status,
            FactoryWorkerAssignmentStatus.QUIESCENT.value,
        )

        result = await self.controller.cleanup(self.run, assignment.id, discard_changes=True)
        self.assertEqual(result.status, FactoryWorkerAssignmentStatus.CLOSED.value)
        self.assertEqual(self.worker_service.closed, [(assignment.worker_id, True)])


if __name__ == "__main__":
    unittest.main()
