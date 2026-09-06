"""Compact, performance-bounded observability projection for the MCP Services UI.

This module deliberately composes existing in-memory/cached telemetry. It does
not create another persistence layer, background worker, browser probe, or MCP
network request. The resulting envelope is small enough for a low-frequency SSE
delta and contains operational aggregates only.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any


def _number(value: Any, default: int | float = 0) -> int | float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _percent(numerator: Any, denominator: Any) -> float:
    top = float(_number(numerator, 0))
    bottom = float(_number(denominator, 0))
    if bottom <= 0:
        return 0.0
    return round(min(100.0, max(0.0, top / bottom * 100.0)), 2)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class McpServicesObservability:
    """Build one read-only observability envelope from bounded subsystem summaries."""

    def __init__(
        self,
        *,
        runtime_snapshot_fn: Callable[[], Any] | None = None,
        command_metrics_fn: Callable[[], Any] | None = None,
        live_event_stats_fn: Callable[[], Any] | None = None,
        diagnostics_summary_fn: Callable[[], Any] | None = None,
        traffic_summary_fn: Callable[[], Any] | None = None,
        worker_snapshot_fn: Callable[[], Any] | None = None,
    ) -> None:
        self.runtime_snapshot_fn = runtime_snapshot_fn
        self.command_metrics_fn = command_metrics_fn
        self.live_event_stats_fn = live_event_stats_fn
        self.diagnostics_summary_fn = diagnostics_summary_fn
        self.traffic_summary_fn = traffic_summary_fn
        self.worker_snapshot_fn = worker_snapshot_fn

    async def snapshot(self) -> dict[str, Any]:
        runtime = await self._runtime()
        commands = await self._commands()
        live_events = await self._live_events()
        diagnostics = await self._diagnostics()
        traffic = await self._traffic()
        watchdog = await self._workers()

        request_latency = dict((runtime.get("requests") or {}).get("latency_ms") or {})
        db_latency = dict((runtime.get("database") or {}).get("latency_ms") or {})
        requests = dict(runtime.get("requests") or {})
        database = dict(runtime.get("database") or {})
        worker_map = dict(watchdog.get("workers") or {})
        healthy_workers = sum(
            1
            for worker in worker_map.values()
            if isinstance(worker, dict) and str(worker.get("status") or "") in {"healthy", "restarted"}
        )
        worker_restarts = sum(
            int(_number(worker.get("restart_count_window"), 0))
            for worker in worker_map.values()
            if isinstance(worker, dict)
        )

        latency = dict(diagnostics.get("latency") or {})
        backend_rtt = dict(latency.get("cptr-mcp-cptr-backend") or {})
        diag_stream = dict(diagnostics.get("stream_health") or {})
        traffic_stream = dict(traffic.get("stream_health") or {})
        live_queue_depth = _number(live_events.get("queue_depth"), 0)
        live_queue_capacity = _number(live_events.get("queue_capacity"), 0)

        return {
            "version": 1,
            "generated_at_ms": int(time.time() * 1000),
            "runtime": {
                "uptime_seconds": int(_number(runtime.get("uptime_seconds"), 0)),
                "requests": {
                    "count": int(_number(requests.get("count"), 0)),
                    "server_error_count": int(_number(requests.get("server_error_count"), 0)),
                    "p95_ms": float(_number(request_latency.get("p95"), 0.0)),
                    "samples": int(_number(request_latency.get("samples"), 0)),
                },
                "database": {
                    "query_count": int(_number(database.get("query_count"), 0)),
                    "error_count": int(_number(database.get("error_count"), 0)),
                    "busy_count": int(_number(database.get("busy_count"), 0)),
                    "p95_ms": float(_number(db_latency.get("p95"), 0.0)),
                    "samples": int(_number(db_latency.get("samples"), 0)),
                },
                "event_loop": dict(runtime.get("event_loop") or {}),
                "process": dict(runtime.get("process") or {}),
            },
            "execution": {
                "commands": commands,
                "live_events": live_events,
            },
            "workers": {
                "aggregate": watchdog.get("aggregate") or "unknown",
                "total": int(_number(watchdog.get("worker_count"), len(worker_map))),
                "healthy": healthy_workers,
                "degraded": max(0, len(worker_map) - healthy_workers),
                "restarts": worker_restarts,
            },
            "mcp": {
                "client_count": int(_number(traffic.get("client_count"), 0)),
                "session_count": int(_number(traffic.get("session_count"), 0)),
                "active_requests": int(_number(traffic.get("active_requests"), 0)),
                "total_requests": int(_number(traffic.get("total_requests"), 0)),
                "errors": int(_number(traffic.get("errors"), 0)),
                "failure_count": int(_number(diagnostics.get("failure_count"), 0)),
                "backend_rtt_p95_ms": float(_number(backend_rtt.get("health_p95_ms"), 0.0)),
                "backend_rtt_samples": int(_number(backend_rtt.get("health_sample_count"), 0)),
                "backend_rtt_health": backend_rtt.get("health") or "unknown",
            },
            "host": diagnostics.get("latest_system"),
            "pressure": {
                "live_event_queue_percent": _percent(live_queue_depth, live_queue_capacity),
                "live_event_queue_depth": int(live_queue_depth),
                "live_event_queue_capacity": int(live_queue_capacity),
                "live_event_subscribers": int(_number(live_events.get("subscriber_count"), 0)),
                "live_event_slow_disconnects": int(
                    _number(live_events.get("slow_subscriber_disconnects"), 0)
                ),
                "mcp_diagnostics_slow_drops": int(
                    _number(diag_stream.get("slow_subscriber_drops"), 0)
                ),
                "mcp_traffic_slow_drops": int(
                    _number(traffic_stream.get("slow_subscriber_drops"), 0)
                ),
            },
        }

    async def _runtime(self) -> dict[str, Any]:
        if self.runtime_snapshot_fn is not None:
            return dict(await _resolve(self.runtime_snapshot_fn()) or {})
        from cptr.services.runtime_metrics import runtime_metrics

        return dict(runtime_metrics.snapshot())

    async def _commands(self) -> dict[str, Any]:
        if self.command_metrics_fn is not None:
            return dict(await _resolve(self.command_metrics_fn()) or {})
        from cptr.utils.tools import command_session_passive_metrics

        return dict(command_session_passive_metrics())

    async def _live_events(self) -> dict[str, Any]:
        if self.live_event_stats_fn is not None:
            return dict(await _resolve(self.live_event_stats_fn()) or {})
        from cptr.services.live_events import live_event_hub

        return dict(live_event_hub.stats())

    async def _diagnostics(self) -> dict[str, Any]:
        if self.diagnostics_summary_fn is not None:
            return dict(await _resolve(self.diagnostics_summary_fn()) or {})
        from cptr.services.mcp_diagnostics import mcp_diagnostics_store

        return dict(await mcp_diagnostics_store.summary())

    async def _traffic(self) -> dict[str, Any]:
        if self.traffic_summary_fn is not None:
            return dict(await _resolve(self.traffic_summary_fn()) or {})
        from cptr.services.mcp_traffic import mcp_traffic_store

        return dict(await mcp_traffic_store.summary())

    async def _workers(self) -> dict[str, Any]:
        if self.worker_snapshot_fn is not None:
            return dict(await _resolve(self.worker_snapshot_fn()) or {})
        from cptr.services.worker_watchdog import worker_watchdog

        return dict(await worker_watchdog.snapshot())


mcp_services_observability = McpServicesObservability()


__all__ = ["McpServicesObservability", "mcp_services_observability"]
