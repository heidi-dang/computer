import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, FactoryRun, WorkbenchSession
from cptr.services.capability_os.tasks import (
    CapabilityTaskCoordinator,
    CapabilityTaskNotExecutable,
    CapabilityTaskNotFound,
)


class CapabilityOsTaskCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(
                WorkbenchSession(
                    id="wbs_active",
                    user_id="user-1",
                    name="Capability OS",
                    workspace_id="workspace-1",
                    status="RUNNING",
                    event_count=0,
                    created_at=1,
                    updated_at=1,
                )
            )
            db.add(
                WorkbenchSession(
                    id="wbs_done",
                    user_id="user-1",
                    name="Done",
                    workspace_id="workspace-1",
                    status="COMPLETE",
                    event_count=0,
                    created_at=1,
                    updated_at=2,
                )
            )
            db.add(
                FactoryRun(
                    id="factory_active",
                    user_id="user-1",
                    workspace_id="workspace-2",
                    mission="test",
                    acceptance_criteria=["pass"],
                    state="IMPLEMENTING",
                    policy={},
                    budget={},
                    config_fingerprint="cfg",
                    created_at=1,
                    updated_at=1,
                )
            )
            await db.commit()
        self.coordinator = CapabilityTaskCoordinator(session_factory=self.sessions)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_active_workbench_is_owner_bound_task_context(self):
        task = await self.coordinator.require_active(user_id="user-1", task_id="wbs_active")
        self.assertEqual(task.source, "workbench")
        self.assertEqual(task.workspace_id, "workspace-1")
        self.assertTrue(task.active)

    async def test_factory_run_is_supported_without_creating_second_task_universe(self):
        task = await self.coordinator.require_active(user_id="user-1", task_id="factory_active")
        self.assertEqual(task.source, "factory")
        self.assertEqual(task.workspace_id, "workspace-2")
        self.assertTrue(task.active)

    async def test_bootstrap_creates_owner_bound_active_workbench_context(self):
        task = await self.coordinator.bootstrap(user_id="user-1")

        self.assertEqual(task.source, "workbench")
        self.assertEqual(task.user_id, "user-1")
        self.assertIsNone(task.workspace_id)
        self.assertEqual(task.status, "OPEN")
        self.assertTrue(task.active)
        self.assertTrue(task.execution_allowed)

        resolved = await self.coordinator.require_active(
            user_id="user-1", task_id=task.task_id
        )
        self.assertEqual(resolved, task)
        with self.assertRaises(CapabilityTaskNotFound):
            await self.coordinator.require_active(
                user_id="user-2", task_id=task.task_id
            )

    async def test_fork_many_creates_owner_bound_isolated_children_in_one_cohort(self):
        children = await self.coordinator.fork_many(
            user_id="user-1", parent_task_id="wbs_active", count=3
        )

        self.assertEqual(len(children), 3)
        self.assertEqual(len({child.task_id for child in children}), 3)
        for index, child in enumerate(children, start=1):
            self.assertEqual(child.user_id, "user-1")
            self.assertEqual(child.workspace_id, "workspace-1")
            self.assertEqual(child.source, "workbench")
            self.assertEqual(child.status, "OPEN")
            self.assertTrue(child.active)
            self.assertTrue(child.execution_allowed)
            self.assertEqual(child.label, f"Capability OS Subagent {index:02d}")
            resolved = await self.coordinator.require_executable(
                user_id="user-1", task_id=child.task_id
            )
            self.assertEqual(resolved.task_id, child.task_id)
            with self.assertRaises(CapabilityTaskNotFound):
                await self.coordinator.require_active(
                    user_id="user-2", task_id=child.task_id
                )

    async def test_fork_many_rejects_invalid_cohort_sizes(self):
        for count in (1, 11):
            with self.assertRaisesRegex(ValueError, "between 2 and 10"):
                await self.coordinator.fork_many(
                    user_id="user-1", parent_task_id="wbs_active", count=count
                )

    async def test_fork_many_rejects_non_workbench_parent_to_preserve_authority_boundary(self):
        with self.assertRaisesRegex(CapabilityTaskNotExecutable, "requires a workbench parent"):
            await self.coordinator.fork_many(
                user_id="user-1", parent_task_id="factory_active", count=2
            )

    async def test_wrong_owner_and_terminal_task_fail_closed(self):
        with self.assertRaises(CapabilityTaskNotFound):
            await self.coordinator.require_active(user_id="user-2", task_id="wbs_active")
        with self.assertRaises(CapabilityTaskNotFound):
            await self.coordinator.require_active(user_id="user-1", task_id="wbs_done")


if __name__ == "__main__":
    unittest.main()
