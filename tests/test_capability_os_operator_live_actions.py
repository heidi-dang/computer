from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cptr.routers import capability_os as capability_os_router
from cptr.services.action_traces import ActionTraceStore
from cptr.services.capability_os.operator import CapabilityOsOperatorService


class CapabilityOsOperatorLiveActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_correlated_chatgpt_request_flows_from_router_into_operator_projection(self):
        traces = ActionTraceStore(max_traces=8, max_stages_per_trace=12)
        await traces.append(
            owner_id="user-1",
            trace_id="trace-live-1",
            layer="chatgpt",
            name="chatgpt.request.dispatched",
            status="started",
            timestamp_ms=100,
            request_id="request-1",
            mcp_session_id="session-1",
            tool_name="cptr_factory",
        )
        request = SimpleNamespace(
            headers={
                "x-cptr-trace-id": "trace-live-1",
                "x-cptr-request-id": "request-1",
                "x-cptr-mcp-session-id": "session-1",
                "x-cptr-tool-name": "cptr_factory",
            },
            app=SimpleNamespace(state=SimpleNamespace()),
        )
        control = SimpleNamespace(resolve=AsyncMock(return_value={"task": {"taskId": "task-1"}}))
        body = capability_os_router.ResolveRequest(
            task_id="task-1",
            required=[{"action": "fs.read", "resource": "workspace"}],
        )

        with (
            patch.object(capability_os_router, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(capability_os_router, "_service", return_value=control),
            patch.object(capability_os_router, "action_trace_store", traces),
        ):
            await capability_os_router.resolve_capability_os(request, body)

        operator = CapabilityOsOperatorService(
            store=SimpleNamespace(),
            tasks=SimpleNamespace(),
            runtime=SimpleNamespace(),
            evidence=SimpleNamespace(),
            traces=traces,
        )
        sequence, actions = await operator.recent_actions(
            user_id="user-1", task_id="task-1", limit=10
        )
        self.assertGreater(sequence, 0)
        self.assertEqual(actions[0]["operation"], "resolve")
        self.assertEqual(actions[0]["status"], "ok")
        self.assertEqual(actions[0]["source"], "chatgpt")
        self.assertIn("chatgpt", actions[0]["layers"])
        self.assertIn("backend", actions[0]["layers"])

    async def test_snapshot_projects_recent_task_scoped_chatgpt_actions(self):
        store = SimpleNamespace(
            list_artifacts=AsyncMock(return_value=[]),
            list_active_leases=AsyncMock(return_value=[]),
            list_active_mounts=AsyncMock(return_value=[]),
            list_evidence=AsyncMock(return_value=[]),
            operator_snapshot=AsyncMock(
                return_value={
                    "runs": 0,
                    "activeRuns": 0,
                    "observations": 0,
                    "policyDecisions": 0,
                    "experiments": 0,
                    "activeExperiments": 0,
                }
            ),
        )
        tasks = SimpleNamespace(
            require_active=AsyncMock(
                return_value=SimpleNamespace(
                    task_id="task-1",
                    workspace_id="workspace-1",
                    source="workbench",
                    status="OPEN",
                    active=True,
                    execution_allowed=True,
                )
            )
        )
        runtime = SimpleNamespace(production_snapshot=Mock(return_value={"production": True}))
        evidence = SimpleNamespace(verify_chain=AsyncMock(return_value={"valid": True}))
        traces = SimpleNamespace(
            summaries=AsyncMock(
                return_value={
                    "version": 1,
                    "sequence": 5,
                    "traces": [
                        {
                            "trace_id": "trace-1",
                            "tool_name": "cptr_factory",
                            "status": "ok",
                            "started_at_ms": 100,
                            "updated_at_ms": 140,
                            "duration_ms": 40,
                            "layers": ["chatgpt", "mcp", "backend"],
                            "error_code": None,
                            "task_ids": ["task-1"],
                            "capability_action": "resolve",
                            "capability_status": "ok",
                            "capability_updated_at_ms": 140,
                        }
                    ],
                }
            )
        )
        service = CapabilityOsOperatorService(
            store=store,
            tasks=tasks,
            runtime=runtime,
            evidence=evidence,
            traces=traces,
        )

        snapshot = await service.snapshot(user_id="user-1", task_id="task-1", limit=100)

        traces.summaries.assert_awaited_once_with(owner_id="user-1", task_id="task-1", limit=10)
        self.assertEqual(snapshot["actionSequence"], 5)
        self.assertEqual(snapshot["recentActions"][0]["traceId"], "trace-1")
        self.assertEqual(snapshot["recentActions"][0]["operation"], "resolve")
        self.assertEqual(snapshot["recentActions"][0]["status"], "ok")
        self.assertEqual(snapshot["recentActions"][0]["source"], "chatgpt")
        self.assertEqual(snapshot["recentActions"][0]["toolName"], "cptr_factory")
        self.assertNotIn("payload", snapshot["recentActions"][0])
        self.assertNotIn("inputs", snapshot["recentActions"][0])


if __name__ == "__main__":
    unittest.main()
