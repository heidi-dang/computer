"""Low-overhead in-process runtime metrics for CPTR health and regression visibility."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from cptr.env import EVENT_LOOP_LAG_SAMPLE_INTERVAL_MS, METRICS_SAMPLE_WINDOW


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _process_snapshot() -> dict[str, int | None]:
    rss_bytes: int | None = None
    open_fds: int | None = None
    try:
        if os.name == "posix" and os.path.exists("/proc/self/statm"):
            with open("/proc/self/statm", encoding="utf-8") as source:
                fields = source.read().split()
            if len(fields) > 1:
                rss_bytes = int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
        elif os.name == "posix":
            import resource

            rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            rss_bytes = rss if sys.platform == "darwin" else rss * 1024
    except Exception:
        rss_bytes = None
    try:
        fd_path = "/proc/self/fd"
        if os.path.isdir(fd_path):
            open_fds = len(os.listdir(fd_path))
    except Exception:
        open_fds = None
    return {"rss_bytes": rss_bytes, "open_fds": open_fds}


@dataclass
class RuntimeMetrics:
    request_latencies_ms: deque[float] = field(
        default_factory=lambda: deque(maxlen=METRICS_SAMPLE_WINDOW)
    )
    db_latencies_ms: deque[float] = field(
        default_factory=lambda: deque(maxlen=METRICS_SAMPLE_WINDOW)
    )
    request_count: int = 0
    request_error_count: int = 0
    db_query_count: int = 0
    db_error_count: int = 0
    db_busy_count: int = 0
    max_event_loop_lag_ms: float = 0.0
    last_event_loop_lag_ms: float = 0.0
    started_at: float = field(default_factory=time.time)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def observe_request(self, duration_ms: float, *, status_code: int) -> None:
        with self._lock:
            self.request_count += 1
            if status_code >= 500:
                self.request_error_count += 1
            self.request_latencies_ms.append(max(0.0, duration_ms))

    def observe_db_query(
        self, duration_ms: float, *, failed: bool = False, busy: bool = False
    ) -> None:
        with self._lock:
            self.db_query_count += 1
            if failed:
                self.db_error_count += 1
            if busy:
                self.db_busy_count += 1
            self.db_latencies_ms.append(max(0.0, duration_ms))

    def observe_event_loop_lag(self, lag_ms: float) -> None:
        with self._lock:
            self.last_event_loop_lag_ms = max(0.0, lag_ms)
            self.max_event_loop_lag_ms = max(
                self.max_event_loop_lag_ms, self.last_event_loop_lag_ms
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            requests = list(self.request_latencies_ms)
            db = list(self.db_latencies_ms)
            payload = {
                "uptime_seconds": int(time.time() - self.started_at),
                "requests": {
                    "count": self.request_count,
                    "server_error_count": self.request_error_count,
                    "latency_ms": {
                        "p50": round(_percentile(requests, 0.50), 3),
                        "p95": round(_percentile(requests, 0.95), 3),
                        "p99": round(_percentile(requests, 0.99), 3),
                        "samples": len(requests),
                    },
                },
                "database": {
                    "query_count": self.db_query_count,
                    "error_count": self.db_error_count,
                    "busy_count": self.db_busy_count,
                    "latency_ms": {
                        "p50": round(_percentile(db, 0.50), 3),
                        "p95": round(_percentile(db, 0.95), 3),
                        "p99": round(_percentile(db, 0.99), 3),
                        "samples": len(db),
                    },
                },
                "event_loop": {
                    "last_lag_ms": round(self.last_event_loop_lag_ms, 3),
                    "max_lag_ms": round(self.max_event_loop_lag_ms, 3),
                },
            }
        payload["process"] = _process_snapshot()
        return payload


runtime_metrics = RuntimeMetrics()


async def event_loop_lag_worker() -> None:
    """Measure scheduler delay without blocking or generating external I/O."""
    interval = EVENT_LOOP_LAG_SAMPLE_INTERVAL_MS / 1000.0
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval
    while True:
        await asyncio.sleep(interval)
        now = loop.time()
        runtime_metrics.observe_event_loop_lag(max(0.0, now - expected) * 1000.0)
        expected = now + interval
