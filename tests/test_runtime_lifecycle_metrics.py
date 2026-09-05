import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cptr.services.browser_device_connections import BrowserDeviceConnectionRegistry
from cptr.services.browser_devices import BrowserDeviceStore
from cptr.services.runtime_metrics import RuntimeMetrics
from cptr.utils.browser.proxy import BrowserProxyManager


class RuntimeProcessMetricsTests(unittest.TestCase):
    def test_snapshot_exposes_monotonic_process_cpu_seconds(self):
        metrics = RuntimeMetrics()
        first = metrics.snapshot()["process"]
        second = metrics.snapshot()["process"]

        self.assertIsInstance(first["cpu_seconds"], float)
        self.assertGreaterEqual(second["cpu_seconds"], first["cpu_seconds"])
        self.assertIn("rss_bytes", second)
        self.assertIn("open_fds", second)


class BrowserDeviceLifecycleMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_metrics_are_owner_scoped_aggregates(self):
        store = BrowserDeviceStore()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        db.scalar.return_value = 2

        all_sessions = MagicMock()
        all_sessions.all.return_value = [("AGENT_CONTROL", 2), ("DISCONNECTED", 5)]
        active_sessions = MagicMock()
        active_sessions.all.return_value = [("AGENT_CONTROL", 2)]
        active_leases = MagicMock()
        active_leases.all.return_value = [("agent", 2)]
        db.execute.side_effect = [all_sessions, active_sessions, active_leases]

        with patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)):
            result = await store.runtime_metrics(user_id="user_1")

        self.assertEqual(result["devices_total"], 2)
        self.assertEqual(result["sessions_total"], 7)
        self.assertEqual(result["active_sessions"], 2)
        self.assertEqual(result["active_leases"], 2)
        self.assertEqual(result["active_sessions_by_state"], {"AGENT_CONTROL": 2})
        self.assertEqual(result["leases_by_owner"], {"agent": 2})


class BrowserProxyLifecycleMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_session_count_tracks_create_and_close(self):
        manager = BrowserProxyManager()
        session = await manager.create("user_1")
        self.assertEqual(manager.count(), 1)
        self.assertTrue(await manager.close(session.session_id, "user_1"))
        self.assertEqual(manager.count(), 0)
        await manager.close_all()


class BrowserConnectionLifecycleMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_connected_device_count_tracks_attach_and_detach(self):
        registry = BrowserDeviceConnectionRegistry()
        websocket = AsyncMock()

        await registry.attach(device_id="device_1", websocket=websocket)
        self.assertEqual(await registry.count(), 1)
        self.assertEqual(await registry.count(device_ids={"device_1"}), 1)
        self.assertEqual(await registry.count(device_ids={"device_2"}), 0)
        self.assertTrue(await registry.detach(device_id="device_1", websocket=websocket))
        self.assertEqual(await registry.count(), 0)


if __name__ == "__main__":
    unittest.main()
