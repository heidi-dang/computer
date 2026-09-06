"""Unit tests for MCP services health scoring and maintain idempotency."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

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
    probe_open_fds,
    probe_plugin_identity,
    probe_request_p95,
)
from cptr.services.mcp_services_maintain import (
    MaintainConflictError,
    MaintainIdempotencyConflictError,
    MaintainJob,
    McpServicesMaintainService,
)


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

    def test_request_p95_insufficient_samples_fail_closed(self):
        probe = probe_request_p95(5000.0, sample_count=5)
        self.assertEqual(probe.band_hint, "moderate")
        self.assertFalse(probe.ok)

    def test_open_fds_unavailable_fail_closed(self):
        probe = probe_open_fds(None)
        self.assertEqual(probe.band_hint, "moderate")
        self.assertFalse(probe.ok)

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
        service = McpServicesHealthService(identity_cache=cache, plugin_update_url="")

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
        service = McpServicesHealthService(
            identity_cache=PluginIdentityCache(), plugin_update_url=""
        )

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

    async def test_chatgpt_client_version_is_not_plugin_identity(self):
        service = McpServicesHealthService(
            identity_cache=PluginIdentityCache(), plugin_update_url=""
        )
        snap = await service.snapshot(
            user_id="user-1",
            database_ready_fn=lambda: True,
            metrics_fn=lambda: {
                "event_loop": {"last_lag_ms": 1.0},
                "requests": {"latency_ms": {"p95": 10, "samples": 50}},
                "process": {"open_fds": 10},
            },
            list_devices_fn=lambda **_: [
                {"device_id": "bdv_1", "status": "ACTIVE", "connected": True}
            ],
            list_leases_fn=lambda **_: [],
            traffic_snapshot_fn=lambda: {
                "clients": [{"id": "chatgpt", "version": EXPECTED_CONTRACT_VERSION}],
                "sessions": [],
                "stream_health": {"slow_subscriber_drops": 0},
            },
            diagnostics_snapshot_fn=lambda: {"stream_health": {"slow_subscriber_drops": 0}},
        )
        self.assertIsNone(snap["plugin"]["contract_version"])
        self.assertEqual(snap["plugin"]["band"], "unhealthy")

    async def test_plugin_manifest_refresh_is_authoritative(self):
        service = McpServicesHealthService(
            identity_cache=PluginIdentityCache(), plugin_update_url=""
        )
        identity = await service.refresh_plugin_identity(
            manifest_fn=lambda: {
                "version": EXPECTED_CONTRACT_VERSION,
                "contract_version": EXPECTED_CONTRACT_VERSION,
                "tool_count": EXPECTED_TOOL_COUNT,
                "release_sha": "abc123",
                "refresh_required": False,
            }
        )
        self.assertEqual(identity.source, "plugin_update")
        self.assertEqual(identity.contract_version, EXPECTED_CONTRACT_VERSION)
        self.assertEqual(identity.tool_count, EXPECTED_TOOL_COUNT)
        self.assertEqual(band_from_probes(probe_plugin_identity(identity)), "healthy")


class MaintainPlaybookTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_playbook_runs_checkpoint_and_registry_reconcile(self):
        class FakeResult:
            def first(self):
                return (0, 12, 12)

        class FakeConnection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def execute(self, _statement):
                return FakeResult()

        class FakeEngine:
            def connect(self):
                return FakeConnection()

        registry = Mock()
        registry.reconcile.return_value = 2
        registry.reconcile_launch_reservations.return_value = 1
        registry.reap.return_value = ["cmd-old"]
        registry.active_count.return_value = 3
        registry.capacity_count.return_value = 4
        metrics = Mock()
        metrics.snapshot.return_value = {
            "event_loop": {"last_lag_ms": 5.0},
            "uptime_seconds": 42,
        }
        health = McpServicesHealthService(
            identity_cache=PluginIdentityCache(), plugin_update_url=""
        )
        maintain = McpServicesMaintainService(health=health)
        job = MaintainJob(job_id="msvc_test", service_id="backend", owner_id="u1")

        with (
            patch("cptr.utils.db.database_ready", new=AsyncMock(return_value=True)),
            patch("cptr.utils.db.get_engine", return_value=FakeEngine()),
            patch(
                "cptr.services.execution_manager.command_session_registry",
                registry,
            ),
            patch("cptr.services.runtime_metrics.runtime_metrics", metrics),
        ):
            ok = await maintain._playbook_backend(job)

        self.assertTrue(ok)
        by_id = {step.step_id: step for step in job.steps}
        self.assertEqual(by_id["backend.wal_checkpoint"].result, "ok")
        self.assertEqual(
            by_id["backend.wal_checkpoint"].evidence["checkpointed_frames"], 12
        )
        self.assertEqual(by_id["backend.reconcile_sessions"].result, "ok")
        self.assertEqual(
            by_id["backend.reconcile_sessions"].evidence["reconciled_sessions"], 2
        )
        self.assertEqual(
            by_id["backend.reconcile_sessions"].evidence[
                "released_orphan_reservations"
            ],
            1,
        )
        self.assertEqual(
            by_id["backend.reconcile_sessions"].evidence[
                "reaped_completed_sessions"
            ],
            1,
        )
        registry.reap.assert_called_once()

    async def test_mcp_playbook_expires_stale_sessions_before_reprobe(self):
        health = McpServicesHealthService(
            identity_cache=PluginIdentityCache(), plugin_update_url=""
        )
        maintain = McpServicesMaintainService(health=health)
        job = MaintainJob(job_id="msvc_mcp", service_id="mcp_transport", owner_id="u1")

        traffic = Mock()
        traffic.expire_stale_sessions = AsyncMock(return_value=3)
        traffic.snapshot = AsyncMock(
            return_value={"stream_health": {"slow_subscriber_drops": 0}}
        )
        diagnostics = Mock()
        diagnostics.snapshot = AsyncMock(
            return_value={"stream_health": {"slow_subscriber_drops": 0}}
        )

        with (
            patch("cptr.services.mcp_traffic.mcp_traffic_store", traffic),
            patch("cptr.services.mcp_diagnostics.mcp_diagnostics_store", diagnostics),
        ):
            ok = await maintain._playbook_mcp(job)

        self.assertTrue(ok)
        by_id = {step.step_id: step for step in job.steps}
        self.assertEqual(by_id["mcp.expire_stale_sessions"].result, "ok")
        self.assertEqual(
            by_id["mcp.expire_stale_sessions"].evidence["expired_sessions"], 3
        )
        traffic.expire_stale_sessions.assert_awaited_once()
        traffic.snapshot.assert_awaited_once()
        diagnostics.snapshot.assert_awaited_once()


class MaintainIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self):
        health = McpServicesHealthService(
            identity_cache=PluginIdentityCache(), plugin_update_url=""
        )
        return health, McpServicesMaintainService(health=health)

    async def test_same_owner_idempotency_key_replays_same_job(self):
        health, maintain = self.make_service()
        with (
            patch.object(
                health,
                "snapshot",
                new=AsyncMock(
                    return_value={
                        "aggregate": "healthy",
                        "services": [{"id": "backend", "band": "healthy"}],
                    }
                ),
            ),
            patch.object(maintain, "_playbook_backend", new=AsyncMock(return_value=True)),
        ):
            job1 = await maintain.start(
                service_id="backend", owner_id="u1", idempotency_key="same"
            )
            job2 = await maintain.start(
                service_id="backend", owner_id="u1", idempotency_key="same"
            )
            self.assertEqual(job1.job_id, job2.job_id)

    async def test_same_idempotency_key_with_different_service_conflicts(self):
        health, maintain = self.make_service()
        with patch.object(maintain, "_playbook_backend", new=AsyncMock(return_value=True)):
            first = await maintain.start(
                service_id="backend", owner_id="u1", idempotency_key="same"
            )
            with self.assertRaises(MaintainIdempotencyConflictError) as raised:
                await maintain.start(
                    service_id="plugin", owner_id="u1", idempotency_key="same"
                )
        self.assertEqual(raised.exception.job_id, first.job_id)

    async def test_different_request_while_owner_job_active_conflicts(self):
        health, maintain = self.make_service()
        with patch.object(maintain, "_playbook_backend", new=AsyncMock(return_value=True)):
            await maintain.start(
                service_id="backend", owner_id="u1", idempotency_key="first"
            )
            with self.assertRaises(MaintainConflictError):
                await maintain.start(
                    service_id="plugin", owner_id="u1", idempotency_key="second"
                )

    async def test_active_jobs_are_owner_scoped(self):
        health, maintain = self.make_service()
        with patch.object(maintain, "_playbook_backend", new=AsyncMock(return_value=True)):
            first = await maintain.start(service_id="backend", owner_id="u1")
            second = await maintain.start(service_id="backend", owner_id="u2")
            self.assertNotEqual(first.job_id, second.job_id)
            self.assertEqual(maintain.active_job_id(owner_id="u1"), first.job_id)
            self.assertEqual(maintain.active_job_id(owner_id="u2"), second.job_id)
            self.assertIsNone(maintain.get_job(first.job_id, owner_id="u2"))

    async def test_maintain_all_runs_every_service_after_backend_failure(self):
        health, maintain = self.make_service()
        playbook = AsyncMock(side_effect=[False, True, True, True])
        with (
            patch.object(
                health,
                "snapshot",
                new=AsyncMock(return_value={"aggregate": "unhealthy", "services": []}),
            ),
            patch.object(maintain, "_run_service_playbook", playbook),
        ):
            job = await maintain.start(service_id="all", owner_id="u1")
            for _ in range(50):
                current = maintain.get_job(job.job_id)
                if current and current.status not in ("queued", "running"):
                    break
                await asyncio.sleep(0.02)

        self.assertEqual(playbook.await_count, 4)
        self.assertEqual(
            [call.args[1] for call in playbook.await_args_list],
            ["backend", "plugin", "extension", "mcp_transport"],
        )
        final = maintain.get_job(job.job_id)
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final.status, "failed")

    async def test_unhealthy_postcheck_cannot_succeed(self):
        health, maintain = self.make_service()
        with (
            patch.object(
                health,
                "snapshot",
                new=AsyncMock(
                    return_value={
                        "aggregate": "unhealthy",
                        "services": [{"id": "backend", "band": "unhealthy"}],
                    }
                ),
            ),
            patch.object(maintain, "_playbook_backend", new=AsyncMock(return_value=True)),
        ):
            job = await maintain.start(service_id="backend", owner_id="u1")
            for _ in range(50):
                current = maintain.get_job(job.job_id)
                if current and current.status not in ("queued", "running"):
                    break
                await asyncio.sleep(0.02)
            final = maintain.get_job(job.job_id)
            self.assertIsNotNone(final)
            assert final is not None
            self.assertEqual(final.post_band, "unhealthy")
            self.assertEqual(final.status, "failed")


if __name__ == "__main__":
    unittest.main()
