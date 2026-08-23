import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers.control import (
    AutonomousCreateRequest,
    TaskCreateRequest,
    _monitor_summary,
    create_autonomous,
    create_task,
)
from cptr.services.supervisor import MonitorState, ScopeRecord, ScopeStatus


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_creation_delegates_to_agent_service_with_stable_contract(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        agent = SimpleNamespace(
            start_task=AsyncMock(
                return_value={
                    "id": "task_1",
                    "workspace_id": "ws_1",
                    "status": "RUNNING",
                }
            )
        )
        services = (agent, SimpleNamespace())
        body = TaskCreateRequest(
            workspace_id="ws_1",
            prompt="Run the tests",
            model_id="model_1",
            idempotency_key="request_1",
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=object())),
            patch("cptr.routers.control._services", return_value=services),
        ):
            result = await create_task(request, body)

        self.assertEqual(result["id"], "task_1")
        agent.start_task.assert_awaited_once_with(
            user_id="user_1",
            workspace_id="ws_1",
            prompt="Run the tests",
            model_id="model_1",
            idempotency_key="request_1",
            request=request,
        )

    async def test_monitor_summary_keeps_original_goal_and_counts_verified_scopes(self):
        monitor = MonitorState(
            monitor_id="mon_1",
            goal_id="goal_1",
            user_id="user_1",
            workspace_id="ws_1",
            original_goal="Ship feature",
            original_acceptance_criteria=["Tests pass", "Diff is reviewed"],
            model_id="model_1",
            scopes=[
                ScopeRecord(
                    "scope_1", "Tests pass", "Tests pass", ["Tests pass"], ScopeStatus.VERIFIED
                ),
                ScopeRecord(
                    "scope_2", "Diff is reviewed", "Diff is reviewed", ["Diff is reviewed"]
                ),
            ],
        )
        result = _monitor_summary(monitor)
        self.assertEqual(result["scope_count"], 2)
        self.assertEqual(result["verified_count"], 1)
        self.assertEqual(result["original_goal"], "Ship feature")
        self.assertEqual(result["acceptance_criteria"], ["Tests pass", "Diff is reviewed"])

    async def test_monitor_creation_schedules_server_side_loop(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        monitor = MonitorState(
            monitor_id="mon_1",
            goal_id="goal_1",
            user_id="user_1",
            workspace_id="ws_1",
            original_goal="Ship feature",
            original_acceptance_criteria=["Tests pass"],
            model_id="model_1",
            scopes=[ScopeRecord("scope_1", "Tests pass", "Tests pass", ["Tests pass"])],
        )
        supervisor = SimpleNamespace(create_goal=AsyncMock(return_value=monitor))
        body = AutonomousCreateRequest(
            workspace_id="ws_1",
            goal="Ship feature",
            acceptance_criteria=["Tests pass"],
            model_id="model_1",
            idempotency_key="goal_1",
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=object())),
            patch("cptr.routers.control._services", return_value=(SimpleNamespace(), supervisor)),
            patch("cptr.routers.control._schedule_monitor") as schedule,
        ):
            result = await create_autonomous(request, body)

        self.assertEqual(result["monitor_id"], "mon_1")
        self.assertEqual(result["status"], "RUNNING")
        schedule.assert_called_once_with(request.app, "mon_1")


if __name__ == "__main__":
    unittest.main()
