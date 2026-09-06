"""Tests for deterministic, bounded maintenance convergence."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from cptr.services.maintenance_orchestrator import MaintenanceOrchestrator


class MaintenanceOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_stabilize_all_retries_only_nonhealthy_targets(self):
        orchestrator = MaintenanceOrchestrator(max_passes=2)
        repair = AsyncMock(return_value=True)
        snapshots = AsyncMock(
            side_effect=[
                {
                    "aggregate": "unhealthy",
                    "services": [
                        {"id": "backend", "band": "unhealthy"},
                        {"id": "plugin", "band": "healthy"},
                        {"id": "extension", "band": "healthy"},
                        {"id": "mcp_transport", "band": "healthy"},
                    ],
                },
                {
                    "aggregate": "healthy",
                    "services": [
                        {"id": "backend", "band": "healthy"},
                        {"id": "plugin", "band": "healthy"},
                        {"id": "extension", "band": "healthy"},
                        {"id": "mcp_transport", "band": "healthy"},
                    ],
                },
            ]
        )

        result = await orchestrator.stabilize(
            service_id="all",
            repair_fn=repair,
            snapshot_fn=snapshots,
        )

        self.assertEqual(result.system_status, "STABLE")
        self.assertEqual(result.pass_count, 2)
        self.assertEqual(
            [call.args[0] for call in repair.await_args_list],
            ["backend", "plugin", "extension", "mcp_transport", "backend"],
        )
        self.assertEqual(snapshots.await_count, 2)

    async def test_healthy_final_state_can_require_external_action(self):
        orchestrator = MaintenanceOrchestrator(max_passes=1)
        repair = AsyncMock(return_value=True)
        snapshots = AsyncMock(
            return_value={
                "aggregate": "healthy",
                "services": [{"id": "plugin", "band": "healthy"}],
            }
        )

        result = await orchestrator.stabilize(
            service_id="plugin",
            repair_fn=repair,
            snapshot_fn=snapshots,
            action_required_fn=lambda: True,
        )

        self.assertEqual(result.system_status, "ACTION_REQUIRED")
        self.assertEqual(result.final_band, "healthy")

    async def test_moderate_final_state_can_require_external_action(self):
        orchestrator = MaintenanceOrchestrator(max_passes=1)
        repair = AsyncMock(return_value=True)
        snapshots = AsyncMock(
            return_value={
                "aggregate": "moderate",
                "services": [{"id": "plugin", "band": "moderate"}],
            }
        )

        result = await orchestrator.stabilize(
            service_id="plugin",
            repair_fn=repair,
            snapshot_fn=snapshots,
            action_required_fn=lambda: True,
        )

        self.assertEqual(result.system_status, "ACTION_REQUIRED")
        self.assertEqual(result.final_band, "moderate")

    async def test_unhealthy_after_budget_is_failed(self):
        orchestrator = MaintenanceOrchestrator(max_passes=2)
        repair = AsyncMock(return_value=False)
        snapshots = AsyncMock(
            return_value={
                "aggregate": "unhealthy",
                "services": [{"id": "backend", "band": "unhealthy"}],
            }
        )

        result = await orchestrator.stabilize(
            service_id="backend",
            repair_fn=repair,
            snapshot_fn=snapshots,
        )

        self.assertEqual(result.system_status, "FAILED")
        self.assertEqual(result.pass_count, 2)
        self.assertEqual(repair.await_count, 2)


if __name__ == "__main__":
    unittest.main()
