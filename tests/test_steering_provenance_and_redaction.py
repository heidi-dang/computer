import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.supervisor import (
    AutonomousSupervisor,
    Decision,
    InMemorySupervisorStore,
    ScopeStatus,
)
from cptr.services.agent_service import AgentService


class ProvenanceAgent:
    def __init__(self, control_message):
        self.control_message = control_message
        self.started = []

    async def start_task(self, **kwargs):
        task_id = f"task_{len(self.started) + 1}"
        self.started.append(task_id)
        return {"id": task_id, "status": "COMPLETE", "output": "worker output"}

    async def get_task(self, task_id, **kwargs):
        return {"id": task_id, "status": "COMPLETE", "output": "worker output"}

    async def get_output(self, task_id, **kwargs):
        return {"task_id": task_id, "content": "worker output"}

    async def get_diff(self, workspace_id, **kwargs):
        return {"is_repo": True, "files": ["fixture.txt"], "patch": "+expected"}

    async def get_verification_evidence(self, workspace_id, **kwargs):
        return {"git_diff_check": {"passed": True}}

    async def cancel_task(self, task_id, **kwargs):
        return {"id": task_id, "status": "CANCELLED", "cancelled": True}


class SnapshotProvenanceAgent(ProvenanceAgent):
    def __init__(self, control_message, snapshots):
        super().__init__(control_message)
        self.snapshots = list(snapshots)

    async def get_workspace_fingerprint(self, workspace_id, **kwargs):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


class ProvenanceStore(InMemorySupervisorStore):
    async def get_message(self, message_id):
        return self.control_message


class AcceptingDirector:
    def __init__(self):
        self.evaluations = 0

    async def evaluate(self, **kwargs):
        self.evaluations += 1
        return Decision(scope_satisfied=True)

    async def diagnose(self, **kwargs):
        return Decision(next_assignment="repair")

    async def plan_next_action(self, **kwargs):
        return Decision(next_assignment="repair")

    async def final_gate(self, **kwargs):
        return Decision(goal_satisfied=True)


class SteeringProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def _supervisor(self, message):
        store = ProvenanceStore()
        store.control_message = message
        agent = ProvenanceAgent(message)
        director = AcceptingDirector()
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=director)
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Make the fixture change",
            acceptance_criteria=["fixture.txt contains expected"],
            model_id="model-1",
        )
        await supervisor.run_once(monitor.monitor_id)
        scope = (await store.get_monitor(monitor.monitor_id)).scopes[0]
        await supervisor.record_steering(
            monitor.monitor_id,
            scope_id=scope.scope_id,
            control_message_id="control-1",
            intended_task_id="task_1",
            intended_generation_id="message-1",
        )
        return store, director, supervisor, monitor

    async def test_queued_control_cannot_make_scope_verified(self):
        store, director, supervisor, monitor = await self._supervisor(
            SimpleNamespace(
                id="control-1",
                status="QUEUED",
                task_id="task_1",
                consumed_task_id=None,
                consumed_message_id=None,
            )
        )

        state = await supervisor.run_once(monitor.monitor_id)

        self.assertNotEqual(state.scopes[0].status, ScopeStatus.VERIFIED)
        self.assertEqual(director.evaluations, 0)
        self.assertEqual((await store.get_monitor(monitor.monitor_id)).status.value, "RUNNING")

    async def test_consumption_by_replacement_task_cannot_verify_original_steering(self):
        store, director, supervisor, monitor = await self._supervisor(
            SimpleNamespace(
                id="control-1",
                status="CONSUMED",
                task_id="task_1",
                consumed_task_id="task_replacement",
                consumed_message_id="message-replacement",
            )
        )

        state = await supervisor.run_once(monitor.monitor_id)

        self.assertNotEqual(state.scopes[0].status, ScopeStatus.VERIFIED)
        self.assertEqual(director.evaluations, 0)

    async def test_same_intended_task_consumption_allows_verification(self):
        store, director, supervisor, monitor = await self._supervisor(
            SimpleNamespace(
                id="control-1",
                status="CONSUMED",
                task_id="task_1",
                consumed_task_id="task_1",
                consumed_message_id="message-2",
            )
        )

        state = await supervisor.run_once(monitor.monitor_id)

        self.assertEqual(state.scopes[0].status, ScopeStatus.VERIFIED)
        self.assertEqual(director.evaluations, 1)

    async def test_preexisting_diff_without_new_effect_cannot_verify_steering(self):
        store, director, supervisor, monitor = await self._supervisor(
            SimpleNamespace(
                id="control-1",
                status="CONSUMED",
                task_id="task_1",
                consumed_task_id="task_1",
                consumed_message_id="message-2",
            )
        )
        scope = (await store.get_monitor(monitor.monitor_id)).scopes[0]
        scope.steering_requests[0]["baseline_diff_fingerprint"] = hashlib.sha256(
            json.dumps(
                {"is_repo": True, "files": ["fixture.txt"], "patch": "+expected"},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        state = await supervisor.run_once(monitor.monitor_id)

        self.assertNotEqual(state.scopes[0].status, ScopeStatus.VERIFIED)
        self.assertEqual(director.evaluations, 0)

    async def test_same_worker_tracked_file_effect_is_observed(self):
        message = SimpleNamespace(
            id="control-1",
            status="CONSUMED",
            task_id="task_1",
            consumed_task_id="task_1",
            consumed_message_id="message-2",
            consumed_at=20,
        )
        store = ProvenanceStore()
        store.control_message = message
        agent = SnapshotProvenanceAgent(
            message,
            [
                {"fingerprint": "tracked-after", "files": [{"path": "fixture.txt"}]},
            ],
        )
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=AcceptingDirector())
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Apply the steering change",
            acceptance_criteria=["fixture.txt contains the steering marker"],
            model_id="model-1",
        )
        await supervisor.run_once(monitor.monitor_id)
        scope = (await store.get_monitor(monitor.monitor_id)).scopes[0]
        await supervisor.record_steering(
            monitor.monitor_id,
            scope_id=scope.scope_id,
            control_message_id="control-1",
            intended_task_id="task_1",
            intended_generation_id="message-1",
            baseline_workspace_snapshot={
                "fingerprint": "tracked-before",
                "files": [{"path": "fixture.txt"}],
            },
        )

        state = await supervisor.run_once(monitor.monitor_id)

        self.assertEqual(state.scopes[0].status, ScopeStatus.VERIFIED)
        record = state.scopes[0].steering_requests[0]
        self.assertEqual(record["intended_task_id"], "task_1")
        self.assertEqual(record["consumed_task_id"], "task_1")
        self.assertEqual(record["consumed_message_id"], "message-2")
        self.assertEqual(record["effect_status"], "EFFECT_OBSERVED")
        self.assertEqual(record["baseline_workspace_fingerprint"], "tracked-before")
        self.assertEqual(record["post_consumption_workspace_fingerprint"], "tracked-after")

    async def test_same_worker_existing_untracked_file_content_effect_is_observed(self):
        message = SimpleNamespace(
            id="control-1",
            status="CONSUMED",
            task_id="task_1",
            consumed_task_id="task_1",
            consumed_message_id="message-2",
            consumed_at=20,
        )
        store = ProvenanceStore()
        store.control_message = message
        agent = SnapshotProvenanceAgent(
            message,
            [
                {
                    "fingerprint": "untracked-after",
                    "files": [{"path": "fixture.txt", "sha256": "base-plus-steering"}],
                },
            ],
        )
        supervisor = AutonomousSupervisor(store=store, agent=agent, director=AcceptingDirector())
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Apply the steering change",
            acceptance_criteria=["fixture.txt contains the steering marker"],
            model_id="model-1",
        )
        await supervisor.run_once(monitor.monitor_id)
        scope = (await store.get_monitor(monitor.monitor_id)).scopes[0]
        await supervisor.record_steering(
            monitor.monitor_id,
            scope_id=scope.scope_id,
            control_message_id="control-1",
            intended_task_id="task_1",
            intended_generation_id="message-1",
            baseline_workspace_snapshot={
                "fingerprint": "untracked-before",
                "files": [{"path": "fixture.txt", "sha256": "base"}],
            },
        )

        state = await supervisor.run_once(monitor.monitor_id)

        self.assertEqual(state.scopes[0].status, ScopeStatus.VERIFIED)
        self.assertEqual(state.scopes[0].steering_requests[0]["effect_status"], "EFFECT_OBSERVED")

    async def test_consumed_same_worker_without_effect_converges_without_verification(self):
        message = SimpleNamespace(
            id="control-1",
            status="CONSUMED",
            task_id="task_1",
            consumed_task_id="task_1",
            consumed_message_id="message-2",
            consumed_at=20,
        )
        store = ProvenanceStore()
        store.control_message = message
        agent = SnapshotProvenanceAgent(
            message,
            [{"fingerprint": "unchanged", "files": [{"path": "fixture.txt", "sha256": "base"}]}],
        )
        director = AcceptingDirector()
        supervisor = AutonomousSupervisor(
            store=store,
            agent=agent,
            director=director,
            max_attempts=1,
        )
        monitor = await supervisor.create_goal(
            user_id="user-1",
            workspace_id="workspace-1",
            goal="Apply the steering change",
            acceptance_criteria=["fixture.txt contains the steering marker"],
            model_id="model-1",
        )
        await supervisor.run_once(monitor.monitor_id)
        scope = (await store.get_monitor(monitor.monitor_id)).scopes[0]
        await supervisor.record_steering(
            monitor.monitor_id,
            scope_id=scope.scope_id,
            control_message_id="control-1",
            intended_task_id="task_1",
            intended_generation_id="message-1",
            baseline_workspace_snapshot={"fingerprint": "unchanged", "files": []},
        )

        state = await supervisor.run_once(monitor.monitor_id)

        self.assertEqual(state.status.value, "BLOCKED")
        self.assertNotEqual(state.scopes[0].status, ScopeStatus.VERIFIED)
        self.assertEqual(director.evaluations, 0)
        self.assertEqual(
            state.scopes[0].steering_requests[0]["effect_status"], "EFFECT_NOT_OBSERVED"
        )


