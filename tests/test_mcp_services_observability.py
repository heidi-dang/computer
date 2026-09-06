"""Performance contracts for the compact Services observability projection."""

from __future__ import annotations

import unittest

from cptr.services.mcp_services_observability import McpServicesObservability


class McpServicesObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_composes_only_bounded_summaries(self):
        observability = McpServicesObservability(
            runtime_snapshot_fn=lambda: {
                "uptime_seconds": 120,
                "requests": {
                    "count": 40,
                    "server_error_count": 2,
                    "latency_ms": {"p95": 85.0, "samples": 40},
                },
                "database": {
                    "query_count": 80,
                    "error_count": 1,
                    "busy_count": 3,
                    "latency_ms": {"p95": 7.0, "samples": 80},
                },
                "event_loop": {"last_lag_ms": 1.5, "max_lag_ms": 8.0},
                "process": {"rss_bytes": 1000, "open_fds": 38},
            },
            command_metrics_fn=lambda: {
                "active": 1,
                "launching": 1,
                "capacity_used": 2,
                "capacity_limit": 5,
                "completed_retained": 7,
                "exited_unreconciled": 0,
                "terminal_events_published": 12,
                "terminal_event_dropped_bytes": 0,
            },
            live_event_stats_fn=lambda: {
                "queue_depth": 3,
                "queue_capacity": 8192,
                "subscriber_count": 1,
                "slow_subscriber_disconnects": 0,
            },
            diagnostics_summary_fn=lambda: {
                "latency": {
                    "cptr-mcp-cptr-backend": {
                        "health_p95_ms": 72,
                        "health_sample_count": 12,
                        "health": "healthy",
                    }
                },
                "failure_count": 2,
                "latest_system": {
                    "timestamp_ms": 1234,
                    "cpu_usage_percent": 20.0,
                    "memory_total_bytes": 1000,
                    "memory_available_bytes": 600,
                    "disk_total_bytes": 2000,
                    "disk_free_bytes": 1000,
                    "cptr_process": {"pid": 42, "cpu_percent": 3.0, "memory_percent": 4.0},
                    "gpu_status": "unavailable",
                    "gpus": [],
                },
                "stream_health": {"subscriber_count": 1, "slow_subscriber_drops": 0},
            },
            traffic_summary_fn=lambda: {
                "client_count": 1,
                "session_count": 1,
                "active_requests": 2,
                "total_requests": 10,
                "errors": 1,
                "stream_health": {"subscriber_count": 1, "slow_subscriber_drops": 0},
            },
            worker_snapshot_fn=lambda: {
                "aggregate": "healthy",
                "worker_count": 6,
                "workers": {
                    "command_reaper": {"status": "healthy", "restart_count_window": 0},
                    "event_loop_monitor": {"status": "restarted", "restart_count_window": 1},
                    "memory_maintenance": {"status": "healthy", "restart_count_window": 0},
                    "workbench_reaper": {"status": "healthy", "restart_count_window": 0},
                    "automation_scheduler": {"status": "healthy", "restart_count_window": 0},
                    "timer_worker": {"status": "healthy", "restart_count_window": 0},
                },
            },
        )

        snapshot = await observability.snapshot()

        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["runtime"]["requests"]["p95_ms"], 85.0)
        self.assertEqual(snapshot["execution"]["commands"]["capacity_used"], 2)
        self.assertEqual(snapshot["workers"]["healthy"], 6)
        self.assertEqual(snapshot["workers"]["restarts"], 1)
        self.assertEqual(snapshot["mcp"]["active_requests"], 2)
        self.assertEqual(snapshot["mcp"]["backend_rtt_p95_ms"], 72)
        self.assertEqual(snapshot["host"]["cpu_usage_percent"], 20.0)
        self.assertEqual(snapshot["pressure"]["live_event_queue_percent"], 0.04)
        self.assertNotIn("events", snapshot)
        self.assertNotIn("failures", snapshot)


if __name__ == "__main__":
    unittest.main()
