"""Deterministic tests for CPTR's model-free background worker watchdog."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from cptr.services.worker_watchdog import WorkerWatchdog, configure_runtime_workers


async def _forever() -> None:
    while True:
        await asyncio.sleep(3600)


class WorkerWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        watchdog = getattr(self, "watchdog", None)
        if watchdog is not None:
            await watchdog.close()

    async def test_core_registration_covers_permanent_runtime_workers(self):
        self.watchdog = WorkerWatchdog(check_interval_seconds=60)
        configure_runtime_workers(SimpleNamespace(), watchdog=self.watchdog)

        snapshot = await self.watchdog.snapshot()

        self.assertEqual(
            set(snapshot["workers"]),
            {
                "command_reaper",
                "workbench_reaper",
                "event_loop_monitor",
                "memory_maintenance",
                "automation_scheduler",
                "timer_worker",
            },
        )

    async def test_reconcile_restarts_missing_worker_and_reports_evidence(self):
        self.watchdog = WorkerWatchdog(check_interval_seconds=60)
        self.watchdog.register(
            name="scheduler",
            task_factory=_forever,
            stale_after_seconds=30,
            restart_limit=3,
            restart_window_seconds=300,
        )

        result = await self.watchdog.reconcile()

        self.assertEqual(result["aggregate"], "healthy")
        worker = result["workers"]["scheduler"]
        self.assertEqual(worker["status"], "restarted")
        self.assertEqual(worker["restart_count_window"], 1)
        self.assertTrue(worker["task_running"])

    async def test_restart_preserves_error_until_replacement_succeeds(self):
        attempts = 0

        async def flaky() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("first boot failed")
            await _forever()

        self.watchdog = WorkerWatchdog(check_interval_seconds=60)
        self.watchdog.register(
            name="flaky",
            task_factory=flaky,
            stale_after_seconds=30,
            restart_limit=3,
            restart_window_seconds=300,
        )

        await self.watchdog.reconcile()
        await asyncio.sleep(0)
        restarted = await self.watchdog.reconcile()

        worker = restarted["workers"]["flaky"]
        self.assertEqual(worker["status"], "restarted")
        self.assertIn("first boot failed", worker["last_error"])

        self.watchdog.heartbeat("flaky", success=True)
        healthy = await self.watchdog.snapshot()
        self.assertIsNone(healthy["workers"]["flaky"]["last_error"])

    async def test_restart_budget_opens_circuit_instead_of_restart_loop(self):
        async def crash() -> None:
            raise RuntimeError("boom")

        self.watchdog = WorkerWatchdog(check_interval_seconds=60)
        self.watchdog.register(
            name="crasher",
            task_factory=crash,
            stale_after_seconds=30,
            restart_limit=2,
            restart_window_seconds=300,
        )

        await self.watchdog.reconcile()
        await asyncio.sleep(0)
        await self.watchdog.reconcile()
        await asyncio.sleep(0)
        result = await self.watchdog.reconcile()

        worker = result["workers"]["crasher"]
        self.assertEqual(worker["status"], "restart_suspended")
        self.assertEqual(worker["restart_count_window"], 2)
        self.assertEqual(result["aggregate"], "unhealthy")
        self.assertIn("boom", worker["last_error"])

    async def test_stale_heartbeat_is_reported_without_cancelling_live_task(self):
        now = 1000.0
        self.watchdog = WorkerWatchdog(check_interval_seconds=60, monotonic_fn=lambda: now)
        self.watchdog.register(
            name="memory",
            task_factory=_forever,
            stale_after_seconds=10,
            restart_limit=3,
            restart_window_seconds=300,
        )
        await self.watchdog.reconcile()
        self.watchdog.heartbeat("memory", now=980.0)

        result = await self.watchdog.reconcile()

        worker = result["workers"]["memory"]
        self.assertEqual(worker["status"], "stalled")
        self.assertTrue(worker["task_running"])
        self.assertEqual(result["aggregate"], "moderate")

    async def test_fresh_liveness_with_stale_success_is_degraded(self):
        clock = [1000.0]
        self.watchdog = WorkerWatchdog(
            check_interval_seconds=60, monotonic_fn=lambda: clock[0]
        )
        self.watchdog.register(
            name="scheduler",
            task_factory=_forever,
            stale_after_seconds=10,
            success_stale_after_seconds=10,
            restart_limit=3,
            restart_window_seconds=300,
        )
        await self.watchdog.reconcile()
        self.watchdog.heartbeat("scheduler", now=1000.0, success=True)
        clock[0] = 1020.0
        self.watchdog.heartbeat("scheduler", now=1019.0, success=False)

        result = await self.watchdog.snapshot()

        worker = result["workers"]["scheduler"]
        self.assertEqual(worker["status"], "degraded")
        self.assertTrue(worker["task_running"])
        self.assertEqual(result["aggregate"], "moderate")

    async def test_heartbeat_restores_running_status(self):
        now = 1000.0
        self.watchdog = WorkerWatchdog(check_interval_seconds=60, monotonic_fn=lambda: now)
        self.watchdog.register(
            name="timer",
            task_factory=_forever,
            stale_after_seconds=10,
            restart_limit=3,
            restart_window_seconds=300,
        )
        await self.watchdog.reconcile()
        self.watchdog.heartbeat("timer", now=999.0)

        result = await self.watchdog.reconcile()

        worker = result["workers"]["timer"]
        self.assertEqual(worker["status"], "healthy")
        self.assertEqual(result["aggregate"], "healthy")


if __name__ == "__main__":
    unittest.main()
