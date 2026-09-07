import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, FactoryRun, WorkbenchSession
from cptr.services.capability_os.tasks import CapabilityTaskCoordinator, CapabilityTaskNotFound


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

    async def test_wrong_owner_and_terminal_task_fail_closed(self):
        with self.assertRaises(CapabilityTaskNotFound):
            await self.coordinator.require_active(user_id="user-2", task_id="wbs_active")
        with self.assertRaises(CapabilityTaskNotFound):
            await self.coordinator.require_active(user_id="user-1", task_id="wbs_done")


if __name__ == "__main__":
    unittest.main()
