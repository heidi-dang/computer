import asyncio
import unittest

from cptr.services.supervisor import (
    AutonomousSupervisor,
    Decision,
    InMemorySupervisorStore,
    MonitorStatus,
    ScopeStatus,
    normalize_failure_signature,
)
from cptr.services.verification import VerificationResult


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


class NonTerminalFailingAgentService(FakeAgentService):
    async def start_task(self, **kwargs):
        raise RuntimeError("transient model unavailable")


class AlwaysRejectingVerifier:
    async def verify(self, **kwargs):
        return VerificationResult(
            passed=False,
            checks=[{"name": "intentional_rejection", "passed": False}],
            failures=["intentional verification failure"],
        )


class FinalGateRepairDirector(FakeDirector):
    async def evaluate(self, **kwargs):
        return Decision(scope_satisfied=True)

    async def final_gate(self, **kwargs):
        self.final_gates += 1
        if self.final_gates == 1:
            return Decision(
                defects=["final acceptance failed"],
                next_action_required=True,
                next_assignment="Repair the final acceptance failure.",
            )
        return Decision(goal_satisfied=True)


class BlockingObserveAgent(FakeAgentService):
    def __init__(self):
        super().__init__()
        self.observe_started = asyncio.Event()
        self.release_observe = asyncio.Event()

    async def get_task(self, task_id, **kwargs):
        self.observe_started.set()
        await self.release_observe.wait()
        return {"id": task_id, "status": "COMPLETE", "output": "worker finished"}


