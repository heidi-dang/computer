import unittest

from cptr.services.supervisor import (
    AutonomousSupervisor,
    Decision,
    InMemorySupervisorStore,
    MonitorStatus,
    ScopeStatus,
    normalize_failure_signature,
)


class FakeAgentService:
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
        return self.tasks[task_id]


class FakeDirector:
    def __init__(self):
        self.evaluations = 0
        self.final_gates = 0

    async def evaluate(self, **kwargs):
        self.evaluations += 1
        if self.evaluations == 1:
            return Decision(
                scope_satisfied=False,
                goal_satisfied=False,
                defects=["verification failed"],
                next_action_required=True,
                next_assignment="Repair the failing verification result.",
            )
        return Decision(scope_satisfied=True, goal_satisfied=True, next_action_required=False)

    async def diagnose(self, **kwargs):
        return Decision(
            scope_satisfied=False,
            goal_satisfied=False,
            defects=["root cause identified"],
            next_action_required=True,
            next_assignment="Apply the root-cause repair.",
        )

    async def plan_next_action(self, **kwargs):
        return Decision(
            scope_satisfied=False,
            goal_satisfied=False,
            next_action_required=True,
            next_assignment="Apply the planned repair.",
        )

    async def final_gate(self, **kwargs):
        self.final_gates += 1
        return Decision(scope_satisfied=True, goal_satisfied=True, next_action_required=False)


class FailingAgentService(FakeAgentService):
    async def start_task(self, **kwargs):
        raise RuntimeError("model unavailable")


class SupervisorCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_completion_enters_verifying_then_repairs_and_completes(self):
        store = InMemorySupervisorStore()
        agent = FakeAgentService()
        director = FakeDirector()
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=director)

        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Add the feature",
            acceptance_criteria=["The feature works"],
            model_id="model-1",
            idempotency_key="goal-1",
        )

        await supervisor.run_once(monitor.monitor_id)
        state = await store.get_monitor(monitor.monitor_id)
        self.assertEqual(state.scopes[0].status, ScopeStatus.WORKING)

        await supervisor.run_once(monitor.monitor_id)
        state = await store.get_monitor(monitor.monitor_id)
        self.assertIn(ScopeStatus.VERIFYING, state.scopes[0].history)
        self.assertEqual(state.scopes[0].status, ScopeStatus.WORKING)
        self.assertEqual(len(agent.started), 2)

        await supervisor.run_once(monitor.monitor_id)
        state = await store.get_monitor(monitor.monitor_id)
        self.assertEqual(state.scopes[0].status, ScopeStatus.VERIFIED)
        self.assertEqual(state.status, MonitorStatus.COMPLETE)
        self.assertEqual(director.final_gates, 1)

    async def test_goal_input_is_immutable_and_creation_is_idempotent(self):
        store = InMemorySupervisorStore()
        supervisor = AutonomousSupervisor(
            store=store, agent=FakeAgentService(), director=FakeDirector()
        )

        first = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Original goal",
            acceptance_criteria=["Original criterion"],
            model_id="model-1",
            idempotency_key="same-goal",
        )
        second = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Changed goal",
            acceptance_criteria=["Changed criterion"],
            model_id="model-1",
            idempotency_key="same-goal",
        )

        self.assertEqual(first.monitor_id, second.monitor_id)
        state = await store.get_monitor(first.monitor_id)
        self.assertEqual(state.original_goal, "Original goal")
        self.assertEqual(state.original_acceptance_criteria, ["Original criterion"])

    def test_failure_signature_ignores_cosmetic_log_details(self):
        first = normalize_failure_signature(
            {"scope_id": "scope-1", "category": "test_failure", "message": "line 12 failed"}
        )
        second = normalize_failure_signature(
            {"scope_id": "scope-1", "category": "test_failure", "message": "line 47 failed"}
        )
        self.assertEqual(first, second)

    async def test_worker_start_failure_is_blocked_instead_of_left_running(self):
        store = InMemorySupervisorStore()
        supervisor = AutonomousSupervisor(
            store=store,
            agent=FailingAgentService(),
            director=FakeDirector(),
            max_attempts=1,
        )
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Add the feature",
            acceptance_criteria=["The feature works"],
            model_id="model-1",
        )

        state = await supervisor.run_once(monitor.monitor_id)

        self.assertEqual(state.status, MonitorStatus.BLOCKED)
        self.assertEqual(state.scopes[0].status, ScopeStatus.BLOCKED)

    async def test_cancel_propagates_to_the_active_worker(self):
        store = InMemorySupervisorStore()
        agent = FakeAgentService()
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=FakeDirector())
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Add the feature",
            acceptance_criteria=["The feature works"],
            model_id="model-1",
        )

        await supervisor.run_once(monitor.monitor_id)
        state = await supervisor.cancel(monitor.monitor_id)

        self.assertEqual(state.status, MonitorStatus.CANCELLED)
        self.assertEqual(agent.cancelled, ["task_1"])
        self.assertEqual(state.scopes[0].status, ScopeStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
