"""Unit tests for MCP services health scoring and maintain idempotency."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.mcp_services_health import (
    EXPECTED_CONTRACT_VERSION,
    EXPECTED_TOOL_COUNT,
    McpServicesHealthService,
    PluginIdentity,
    PluginIdentityCache,
    aggregate_bands,
    band_from_probes,
    probe_backend_readiness,
    probe_event_loop_lag,
    probe_extension_devices,
    probe_plugin_identity,
    probe_request_p95,
)
from cptr.services.mcp_services_maintain import McpServicesMaintainService


class BandScoringTests(unittest.TestCase):
    def test_empty_probes_unhealthy(self):
        self.assertEqual(band_from_probes([]), "unhealthy")

    def test_readiness_fail_is_unhealthy(self):
        probe = probe_backend_readiness(database_ready=False)
        self.assertEqual(probe.band_hint, "unhealthy")
        self.assertTrue(probe.critical)
        self.assertEqual(band_from_probes([probe]), "unhealthy")

    def test_readiness_unknown_unhealthy(self):
        probe = probe_backend_readiness(database_ready=None, error="timeout")
        self.assertEqual(probe.band_hint, "unhealthy")

    def test_event_loop_lag_boundaries(self):
        self.assertEqual(probe_event_loop_lag(10.0).band_hint, "healthy")
        self.assertEqual(probe_event_loop_lag(100.0).band_hint, "moderate")
        self.assertEqual(probe_event_loop_lag(300.0).band_hint, "unhealthy")
        self.assertEqual(probe_event_loop_lag(None).band_hint, "moderate")

    def test_request_p95_insufficient_samples_healthy(self):
        probe = probe_request_p95(5000.0, sample_count=5)
        self.assertEqual(probe.band_hint, "healthy")

    def test_request_p95_boundaries(self):
        self.assertEqual(probe_request_p95(100.0, 50).band_hint, "healthy")
        self.assertEqual(probe_request_p95(500.0, 50).band_hint, "moderate")
        self.assertEqual(probe_request_p95(1500.0, 50).band_hint, "unhealthy")

    def test_plugin_identity_unreachable_unhealthy(self):
        probes = probe_plugin_identity(PluginIdentity())
        self.assertEqual(band_from_probes(probes), "unhealthy")

    def test_plugin_contract_match_and_refresh_moderate(self):
        identity = PluginIdentity(
            version="1.4.5",
            contract_version=EXPECTED_CONTRACT_VERSION,
            tool_count=EXPECTED_TOOL_COUNT,
            refresh_required=True,
            source="test",
        )
        probes = probe_plugin_identity(identity)
        self.assertEqual(band_from_probes(probes), "moderate")

    def test_plugin_contract_drift_unhealthy(self):
        identity = PluginIdentity(
            version="1.0.0",
            contract_version="0.0.1",
            tool_count=EXPECTED_TOOL_COUNT,
            refresh_required=False,
            source="test",
        )
        probes = probe_plugin_identity(identity)
        self.assertEqual(band_from_probes(probes), "unhealthy")

    def test_extension_no_active_unhealthy(self):
        probes = probe_extension_devices(
            [{"status": "REVOKED", "connected": False}]
        )
        self.assertEqual(band_from_probes(probes), "unhealthy")

    def test_extension_active_not_connected_moderate(self):
        probes = probe_extension_devices(
            [{"status": "ACTIVE", "connected": False}]
        )
        # critical moderate -> aggregate moderate (not unhealthy)
        self.assertEqual(probes[0].band_hint, "moderate")
        self.assertEqual(band_from_probes(probes), "moderate")

    def test_extension_connected_healthy(self):
        probes = probe_extension_devices(
            [{"status": "ACTIVE", "connected": True}]
        )
        self.assertEqual(band_from_probes(probes), "healthy")

    def test_aggregate_bands(self):
        self.assertEqual(aggregate_bands(["healthy", "healthy"]), "healthy")
        self.assertEqual(aggregate_bands(["healthy", "moderate"]), "moderate")
        self.assertEqual(aggregate_bands(["moderate", "unhealthy"]), "unhealthy")


class SnapshotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_shape_with_injected_deps(self):
        cache = PluginIdentityCache()
        cache.set(
            PluginIdentity(
                version="1.4.5",
                contract_version=EXPECTED_CONTRACT_VERSION,
                tool_count=EXPECTED_TOOL_COUNT,
                refresh_required=False,
                source="test",
            )
        )
        service = McpServicesHealthService(identity_cache=cache)

        async def db_ready():
            return True

        def metrics():
            return {
                "uptime_seconds": 10,
                "event_loop": {"last_lag_ms": 5.0, "max_lag_ms": 12.0},
                "requests": {
                    "count": 100,
                    "server_error_count": 0,
                    "latency_ms": {"p50": 10, "p95": 40, "p99": 80, "samples": 50},
                },
                "process": {"rss_bytes": 1000, "open_fds": 100},
            }

        async def list_devices(user_id=None):
            return [{"device_id": "bdv_1", "status": "ACTIVE", "connected": True}]

        async def traffic():
            return {"clients": [], "sessions": [], "stream_health": {"slow_subscriber_drops": 0}}

        async def diagnostics():
            return {"stream_health": {"slow_subscriber_drops": 0}}

        snap = await service.snapshot(
            user_id="user-1",
            database_ready_fn=db_ready,
            metrics_fn=metrics,
            list_devices_fn=list_devices,
            list_leases_fn=lambda **_: [],
            traffic_snapshot_fn=traffic,
            diagnostics_snapshot_fn=diagnostics,
        )
        self.assertIn(snap["aggregate"], ("healthy", "moderate", "unhealthy"))
        self.assertEqual(len(snap["services"]), 4)
        ids = {s["id"] for s in snap["services"]}
        self.assertEqual(ids, {"backend", "plugin", "extension", "mcp_transport"})
        self.assertIn("fingerprint", snap)
        self.assertEqual(snap["plugin"]["contract_version"], EXPECTED_CONTRACT_VERSION)
        backend = next(s for s in snap["services"] if s["id"] == "backend")
        self.assertEqual(backend["band"], "healthy")

    async def test_unreachable_plugin_makes_aggregate_unhealthy(self):
        service = McpServicesHealthService(identity_cache=PluginIdentityCache())

        snap = await service.snapshot(
            user_id="user-1",
            database_ready_fn=lambda: True,
            metrics_fn=lambda: {
                "event_loop": {"last_lag_ms": 1.0},
                "requests": {"latency_ms": {"p95": 10, "samples": 0}},
                "process": {},
            },
            list_devices_fn=lambda **_: [
                {"device_id": "bdv_1", "status": "ACTIVE", "connected": True}
            ],
            list_leases_fn=lambda **_: [],
            traffic_snapshot_fn=lambda: {"stream_health": {"slow_subscriber_drops": 0}},
            diagnostics_snapshot_fn=lambda: {"stream_health": {"slow_subscriber_drops": 0}},
        )
        plugin = next(s for s in snap["services"] if s["id"] == "plugin")
        self.assertEqual(plugin["band"], "unhealthy")
        self.assertEqual(snap["aggregate"], "unhealthy")


class MaintainIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_start_returns_same_active_job(self):
        health = McpServicesHealthService(identity_cache=PluginIdentityCache())
        maintain = McpServicesMaintainService(health=health)

        async def fake_snapshot(**kwargs):
            return {
                "aggregate": "moderate",
                "services": [
                    {"id": "backend", "band": "healthy"},
                    {"id": "plugin", "band": "unhealthy"},
                ],
            }

        with patch.object(health, "snapshot", side_effect=fake_snapshot):
            with patch.object(
                maintain,
                "_playbook_backend",
                new=AsyncMock(return_value=True),
            ):
                job1 = await maintain.start(service_id="backend", owner_id="u1")
                job2 = await maintain.start(service_id="backend", owner_id="u1")
                self.assertEqual(job1.job_id, job2.job_id)
                # Allow background task to finish
                for _ in range(50):
                    current = maintain.get_job(job1.job_id)
                    if current and current.status not in ("queued", "running"):
                        break
                    await asyncio.sleep(0.02)
                final = maintain.get_job(job1.job_id)
                self.assertIsNotNone(final)
                assert final is not None
                self.assertIn(final.status, ("succeeded", "partial", "failed"))
                self.assertIsNotNone(final.post_band)


if __name__ == "__main__":
    unittest.main()
