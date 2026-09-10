"""API contract tests for the Capability OS operator projection and live stream."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cptr.routers import mcp as mcp_router


def route_request(*, disconnect_after: int = 20):
    calls = 0

    async def is_disconnected():
        nonlocal calls
        calls += 1
        return calls > disconnect_after

    return SimpleNamespace(
        headers={},
        cookies={},
        client=None,
        state=SimpleNamespace(),
        is_disconnected=is_disconnected,
    )


def operator_snapshot(*, active_runs: int = 0, task_id: str = "task-1") -> dict:
    return {
        "task": {
            "taskId": task_id,
            "workspaceId": "workspace-1",
            "source": "workbench",
            "status": "OPEN",
            "active": True,
            "executionAllowed": True,
        },
        "views": {
            "taskCausality": {
                "runs": active_runs,
                "activeRuns": active_runs,
                "evidenceRecords": 1,
                "observations": 1,
                "evidenceChain": {"valid": True},
            },
            "capabilityHealth": {
                "artifacts": 1,
                "capabilities": 1,
                "byKind": {"Capability": 1},
                "byState": {"QUALIFIED": 1},
            },
            "forge": {"tools": 0, "builds": 0, "runs": 0, "failures": 0},
            "skillEvolution": {"skills": 0, "evaluations": 0, "promotions": 0},
            "mcpFabric": {"adapters": 0, "activeMounts": 0, "events": 0},
            "authority": {"activeLeases": 0, "policyDecisions": 0},
            "sandbox": {"runtime": {"production": True, "gvisor": False}},
            "evolution": {"experiments": 0, "activeExperiments": 0, "events": 0, "promotions": 0},
            "releases": {"learnedOrHigher": 0, "certified": 0, "core": 0, "supplyChainBuilds": 0},
        },
        "activeLeases": [],
        "activeMounts": [],
        "artifactStates": {"QUALIFIED": 1},
        "evidenceKinds": {"verification": 1},
    }


class McpCapabilityOsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_os_stream_is_admin_scoped_and_emits_changed_snapshots(self):
        self.assertTrue(
            hasattr(mcp_router, "stream_capability_os_operator"),
            "Capability OS live stream endpoint must exist",
        )
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        snapshots = AsyncMock(
            side_effect=[operator_snapshot(active_runs=0), operator_snapshot(active_runs=1)]
        )
        request = route_request(disconnect_after=4)

        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router.capability_os_operator, "snapshot", new=snapshots),
            patch.object(mcp_router.asyncio, "sleep", new=AsyncMock()),
        ):
            response = await mcp_router.stream_capability_os_operator(
                request, task_id="task-1", limit=100
            )
            iterator = response.body_iterator.__aiter__()
            retry = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            first = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            second = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            await iterator.aclose()

        self.assertEqual(retry, "retry: 1500\n\n")
        self.assertIn("event: snapshot", first)
        self.assertIn('"activeRuns":0', first)
        self.assertIn("event: snapshot", second)
        self.assertIn('"activeRuns":1', second)
        admin.assert_called_once()
        self.assertEqual(snapshots.await_count, 2)
        for call in snapshots.await_args_list:
            self.assertEqual(call.kwargs["user_id"], "admin-1")
            self.assertEqual(call.kwargs["task_id"], "task-1")
            self.assertEqual(call.kwargs["limit"], 100)

    async def test_capability_os_stream_emits_structured_error_when_task_disappears(self):
        self.assertTrue(hasattr(mcp_router, "stream_capability_os_operator"))
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        snapshots = AsyncMock(side_effect=KeyError("task missing"))
        request = route_request(disconnect_after=2)

        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router.capability_os_operator, "snapshot", new=snapshots),
        ):
            response = await mcp_router.stream_capability_os_operator(
                request, task_id="task-1", limit=100
            )
            iterator = response.body_iterator.__aiter__()
            await asyncio.wait_for(iterator.__anext__(), timeout=1)
            error_event = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            await iterator.aclose()

        self.assertIn("event: capability_os_error", error_event)
        data_line = next(line for line in error_event.splitlines() if line.startswith("data: "))
        payload = json.loads(data_line.removeprefix("data: "))
        self.assertEqual(payload["code"], "CAPABILITY_OS_TASK_NOT_FOUND")
        self.assertEqual(payload["task_id"], "task-1")


if __name__ == "__main__":
    unittest.main()
