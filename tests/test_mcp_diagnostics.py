import asyncio
import unittest

from pydantic import ValidationError

from cptr.services.mcp_diagnostics import (
    McpBackendMetricsSample,
    McpDiagnosticsStore,
    McpFailureDiagnostic,
    McpGpuMetrics,
    McpLatencySample,
    McpProcessMetrics,
)

BASE_TS = 1_788_000_000_000


def latency(event_id: str, duration_ms: int, *, status: str = "ok") -> McpLatencySample:
    return McpLatencySample(
        event_id=event_id,
        timestamp_ms=BASE_TS + duration_ms,
        request_id="request-1",
        correlation_id="corr-1",
        edge_id="cptr-mcp-cptr-backend",
        metric_type="backend_api_rtt",
        duration_ms=duration_ms,
        status=status,
    )


def failure(diagnostic_id: str, *, summary: str = "Backend request failed") -> McpFailureDiagnostic:
    return McpFailureDiagnostic(
        diagnostic_id=diagnostic_id,
        request_id="request-1",
        correlation_id="corr-1",
        session_id="session-1",
        client_id="chatgpt",
        method="tools/call",
        tool_name="cptr_list_workspaces",
        stage="cptr_backend",
        error_code="backend_unavailable",
        http_status=503,
        retryable=True,
        started_at_ms=BASE_TS,
        completed_at_ms=BASE_TS + 25,
        duration_ms=25,
        request_bytes=100,
        response_bytes=20,
        summary=summary,
    )


class McpDiagnosticsSchemaTests(unittest.TestCase):
    def test_strict_models_reject_extra_sensitive_fields(self):
        payload = failure("diagnostic-001").model_dump()
        for key in ("authorization", "headers", "stack"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                McpFailureDiagnostic.model_validate({**payload, key: "must-not-survive"})

    def test_gpu_process_and_system_bounds_are_strict(self):
        gpu = McpGpuMetrics(
            index=0,
            name="NVIDIA RTX 2080 Ti",
            utilization_percent=42,
            memory_used_bytes=1024,
            memory_total_bytes=11264,
            temperature_c=55,
        )
        process = McpProcessMetrics(pid=123, cpu_percent=10, memory_percent=2, name="cptr")
        sample = McpBackendMetricsSample(
            timestamp_ms=BASE_TS,
            cpu_usage_percent=25,
            cpu_count=8,
            load_avg=[0.5, 0.4, 0.3],
            gpu_status="available",
            gpus=[gpu],
            cptr_process=process,
            processes=[process],
        )
        self.assertEqual(sample.gpus[0].utilization_percent, 42)
        with self.assertRaises(ValidationError):
            McpBackendMetricsSample.model_validate({**sample.model_dump(), "headers": {}})


class McpDiagnosticsStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_latency_snapshot_uses_nearest_rank_percentiles_and_health(self):
        store = McpDiagnosticsStore(
            max_latency_samples_per_edge=5,
            max_failures=4,
            max_system_samples=3,
            subscriber_queue_size=2,
        )
        result = await store.ingest(
            [
                latency(f"latency-{index:03}", value)
                for index, value in enumerate([10, 20, 30, 40, 100], 1)
            ]
        )
        self.assertEqual(result, {"accepted": 5, "duplicates": 0, "dropped": 0})

        snapshot = await store.snapshot()
        aggregate = snapshot["latency"]["cptr-mcp-cptr-backend"]
        self.assertEqual(aggregate["latest_ms"], 100)
        self.assertEqual(aggregate["average_ms"], 40.0)
        self.assertEqual(aggregate["p50_ms"], 30)
        self.assertEqual(aggregate["p95_ms"], 100)
        self.assertEqual(aggregate["max_ms"], 100)
        self.assertEqual(aggregate["sample_count"], 5)
        self.assertEqual(aggregate["metric_type"], "backend_api_rtt")

    async def test_dedupe_latency_failure_and_system_rings_are_bounded(self):
        store = McpDiagnosticsStore(
            max_latency_samples_per_edge=2,
            max_failures=2,
            max_system_samples=2,
            subscriber_queue_size=2,
        )
        first = latency("latency-001", 10)
        result = await store.ingest(
            [first, first, latency("latency-002", 20), latency("latency-003", 30)]
        )
        self.assertEqual(result["accepted"], 3)
        self.assertEqual(result["duplicates"], 1)

        await store.ingest([failure("failure-001"), failure("failure-002"), failure("failure-003")])
        for index in range(3):
            await store.record_system_sample(
                McpBackendMetricsSample(
                    timestamp_ms=BASE_TS + index, cpu_count=8, cpu_usage_percent=index
                )
            )

        snapshot = await store.snapshot()
        aggregate = snapshot["latency"]["cptr-mcp-cptr-backend"]
        self.assertEqual(aggregate["sample_count"], 2)
        self.assertEqual(aggregate["latest_ms"], 30)
        self.assertEqual(
            [item["diagnostic_id"] for item in snapshot["failures"]], ["failure-002", "failure-003"]
        )
        self.assertEqual(len(snapshot["system"]), 2)

    async def test_failure_summary_is_redacted_before_storage(self):
        store = McpDiagnosticsStore(max_failures=2)
        secret_summary = (
            "Authorization: Bearer abcdefghijklmnop token=secret-value "
            "failed at /home/user/project/file.py and C:\\Users\\name\\secret.txt"
        )
        await store.ingest([failure("failure-001", summary=secret_summary)])
        snapshot = await store.snapshot()
        stored = snapshot["failures"][0]["summary"]
        self.assertNotIn("abcdefghijklmnop", stored)
        self.assertNotIn("secret-value", stored)
        self.assertNotIn("/home/", stored)
        self.assertNotIn("C:\\Users", stored)
        self.assertLessEqual(len(stored), 500)

    async def test_subscriber_overflow_drops_oldest_without_blocking(self):
        store = McpDiagnosticsStore(subscriber_queue_size=1)
        queue = store.subscribe()
        await asyncio.wait_for(store.ingest([latency("latency-001", 10)]), timeout=0.2)
        await asyncio.wait_for(store.ingest([latency("latency-002", 20)]), timeout=0.2)
        queued = queue.get_nowait()
        self.assertEqual(queued["event_id"], "latency-002")
        snapshot = await store.snapshot()
        self.assertGreaterEqual(snapshot["stream_health"]["slow_subscriber_drops"], 1)
        store.unsubscribe(queue)
        self.assertEqual((await store.snapshot())["stream_health"]["subscriber_count"], 0)


if __name__ == "__main__":
    unittest.main()
