import asyncio
import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.system_metrics import (
    BackendCounterSnapshot,
    BackendMetricsSampler,
    _collect_gpu_metrics,
    derive_backend_metrics,
)


class SystemMetricsDerivationTests(unittest.TestCase):
    def test_linux_like_counter_deltas_produce_expected_rates(self):
        previous = BackendCounterSnapshot(
            timestamp_ms=1000,
            cpu_total=1000,
            cpu_idle=400,
            cpu_count=8,
            memory_total=1000,
            memory_available=400,
            disk_total=2000,
            disk_used=1000,
            disk_free=1000,
            disk_read_bytes=10_000,
            disk_write_bytes=20_000,
            disk_read_ops=100,
            disk_write_ops=200,
            network_rx_bytes=30_000,
            network_tx_bytes=40_000,
            uptime_seconds=10,
            load_avg=[0.5, 0.4, 0.3],
            cptr_process_cpu_ticks=100,
            cptr_process_rss_bytes=100,
            clock_ticks_per_second=100,
            cptr_process_name="cptr",
            processes=[],
            gpus=[],
            gpu_status="unavailable",
        )
        current = replace(
            previous,
            timestamp_ms=2000,
            cpu_total=1200,
            cpu_idle=450,
            disk_read_bytes=12_000,
            disk_write_bytes=25_000,
            disk_read_ops=110,
            disk_write_ops=220,
            network_rx_bytes=33_000,
            network_tx_bytes=44_000,
            cptr_process_cpu_ticks=150,
            cptr_process_rss_bytes=120,
        )

        sample = derive_backend_metrics(previous, current)
        self.assertEqual(sample.cpu_usage_percent, 75.0)
        self.assertEqual(sample.disk_read_bytes_per_s, 2000.0)
        self.assertEqual(sample.disk_write_bytes_per_s, 5000.0)
        self.assertEqual(sample.disk_read_ops_per_s, 10.0)
        self.assertEqual(sample.disk_write_ops_per_s, 20.0)
        self.assertEqual(sample.network_rx_bytes_per_s, 3000.0)
        self.assertEqual(sample.network_tx_bytes_per_s, 4000.0)
        self.assertIsNotNone(sample.cptr_process)
        self.assertEqual(sample.cptr_process.pid, os.getpid())
        self.assertEqual(sample.cptr_process.name, "cptr")
        self.assertEqual(sample.cptr_process.cpu_percent, 50.0)
        self.assertEqual(sample.cptr_process.memory_percent, 12.0)

    def test_first_sample_has_current_capacity_but_no_rates(self):
        current = BackendCounterSnapshot(
            timestamp_ms=1000,
            cpu_total=1000,
            cpu_idle=400,
            cpu_count=8,
            memory_total=2000,
            memory_available=500,
            disk_total=4000,
            disk_used=1000,
            disk_free=3000,
            disk_read_bytes=10_000,
            disk_write_bytes=20_000,
            disk_read_ops=100,
            disk_write_ops=200,
            network_rx_bytes=30_000,
            network_tx_bytes=40_000,
            uptime_seconds=10,
            load_avg=[0.5],
            cptr_process_cpu_ticks=100,
            cptr_process_rss_bytes=100,
            clock_ticks_per_second=100,
            cptr_process_name="cptr",
            processes=[],
            gpus=[],
            gpu_status="unavailable",
        )
        sample = derive_backend_metrics(None, current)
        self.assertIsNone(sample.cpu_usage_percent)
        self.assertEqual(sample.memory_total_bytes, 2000)
        self.assertEqual(sample.disk_total_bytes, 4000)
        self.assertIsNone(sample.network_rx_bytes_per_s)


class SystemMetricsGpuTests(unittest.TestCase):
    def test_gpu_unavailable_when_nvidia_smi_is_absent(self):
        with patch("cptr.services.system_metrics.shutil.which", return_value=None):
            status, gpus = _collect_gpu_metrics()
        self.assertEqual(status, "unavailable")
        self.assertEqual(gpus, [])

    def test_gpu_csv_is_parsed_into_bounded_numeric_values(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="0, NVIDIA RTX 2080 Ti, 42, 1024, 11264, 55\n",
        )
        with (
            patch("cptr.services.system_metrics.shutil.which", return_value="/usr/bin/nvidia-smi"),
            patch("cptr.services.system_metrics.subprocess.run", return_value=completed) as run,
        ):
            status, gpus = _collect_gpu_metrics()
        self.assertEqual(status, "available")
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0].index, 0)
        self.assertEqual(gpus[0].name, "NVIDIA RTX 2080 Ti")
        self.assertEqual(gpus[0].utilization_percent, 42)
        self.assertEqual(gpus[0].memory_used_bytes, 1024 * 1024 * 1024)
        self.assertEqual(gpus[0].memory_total_bytes, 11264 * 1024 * 1024)
        self.assertEqual(gpus[0].temperature_c, 55)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 3)


class SystemMetricsSamplerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sample_once_collects_off_loop_and_records_derived_sample(self):
        store = SimpleNamespace(record_system_sample=AsyncMock())
        sampler = BackendMetricsSampler(store, interval_seconds=1)
        current = BackendCounterSnapshot(
            timestamp_ms=1000,
            cpu_total=1000,
            cpu_idle=400,
            cpu_count=8,
            memory_total=2000,
            memory_available=500,
            disk_total=4000,
            disk_used=1000,
            disk_free=3000,
            disk_read_bytes=None,
            disk_write_bytes=None,
            disk_read_ops=None,
            disk_write_ops=None,
            network_rx_bytes=None,
            network_tx_bytes=None,
            uptime_seconds=10,
            load_avg=[],
            cptr_process_cpu_ticks=None,
            cptr_process_rss_bytes=None,
            clock_ticks_per_second=None,
            cptr_process_name="cptr",
            processes=[],
            gpus=[],
            gpu_status="unavailable",
        )
        with patch("cptr.services.system_metrics.collect_backend_counters", return_value=current):
            sample = await asyncio.wait_for(sampler.sample_once(), timeout=0.5)
        self.assertEqual(sample.timestamp_ms, 1000)
        store.record_system_sample.assert_awaited_once_with(sample)
        await sampler.close()


if __name__ == "__main__":
    unittest.main()
