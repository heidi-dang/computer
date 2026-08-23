import unittest

from cptr.services.supervisor import (
    AutonomousSupervisor,
    Decision,
    InMemorySupervisorStore,
    MonitorStatus,
)
from cptr.services.verification import VerificationResult


class DeterministicWorker:
    def __init__(self):
        self.started = []
        self.tasks = {}

    async def start_task(self, **kwargs):
        task_id = f"task_{len(self.started) + 1}"
        self.started.append(kwargs)
        self.tasks[task_id] = {"id": task_id, "status": "COMPLETE"}
        return self.tasks[task_id]

    async def get_task(self, task_id, **kwargs):
        return self.tasks[task_id]

    async def get_output(self, task_id, **kwargs):
        return {"task_id": task_id, "content": "durable worker output"}

    async def get_diff(self, workspace_id, **kwargs):
        return {"files": ["src/feature.py"], "patch": "diff"}

    async def get_verification_evidence(self, workspace_id, **kwargs):
        return {"git_diff_check": {"passed": True}}


class RejectThenPassVerifier:
    def __init__(self):
        self.calls = 0

    async def verify(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return VerificationResult(
                passed=False,
                checks=[{"name": "tests", "passed": False}],
                failures=["intentional first verification rejection"],
            )
        return VerificationResult(
            passed=True,
            checks=[{"name": "tests", "passed": True}],
        )


class DeterministicDirector:
    async def evaluate(self, **kwargs):
        return Decision(scope_satisfied=True)

    async def diagnose(self, **kwargs):
        return Decision(next_assignment="Repair the failed verification.")

    async def plan_next_action(self, **kwargs):
        return Decision(next_assignment="Repair the failed verification.")

    async def final_gate(self, **kwargs):
        return Decision(goal_satisfied=True)


class AutonomousE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_repair_reverify_final_gate_complete(self):
        store = InMemorySupervisorStore()
        worker = DeterministicWorker()
        supervisor = AutonomousSupervisor(
            store=store,
            agent=worker,
            director=DeterministicDirector(),
            verifier=RejectThenPassVerifier(),
        )
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Implement the feature",
            acceptance_criteria=["The feature is verified"],
            model_id="model-1",
        )

        await supervisor.run_once(monitor.monitor_id)
        await supervisor.run_once(monitor.monitor_id)
        await supervisor.run_once(monitor.monitor_id)

        state = await store.get_monitor(monitor.monitor_id)
        evidence = await store.list_evidence(monitor.monitor_id)
        self.assertEqual(state.status, MonitorStatus.COMPLETE)
        self.assertEqual(len(worker.started), 2)
        self.assertTrue(any(item.kind == "verification_result" for item in evidence))
        self.assertTrue(any(item.kind == "failure" for item in evidence))
        self.assertTrue(any(item.kind == "final_gate" for item in evidence))


if __name__ == "__main__":
    unittest.main()