class RedactionTests(unittest.TestCase):
    def test_redacts_nested_secrets_and_embedded_credentials(self):
        from cptr.utils.redaction import redact_sensitive

        value = {
            "authorization": "Bearer bearer-secret-123",
            "nested": {"refresh_token": "refresh-secret-456"},
            "url": "https://example.test/callback?code=auth-code-789&state=state-000",
            "text": "Authorization: Bearer header-secret-999",
        }

        redacted = redact_sensitive(value)
        rendered = repr(redacted)

        for secret in (
            "bearer-secret-123",
            "refresh-secret-456",
            "auth-code-789",
            "state-000",
            "header-secret-999",
        ):
            self.assertNotIn(secret, rendered)
        self.assertEqual(redacted["authorization"], "[REDACTED]")
        self.assertIn("[REDACTED]", redacted["url"])

    def test_redacts_json_encoded_output_without_losing_safe_context(self):
        from cptr.utils.redaction import redact_sensitive

        text = (
            '{"event":"browser","access_token":"token-123","url":"https://x.test/?api_key=key-456"}'
        )
        redacted = redact_sensitive(text)

        self.assertNotIn("token-123", redacted)
        self.assertNotIn("key-456", redacted)
        self.assertIn("browser", redacted)

    def test_agent_task_and_evidence_boundaries_redact_output(self):
        service = AgentService()
        task = SimpleNamespace(
            id="task-1",
            user_id="user-1",
            workspace_id="workspace-1",
            chat_id="chat-1",
            message_id="message-1",
            status="COMPLETE",
            prompt="inspect fixture",
            model_id="model-1",
            output=None,
            error=None,
            created_at=1,
            updated_at=1,
        )
        message = SimpleNamespace(
            id="message-1",
            done=True,
            content="opened https://example.test/?access_token=secret-123",
            output=[{"type": "tool", "authorization": "Bearer secret-456"}],
            meta={"error": "cookie: session=secret-789"},
        )
        with (
            patch.object(service.store, "get", new=AsyncMock(return_value=task)),
            patch.object(service.store, "transition_terminal", new=AsyncMock(return_value=True)),
            patch.object(service.store, "update", new=AsyncMock()),
            patch("cptr.models.ChatMessage.get_by_id", new=AsyncMock(return_value=message)),
        ):
            import asyncio

            result = asyncio.run(service.get_task("task-1", user_id="user-1"))

        rendered = repr(result)
        for secret in ("secret-123", "secret-456", "secret-789"):
            self.assertNotIn(secret, rendered)

    def test_in_memory_evidence_boundary_redacts_payload(self):
        import asyncio

        store = InMemorySupervisorStore()
        evidence = asyncio.run(
            store.append_evidence(
                "monitor-1",
                "scope-1",
                "worker_output",
                {"url": "https://example.test/?token=secret-000"},
            )
        )

        self.assertNotIn("secret-000", repr(evidence.payload))


if __name__ == "__main__":
    unittest.main()
