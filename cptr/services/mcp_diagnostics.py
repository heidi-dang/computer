"""Bounded allowlisted MCP diagnostics and backend system telemetry state."""

from __future__ import annotations

import asyncio
import math
import os
from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cptr.utils.redaction import redact_external_text

LatencyEdge = Literal[
    "client-mcp-connector",
    "mcp-connector-cptr-mcp",
    "cptr-mcp-cptr-backend",
]
LatencyMetric = Literal[
    "observed_request_time",
    "adapter_handoff",
    "backend_api_rtt",
]
FailureStage = Literal[
    "client_transport",
    "mcp_connector",
    "cptr_mcp",
    "cptr_backend",
    "activity_delivery",
    "traffic_delivery",
]


class McpLatencySample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["latency"] = "latency"
    version: Literal[1] = 1
    event_id: str = Field(min_length=8, max_length=128)
    timestamp_ms: int = Field(ge=0)
    request_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    edge_id: LatencyEdge
    metric_type: LatencyMetric
    duration_ms: int = Field(ge=0, le=86_400_000)
    status: Literal["ok", "error"] = "ok"


class McpFailureDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["failure"] = "failure"
    version: Literal[1] = 1
    diagnostic_id: str = Field(min_length=8, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    client_id: str = Field(min_length=1, max_length=128)
    method: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=256)
    stage: FailureStage
    error_code: str = Field(min_length=1, max_length=64)
    http_status: int | None = Field(default=None, ge=100, le=599)
    retryable: bool | None = None
    started_at_ms: int | None = Field(default=None, ge=0)
    completed_at_ms: int = Field(ge=0)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    request_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    response_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    summary: str = Field(min_length=1, max_length=500)


class McpGpuMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, le=64)
    name: str = Field(min_length=1, max_length=120)
    utilization_percent: float = Field(ge=0, le=100)
    memory_used_bytes: int = Field(ge=0)
    memory_total_bytes: int = Field(ge=0)
    temperature_c: float | None = Field(default=None, ge=-50, le=150)


class McpProcessMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int = Field(ge=0)
    cpu_percent: float | None = Field(default=None, ge=0)
    memory_percent: float | None = Field(default=None, ge=0, le=100)
    name: str = Field(min_length=1, max_length=160)


class McpBackendMetricsSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["system"] = "system"
    version: Literal[1] = 1
    timestamp_ms: int = Field(ge=0)
    cpu_usage_percent: float | None = Field(default=None, ge=0, le=100)
    cpu_count: int = Field(ge=0, le=4096)
    load_avg: list[float] = Field(default_factory=list, max_length=3)
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_available_bytes: int | None = Field(default=None, ge=0)
    disk_total_bytes: int | None = Field(default=None, ge=0)
    disk_used_bytes: int | None = Field(default=None, ge=0)
    disk_free_bytes: int | None = Field(default=None, ge=0)
    disk_read_bytes_per_s: float | None = Field(default=None, ge=0)
    disk_write_bytes_per_s: float | None = Field(default=None, ge=0)
    disk_read_ops_per_s: float | None = Field(default=None, ge=0)
    disk_write_ops_per_s: float | None = Field(default=None, ge=0)
    network_rx_bytes_per_s: float | None = Field(default=None, ge=0)
    network_tx_bytes_per_s: float | None = Field(default=None, ge=0)
    uptime_seconds: int | None = Field(default=None, ge=0)
    gpu_status: Literal["available", "unavailable", "error"] = "unavailable"
    gpus: list[McpGpuMetrics] = Field(default_factory=list, max_length=16)
    cptr_process: McpProcessMetrics | None = None
    processes: list[McpProcessMetrics] = Field(default_factory=list, max_length=10)


class McpDiagnosticsBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[McpLatencySample | McpFailureDiagnostic] = Field(min_length=1, max_length=100)


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _nearest_rank(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    if not ordered:
        return 0
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[min(len(ordered), rank) - 1]


class McpDiagnosticsStore:
    """Bounded in-memory diagnostics with non-blocking subscriber fan-out."""

    def __init__(
        self,
        *,
        max_latency_samples_per_edge: int = 120,
        max_failures: int = 250,
        max_system_samples: int = 60,
        subscriber_queue_size: int = 64,
        observed_degraded_ms: int | None = None,
        handoff_degraded_ms: int | None = None,
        backend_rtt_degraded_ms: int | None = None,
    ) -> None:
        self.max_latency_samples_per_edge = max(1, int(max_latency_samples_per_edge))
        self.max_failures = max(1, int(max_failures))
        self.max_system_samples = max(1, int(max_system_samples))
        self.subscriber_queue_size = max(1, int(subscriber_queue_size))
        self._thresholds: dict[str, int] = {
            "observed_request_time": observed_degraded_ms
            or _bounded_env_int("CPTR_MCP_DIAGNOSTICS_OBSERVED_DEGRADED_MS", 5000, 1, 86_400_000),
            "adapter_handoff": handoff_degraded_ms
            or _bounded_env_int("CPTR_MCP_DIAGNOSTICS_HANDOFF_DEGRADED_MS", 100, 1, 86_400_000),
            "backend_api_rtt": backend_rtt_degraded_ms
            or _bounded_env_int(
                "CPTR_MCP_DIAGNOSTICS_BACKEND_RTT_DEGRADED_MS", 1000, 1, 86_400_000
            ),
        }
        self._latency: dict[str, deque[dict[str, object]]] = {}
        self._failures: deque[dict[str, object]] = deque(maxlen=self.max_failures)
        self._system: deque[dict[str, object]] = deque(maxlen=self.max_system_samples)
        self._seen_ids: deque[str] = deque()
        self._seen_id_set: set[str] = set()
        self._dedupe_capacity = max(
            64,
            (self.max_latency_samples_per_edge * 3 + self.max_failures) * 4,
        )
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._slow_subscriber_drops = 0

    def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=self.subscriber_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        self._subscribers.discard(queue)

    async def ingest(self, events: list[McpLatencySample | McpFailureDiagnostic]) -> dict[str, int]:
        accepted = 0
        duplicates = 0
        dropped = 0
        async with self._lock:
            for event in events:
                event_id = (
                    event.event_id if isinstance(event, McpLatencySample) else event.diagnostic_id
                )
                if event_id in self._seen_id_set:
                    duplicates += 1
                    continue
                self._remember_id(event_id)
                self._sequence += 1

                if isinstance(event, McpLatencySample):
                    projected = event.model_dump(mode="json")
                    bucket = self._latency.setdefault(
                        event.edge_id,
                        deque(maxlen=self.max_latency_samples_per_edge),
                    )
                    bucket.append(projected)
                else:
                    safe_summary = redact_external_text(event.summary).strip()[:500]
                    if not safe_summary:
                        safe_summary = "MCP request failed."
                    projected = event.model_copy(update={"summary": safe_summary}).model_dump(
                        mode="json"
                    )
                    self._failures.append(projected)

                projected = {**projected, "ingestion_sequence": self._sequence}
                self._fan_out(projected)
                accepted += 1
        return {"accepted": accepted, "duplicates": duplicates, "dropped": dropped}

    async def record_system_sample(self, sample: McpBackendMetricsSample) -> None:
        async with self._lock:
            self._sequence += 1
            projected = sample.model_dump(mode="json")
            self._system.append(projected)
            self._fan_out({**projected, "ingestion_sequence": self._sequence})

    async def snapshot(self) -> dict[str, object]:
        async with self._lock:
            latency = {
                edge_id: self._latency_aggregate(samples)
                for edge_id, samples in sorted(self._latency.items())
                if samples
            }
            return {
                "version": 1,
                "sequence": self._sequence,
                "latency": latency,
                "failures": list(self._failures),
                "system": list(self._system),
                "stream_health": {
                    "subscriber_count": len(self._subscribers),
                    "slow_subscriber_drops": self._slow_subscriber_drops,
                    "latency_sample_capacity_per_edge": self.max_latency_samples_per_edge,
                    "failure_capacity": self.max_failures,
                    "system_sample_capacity": self.max_system_samples,
                    "subscriber_queue_capacity": self.subscriber_queue_size,
                },
            }

    def _latency_aggregate(self, samples: deque[dict[str, object]]) -> dict[str, object]:
        values = [int(sample["duration_ms"]) for sample in samples]
        latest = samples[-1]
        p95 = _nearest_rank(values, 0.95)
        metric_type = str(latest["metric_type"])
        latest_status = str(latest["status"])
        health = (
            "error"
            if latest_status == "error"
            else ("degraded" if p95 >= self._thresholds.get(metric_type, 86_400_000) else "healthy")
        )
        return {
            "metric_type": metric_type,
            "latest_ms": values[-1],
            "average_ms": sum(values) / len(values),
            "p50_ms": _nearest_rank(values, 0.50),
            "p95_ms": p95,
            "max_ms": max(values),
            "sample_count": len(values),
            "last_updated_ms": int(latest["timestamp_ms"]),
            "latest_status": latest_status,
            "health": health,
        }

    def _remember_id(self, event_id: str) -> None:
        self._seen_ids.append(event_id)
        self._seen_id_set.add(event_id)
        while len(self._seen_ids) > self._dedupe_capacity:
            expired = self._seen_ids.popleft()
            self._seen_id_set.discard(expired)

    def _fan_out(self, projected: dict[str, object]) -> None:
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(projected)
                continue
            except asyncio.QueueFull:
                pass
            try:
                queue.get_nowait()
                self._slow_subscriber_drops += 1
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(projected)
            except asyncio.QueueFull:
                self._slow_subscriber_drops += 1


mcp_diagnostics_store = McpDiagnosticsStore(
    max_latency_samples_per_edge=_bounded_env_int(
        "CPTR_MCP_DIAGNOSTICS_LATENCY_SAMPLES_PER_EDGE", 120, 10, 2000
    ),
    max_failures=_bounded_env_int("CPTR_MCP_DIAGNOSTICS_MAX_FAILURES", 250, 10, 5000),
    max_system_samples=_bounded_env_int("CPTR_MCP_DIAGNOSTICS_MAX_SYSTEM_SAMPLES", 60, 10, 600),
    subscriber_queue_size=_bounded_env_int("CPTR_MCP_DIAGNOSTICS_SUBSCRIBER_QUEUE", 64, 8, 512),
)
