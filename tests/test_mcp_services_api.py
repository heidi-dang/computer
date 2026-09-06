"""API contract tests for /api/mcp/services/* endpoints."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from cptr.routers import mcp as mcp_router
from cptr.services.mcp_services_health import (
    EXPECTED_CONTRACT_VERSION,
    EXPECTED_TOOL_COUNT,
    McpServicesHealthService,
    PluginIdentity,
    PluginIdentityCache,
)
from cptr.services.mcp_services_maintain import McpServicesMaintainService


def route_request(*, disconnected: bool = False):
    return SimpleNamespace(
        headers={},
        cookies={},
        client=None,
        state=SimpleNamespace(),
        is_disconnected=AsyncMock(return_value=disconnected),
    )


def make_health() -> McpServicesHealthService:
    cache = PluginIdentityCache()
    cache.set(
        PluginIdentity(
            version=EXPECTED_CONTRACT_VERSION,
            contract_version=EXPECTED_CONTRACT_VERSION,
            tool_count=EXPECTED_TOOL_COUNT,
            refresh_required=False,
            source="test",
        )
    )
    return McpServicesHealthService(identity_cache=cache, plugin_update_url="")


def snapshot_kwargs():
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
        return {
            "clients": [{"id": "chatgpt", "version": EXPECTED_CONTRACT_VERSION}],
            "sessions": [],
            "stream_health": {"slow_subscriber_drops": 0},
        }

    async def diagnostics():
        return {"stream_health": {"slow_subscriber_drops": 0}}

    return {
        "database_ready_fn": db_ready,
        "metrics_fn": metrics,
        "list_devices_fn": list_devices,
        "list_leases_fn": lambda **_: [],
        "traffic_snapshot_fn": traffic,
        "diagnostics_snapshot_fn": diagnostics,
    }


class McpServicesApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_requires_admin_and_shape(self):
        health = make_health()
        maintain = McpServicesMaintainService(health=health)
        deps = snapshot_kwargs()
        fixed = await health.snapshot(user_id="admin-1", **deps)

        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "mcp_services_health", health),
            patch.object(mcp_router, "mcp_services_maintain", maintain),
            patch.object(health, "snapshot", new=AsyncMock(return_value=fixed)),
        ):
            payload = await mcp_router.get_mcp_services_snapshot(route_request())

        admin.assert_called_once()
        self.assertIn(payload["aggregate"], ("healthy", "moderate", "unhealthy"))
        self.assertEqual(len(payload["services"]), 4)
        self.assertIn("plugin", payload)
        self.assertIn("maintain", payload)
        self.assertIn("fingerprint", payload)
        ids = {s["id"] for s in payload["services"]}
        self.assertEqual(ids, {"backend", "plugin", "extension", "mcp_transport"})

    async def test_maintain_happy_path_and_post_band(self):
        health = make_health()
        maintain = McpServicesMaintainService(health=health)
        deps = snapshot_kwargs()
        fixed = await health.snapshot(user_id="admin-1", **deps)

        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "mcp_services_health", health),
            patch.object(mcp_router, "mcp_services_maintain", maintain),
            patch.object(health, "snapshot", new=AsyncMock(return_value=fixed)),
            patch.object(maintain, "_playbook_backend", new=AsyncMock(return_value=True)),
        ):
            started = await mcp_router.start_mcp_services_maintain(
                route_request(),
                mcp_router.McpServicesMaintainRequest(service_id="backend"),
            )
            self.assertIn("job_id", started)
            job_id = started["job_id"]

            final = None
            for _ in range(50):
                final = maintain.get_job(job_id)
                if final and final.status not in ("queued", "running"):
                    break
                await asyncio.sleep(0.02)

            self.assertIsNotNone(final)
            assert final is not None
            self.assertIn(final.status, ("succeeded", "partial", "failed"))
            self.assertIsNotNone(final.post_band)

            job_payload = await mcp_router.get_mcp_services_maintain_job(
                route_request(), job_id
            )
            self.assertEqual(job_payload["job_id"], job_id)
            self.assertIn("steps", job_payload)
            self.assertEqual(job_payload["system_status"], "STABLE")
            self.assertEqual(job_payload["pass_count"], 1)

    async def test_plugin_refresh_advisory_is_action_required_with_healthy_post_band(self):
        health = make_health()
        health.identity_cache.set(
            PluginIdentity(
                version=EXPECTED_CONTRACT_VERSION,
                contract_version=EXPECTED_CONTRACT_VERSION,
                tool_count=EXPECTED_TOOL_COUNT,
                refresh_required=True,
                source="test",
            )
        )
        maintain = McpServicesMaintainService(health=health)
        fixed = await health.snapshot(user_id="admin-1", **snapshot_kwargs())
        plugin = next(item for item in fixed["services"] if item["id"] == "plugin")
        self.assertEqual(plugin["band"], "healthy")
        self.assertEqual(fixed["aggregate"], "healthy")

        with patch.object(health, "snapshot", new=AsyncMock(return_value=fixed)):
            job = await maintain.start(
                service_id="plugin",
                owner_id="admin-1",
                idempotency_key="host-refresh-advisory",
            )
            final = None
            for _ in range(50):
                final = maintain.get_job(job.job_id, owner_id="admin-1")
                if final and final.status not in ("queued", "running"):
                    break
                await asyncio.sleep(0.02)

        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final.status, "partial")
        self.assertEqual(final.system_status, "ACTION_REQUIRED")
        self.assertEqual(final.post_band, "healthy")
        self.assertEqual(final.post_aggregate, "healthy")
        self.assertEqual(final.pass_count, 1)

    async def test_maintain_rejects_unknown_service_id(self):
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        with patch.object(mcp_router, "require_admin", admin):
            with self.assertRaises(HTTPException) as raised:
                await mcp_router.start_mcp_services_maintain(
                    route_request(),
                    mcp_router.McpServicesMaintainRequest(service_id="not-a-service"),
                )
        self.assertEqual(raised.exception.status_code, 400)

    async def test_maintain_job_is_owner_scoped(self):
        health = make_health()
        maintain = McpServicesMaintainService(health=health)
        job = await maintain.start(
            service_id="backend", owner_id="admin-1", idempotency_key="owner-scope"
        )
        other_admin = Mock(return_value=SimpleNamespace(user_id="admin-2"))
        with (
            patch.object(mcp_router, "require_admin", other_admin),
            patch.object(mcp_router, "mcp_services_maintain", maintain),
        ):
            with self.assertRaises(HTTPException) as raised:
                await mcp_router.get_mcp_services_maintain_job(
                    route_request(), job.job_id
                )
        self.assertEqual(raised.exception.status_code, 404)

    async def test_maintain_conflict_returns_409_with_active_job(self):
        health = make_health()
        maintain = McpServicesMaintainService(health=health)
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        await maintain.start(
            service_id="backend", owner_id="admin-1", idempotency_key="first"
        )
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "mcp_services_maintain", maintain),
        ):
            with self.assertRaises(HTTPException) as raised:
                await mcp_router.start_mcp_services_maintain(
                    route_request(),
                    mcp_router.McpServicesMaintainRequest(
                        service_id="plugin", idempotency_key="second"
                    ),
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"], "MCP_SERVICES_MAINTAIN_ACTIVE"
        )
        self.assertTrue(raised.exception.detail["active_job_id"].startswith("msvc_"))

    async def test_idempotency_key_reuse_for_different_service_returns_409(self):
        health = make_health()
        maintain = McpServicesMaintainService(health=health)
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        await maintain.start(
            service_id="backend", owner_id="admin-1", idempotency_key="same-key"
        )
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "mcp_services_maintain", maintain),
        ):
            with self.assertRaises(HTTPException) as raised:
                await mcp_router.start_mcp_services_maintain(
                    route_request(),
                    mcp_router.McpServicesMaintainRequest(
                        service_id="plugin", idempotency_key="same-key"
                    ),
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"], "MCP_SERVICES_IDEMPOTENCY_CONFLICT"
        )
        self.assertTrue(raised.exception.detail["job_id"].startswith("msvc_"))

    async def test_maintain_job_not_found(self):
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        maintain = McpServicesMaintainService()
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "mcp_services_maintain", maintain),
        ):
            with self.assertRaises(HTTPException) as raised:
                await mcp_router.get_mcp_services_maintain_job(
                    route_request(), "msvc_missing"
                )
        self.assertEqual(raised.exception.status_code, 404)

    async def test_failure_path_backend_readiness_fails_job(self):
        health = McpServicesHealthService(
            identity_cache=PluginIdentityCache(), plugin_update_url=""
        )
        maintain = McpServicesMaintainService(health=health)

        async def snapshot(**kwargs):
            return {
                "aggregate": "unhealthy",
                "services": [{"id": "backend", "band": "unhealthy"}],
            }

        with (
            patch.object(health, "snapshot", side_effect=snapshot),
            patch(
                "cptr.utils.db.database_ready",
                new=AsyncMock(return_value=False),
            ),
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
            self.assertIn(final.status, ("failed", "partial"))
            self.assertTrue(
                any(
                    s.step_id == "backend.recheck_readiness" and s.result == "failed"
                    for s in final.steps
                )
            )
            self.assertEqual(final.post_band, "unhealthy")


if __name__ == "__main__":
    unittest.main()