class CountingDirector(FakeDirector):
    pass


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

    async def test_transient_worker_start_failure_releases_lease_for_next_monitor(self):
        store = InMemorySupervisorStore()
        supervisor = AutonomousSupervisor(
            store=store,
            agent=NonTerminalFailingAgentService(),
            director=FakeDirector(),
            max_attempts=3,
        )
        first = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="First attempt",
            acceptance_criteria=["The work is safe"],
            model_id="model-1",
        )
        await supervisor.run_once(first.monitor_id)
        self.assertEqual((await store.get_monitor(first.monitor_id)).status, MonitorStatus.RUNNING)

        second = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Second attempt",
            acceptance_criteria=["The work is safe"],
            model_id="model-1",
        )
        state = await supervisor.run_once(second.monitor_id)

        self.assertNotEqual(
            state.scopes[0].next_action, "Waiting for the workspace writer lease to be released."
        )

    async def test_terminal_monitor_release_does_not_steal_live_lease(self):
        store = InMemorySupervisorStore()
        owner = await AutonomousSupervisor(
            store=store, agent=FakeAgentService(), director=FakeDirector()
        ).create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Owner",
            acceptance_criteria=["Owner work"],
            model_id="model-1",
        )
        contender = await AutonomousSupervisor(
            store=store, agent=FakeAgentService(), director=FakeDirector()
        ).create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Contender",
            acceptance_criteria=["Contender work"],
            model_id="model-1",
        )
        self.assertTrue(await store.claim_workspace("workspace-1", owner.monitor_id))
        self.assertFalse(await store.claim_workspace("workspace-1", contender.monitor_id))

        owner_state = await store.get_monitor(owner.monitor_id)
        owner_state.status = MonitorStatus.COMPLETE
        await store.save_monitor(owner_state)

        self.assertTrue(await store.claim_workspace("workspace-1", contender.monitor_id))

    async def test_scoped_assignment_explicitly_selects_current_workspace_only(self):
        store = InMemorySupervisorStore()
        agent = FakeAgentService()
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=FakeDirector())
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Inspect the fixture",
            acceptance_criteria=["The fixture is valid"],
            model_id="model-1",
        )

        await supervisor.run_once(monitor.monitor_id)

        prompt = agent.started[0][1]
        self.assertIn("workspace_scope=current", prompt)
        self.assertIn("inspection_scope=workspace", prompt)
        self.assertNotIn("inspection_scope=assignment", prompt)
        self.assertNotIn("workspace_scope=all", prompt)

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
        self.assertEqual(state.scopes[0].history[-1], ScopeStatus.CANCELLED)

    async def test_cancel_does_not_overwrite_completed_monitor(self):
        store = InMemorySupervisorStore()
        agent = FakeAgentService()
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=FakeDirector())
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Already complete",
            acceptance_criteria=["The work is complete"],
            model_id="model-1",
        )
        monitor.status = MonitorStatus.COMPLETE
        monitor.scopes[0].status = ScopeStatus.VERIFIED
        await store.save_monitor(monitor)

        result = await supervisor.cancel(monitor.monitor_id)

        self.assertEqual(result.status, MonitorStatus.COMPLETE)
        self.assertEqual(agent.cancelled, [])

    async def test_cancel_stops_stale_supervision_before_verification_or_repair(self):
        store = InMemorySupervisorStore()
        agent = BlockingObserveAgent()
        director = CountingDirector()
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=director)
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Cancel during observation",
            acceptance_criteria=["The work is cancelled safely"],
            model_id="model-1",
        )

        await supervisor.run_once(monitor.monitor_id)
        stale_run = asyncio.create_task(supervisor.run_once(monitor.monitor_id))
        await asyncio.wait_for(agent.observe_started.wait(), timeout=1)

        cancelled = await supervisor.cancel(monitor.monitor_id)
        self.assertEqual(cancelled.status, MonitorStatus.CANCELLED)
        agent.release_observe.set()
        with self.assertRaises(asyncio.CancelledError):
            await stale_run

        state = await store.get_monitor(monitor.monitor_id)
        self.assertEqual(state.status, MonitorStatus.CANCELLED)
        self.assertEqual(state.scopes[0].status, ScopeStatus.CANCELLED)
        self.assertEqual(director.evaluations, 0)
        self.assertEqual(len(agent.started), 1)

    async def test_approval_is_persisted_enforced_and_resumed(self):
        store = InMemorySupervisorStore()
        agent = FakeAgentService()
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=FakeDirector())
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Publish the change",
            acceptance_criteria=["Run git push origin feature"],
            model_id="model-1",
        )

        paused = await supervisor.run_once(monitor.monitor_id)
        self.assertEqual(paused.status, MonitorStatus.APPROVAL_REQUIRED)
        self.assertIsNotNone(paused.approval_id)
        approval = await store.get_approval(paused.approval_id)
        self.assertEqual(approval.status, "PENDING")
        self.assertEqual(agent.started, [])

        resumed = await supervisor.approve(
            monitor.monitor_id,
            approval_id=paused.approval_id,
            approved=True,
        )
        self.assertEqual(resumed.status, MonitorStatus.RUNNING)
        await supervisor.run_once(monitor.monitor_id)
        self.assertEqual(len(agent.started), 1)
        self.assertEqual((await store.get_approval(approval.approval_id)).status, "APPROVED")

    async def test_evidence_is_persisted_and_repeated_signature_escalates(self):
        store = InMemorySupervisorStore()
        supervisor = AutonomousSupervisor(
            store=store,
            agent=FakeAgentService(),
            director=FakeDirector(),
            verifier=AlwaysRejectingVerifier(),
            max_attempts=3,
        )
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Add the feature",
            acceptance_criteria=["The feature works"],
            model_id="model-1",
        )

        await supervisor.run_once(monitor.monitor_id)
        await supervisor.run_once(monitor.monitor_id)
        state = await store.get_monitor(monitor.monitor_id)
        self.assertEqual(state.scopes[0].failure_signature_counts.__len__(), 1)
        self.assertIn("normal repair", state.scopes[0].next_action)
        await supervisor.run_once(monitor.monitor_id)
        state = await store.get_monitor(monitor.monitor_id)
        self.assertIn("explicit root-cause re-analysis", state.scopes[0].next_action)
        evidence = await store.list_evidence(monitor.monitor_id)
        self.assertTrue(any(item.kind == "verification_result" for item in evidence))

    async def test_final_gate_failure_creates_repair_work(self):
        store = InMemorySupervisorStore()
        agent = FakeAgentService()
        director = FinalGateRepairDirector()
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=director)
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Add the feature",
            acceptance_criteria=["The feature works"],
            model_id="model-1",
        )

        await supervisor.run_once(monitor.monitor_id)
        await supervisor.run_once(monitor.monitor_id)
        await supervisor.run_once(monitor.monitor_id)

        state = await store.get_monitor(monitor.monitor_id)
        self.assertEqual(state.status, MonitorStatus.COMPLETE)
        self.assertEqual(len(agent.started), 2)
        self.assertEqual(director.final_gates, 2)

    def test_approval_classifier_respects_negated_risky_actions(self):
        self.assertTrue(AutonomousSupervisor._requires_approval("deploy production"))
        self.assertTrue(AutonomousSupervisor._requires_approval("push to GitHub"))
        self.assertFalse(AutonomousSupervisor._requires_approval("do not deploy production"))
        self.assertFalse(AutonomousSupervisor._requires_approval("do not push to GitHub"))
        self.assertFalse(
            AutonomousSupervisor._requires_approval(
                "Do not commit, push, deploy, access the network, access credentials, "
                "or perform external actions."
            )
        )
        self.assertTrue(
            AutonomousSupervisor._requires_approval(
                "Push the current commit to an external Git remote."
            )
        )


if __name__ == "__main__":
    unittest.main()
