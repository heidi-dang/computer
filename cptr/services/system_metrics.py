"""Best-effort bounded host telemetry for MCP backend diagnostics.

The collector intentionally uses only local OS facilities. Potentially blocking
filesystem, process-table, and NVIDIA probes are run off the event loop by
``BackendMetricsSampler`` so diagnostics can never block MCP request handling.
"""

from __future__ import annotations

import asyncio
import csv
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from cptr.services.mcp_diagnostics import (
    McpBackendMetricsSample,
    McpGpuMetrics,
    McpProcessMetrics,
    _bounded_env_int,
    mcp_diagnostics_store,
)

_GPU_STATUS = Literal["available", "unavailable", "error"]
_DISK_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class BackendCounterSnapshot:
    timestamp_ms: int
    cpu_total: int | None
    cpu_idle: int | None
    cpu_count: int
    memory_total: int | None
    memory_available: int | None
    disk_total: int | None
    disk_used: int | None
    disk_free: int | None
    disk_read_bytes: int | None
    disk_write_bytes: int | None
    disk_read_ops: int | None
    disk_write_ops: int | None
    network_rx_bytes: int | None
    network_tx_bytes: int | None
    uptime_seconds: int | None
    load_avg: list[float]
    cptr_process_cpu_ticks: int | None
    cptr_process_rss_bytes: int | None
    clock_ticks_per_second: int | None
    cptr_process_name: str
    processes: list[McpProcessMetrics]
    gpus: list[McpGpuMetrics]
    gpu_status: _GPU_STATUS


class _SystemSampleStore(Protocol):
    async def record_system_sample(self, sample: McpBackendMetricsSample) -> None: ...


def _safe_float(value: str) -> float | None:
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed


def _safe_int(value: str) -> int | None:
    try:
        return int(float(value.strip()))
    except (TypeError, ValueError):
        return None


def _read_proc_cpu() -> tuple[int | None, int | None]:
    try:
        first = Path("/proc/stat").read_text(encoding="utf-8", errors="replace").splitlines()[0]
        fields = first.split()
        if not fields or fields[0] != "cpu":
            return None, None
        values = [int(value) for value in fields[1:]]
        if len(values) < 4:
            return None, None
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle
    except (OSError, ValueError, IndexError):
        return None, None


def _read_proc_memory() -> tuple[int | None, int | None]:
    try:
        values: dict[str, int] = {}
        for line in (
            Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines()
        ):
            key, sep, remainder = line.partition(":")
            if not sep:
                continue
            raw = remainder.strip().split()[0]
            values[key] = int(raw) * 1024
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if available is None:
            available = sum(
                values.get(name, 0) for name in ("MemFree", "Buffers", "Cached", "SReclaimable")
            )
        return total, available
    except (OSError, ValueError, IndexError):
        return None, None


def _is_physical_block_device(name: str) -> bool:
    if not _DISK_NAME_RE.fullmatch(name):
        return False
    if name.startswith(("loop", "ram", "fd", "sr", "zram", "dm-")):
        return False
    sys_block = Path("/sys/block")
    if sys_block.exists():
        return (sys_block / name).exists()
    # Conservative fallback when /sys is unavailable: reject obvious partitions.
    if re.search(r"(?:p\d+|[A-Za-z]\d+)$", name):
        return False
    return True


def _read_proc_diskstats() -> tuple[int | None, int | None, int | None, int | None]:
    read_bytes = write_bytes = read_ops = write_ops = 0
    matched = False
    try:
        lines = Path("/proc/diskstats").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None, None, None

    for line in lines:
        fields = line.split()
        if len(fields) < 14:
            continue
        name = fields[2]
        if not _is_physical_block_device(name):
            continue
        try:
            reads_completed = int(fields[3])
            sectors_read = int(fields[5])
            writes_completed = int(fields[7])
            sectors_written = int(fields[9])
        except ValueError:
            continue
        matched = True
        read_ops += max(0, reads_completed)
        write_ops += max(0, writes_completed)
        read_bytes += max(0, sectors_read) * 512
        write_bytes += max(0, sectors_written) * 512

    if not matched:
        return None, None, None, None
    return read_bytes, write_bytes, read_ops, write_ops


def _read_proc_network() -> tuple[int | None, int | None]:
    rx = tx = 0
    matched = False
    try:
        lines = Path("/proc/net/dev").read_text(encoding="utf-8", errors="replace").splitlines()[2:]
    except OSError:
        return None, None

    for line in lines:
        iface, sep, remainder = line.partition(":")
        if not sep or iface.strip() == "lo":
            continue
        fields = remainder.split()
        if len(fields) < 9:
            continue
        try:
            rx += max(0, int(fields[0]))
            tx += max(0, int(fields[8]))
        except ValueError:
            continue
        matched = True
    return (rx, tx) if matched else (None, None)


def _read_uptime() -> int | None:
    try:
        raw = Path("/proc/uptime").read_text(encoding="utf-8", errors="replace").split()[0]
        return max(0, int(float(raw)))
    except (OSError, ValueError, IndexError):
        return None


def _read_process_counters() -> tuple[int | None, int | None, int | None, str]:
    name = "cptr"
    try:
        raw_name = Path("/proc/self/comm").read_text(encoding="utf-8", errors="replace").strip()
        if raw_name:
            name = raw_name[:160]
    except OSError:
        pass

    ticks: int | None = None
    rss_bytes: int | None = None
    clock_ticks: int | None = None
    try:
        raw = Path("/proc/self/stat").read_text(encoding="utf-8", errors="replace").strip()
        close = raw.rfind(")")
        rest = raw[close + 2 :].split() if close >= 0 else []
        if len(rest) > 12:
            ticks = max(0, int(rest[11]) + int(rest[12]))
    except (OSError, ValueError):
        ticks = None

    try:
        pages = int(
            Path("/proc/self/statm").read_text(encoding="utf-8", errors="replace").split()[1]
        )
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        rss_bytes = max(0, pages * page_size)
    except (OSError, ValueError, IndexError):
        rss_bytes = None

    try:
        clock_ticks = max(1, int(os.sysconf("SC_CLK_TCK")))
    except (OSError, ValueError):
        clock_ticks = None
    return ticks, rss_bytes, clock_ticks, name


def _collect_processes() -> list[McpProcessMetrics]:
    ps = shutil.which("ps")
    if not ps:
        return []
    try:
        completed = subprocess.run(
            [ps, "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu", "--no-headers"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []

    result: list[McpProcessMetrics] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) != 4:
            continue
        pid = _safe_int(fields[0])
        cpu = _safe_float(fields[1])
        memory = _safe_float(fields[2])
        if pid is None or pid < 0:
            continue
        cpu = None if cpu is None else max(0.0, cpu)
        memory = None if memory is None else min(100.0, max(0.0, memory))
        name = fields[3].strip()[:160] or "process"
        try:
            result.append(
                McpProcessMetrics(pid=pid, cpu_percent=cpu, memory_percent=memory, name=name)
            )
        except ValueError:
            continue
        if len(result) >= 10:
            break
    return result


def _collect_gpu_metrics() -> tuple[_GPU_STATUS, list[McpGpuMetrics]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return "unavailable", []
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "error", []
    if completed.returncode != 0:
        return "error", []

    gpus: list[McpGpuMetrics] = []
    try:
        rows = csv.reader(completed.stdout.splitlines(), skipinitialspace=True)
        for row in rows:
            if len(row) < 6:
                continue
            index = _safe_int(row[0])
            utilization = _safe_float(row[2])
            memory_used_mib = _safe_float(row[3])
            memory_total_mib = _safe_float(row[4])
            temperature = _safe_float(row[5])
            if None in (index, utilization, memory_used_mib, memory_total_mib):
                continue
            if index is None or index < 0 or index > 64:
                continue
            name = row[1].strip()[:120] or f"GPU {index}"
            try:
                gpus.append(
                    McpGpuMetrics(
                        index=index,
                        name=name,
                        utilization_percent=min(100.0, max(0.0, float(utilization))),
                        memory_used_bytes=max(0, int(float(memory_used_mib) * 1024 * 1024)),
                        memory_total_bytes=max(0, int(float(memory_total_mib) * 1024 * 1024)),
                        temperature_c=(
                            None
                            if temperature is None
                            else min(150.0, max(-50.0, float(temperature)))
                        ),
                    )
                )
            except ValueError:
                continue
            if len(gpus) >= 16:
                break
    except (csv.Error, TypeError, ValueError):
        return "error", []
    return ("available", gpus) if gpus else ("unavailable", [])


def _disk_capacity() -> tuple[int | None, int | None, int | None]:
    try:
        usage = shutil.disk_usage(Path.home())
        return int(usage.total), int(usage.used), int(usage.free)
    except OSError:
        return None, None, None


def collect_backend_counters() -> BackendCounterSnapshot:
    """Collect one best-effort synchronous host counter snapshot."""
    timestamp_ms = int(time.time() * 1000)
    cpu_total, cpu_idle = _read_proc_cpu()
    memory_total, memory_available = _read_proc_memory()
    disk_total, disk_used, disk_free = _disk_capacity()
    disk_read, disk_write, disk_read_ops, disk_write_ops = _read_proc_diskstats()
    network_rx, network_tx = _read_proc_network()
    process_ticks, process_rss, clock_ticks, process_name = _read_process_counters()
    gpu_status, gpus = _collect_gpu_metrics()
    try:
        load_avg = [max(0.0, float(value)) for value in os.getloadavg()[:3]]
    except (AttributeError, OSError):
        load_avg = []

    return BackendCounterSnapshot(
        timestamp_ms=timestamp_ms,
        cpu_total=cpu_total,
        cpu_idle=cpu_idle,
        cpu_count=max(0, int(os.cpu_count() or 0)),
        memory_total=memory_total,
        memory_available=memory_available,
        disk_total=disk_total,
        disk_used=disk_used,
        disk_free=disk_free,
        disk_read_bytes=disk_read,
        disk_write_bytes=disk_write,
        disk_read_ops=disk_read_ops,
        disk_write_ops=disk_write_ops,
        network_rx_bytes=network_rx,
        network_tx_bytes=network_tx,
        uptime_seconds=_read_uptime(),
        load_avg=load_avg,
        cptr_process_cpu_ticks=process_ticks,
        cptr_process_rss_bytes=process_rss,
        clock_ticks_per_second=clock_ticks,
        cptr_process_name=process_name,
        processes=_collect_processes(),
        gpus=gpus,
        gpu_status=gpu_status,
    )


def _rate(previous: int | None, current: int | None, elapsed_seconds: float) -> float | None:
    if previous is None or current is None or elapsed_seconds <= 0:
        return None
    delta = current - previous
    if delta < 0:
        return None
    return delta / elapsed_seconds


def derive_backend_metrics(
    previous: BackendCounterSnapshot | None,
    current: BackendCounterSnapshot,
) -> McpBackendMetricsSample:
    """Convert monotonic counters into one bounded current/rate sample."""
    elapsed_seconds = 0.0
    if previous is not None:
        elapsed_seconds = (current.timestamp_ms - previous.timestamp_ms) / 1000.0

    cpu_usage: float | None = None
    if (
        previous is not None
        and previous.cpu_total is not None
        and current.cpu_total is not None
        and previous.cpu_idle is not None
        and current.cpu_idle is not None
    ):
        total_delta = current.cpu_total - previous.cpu_total
        idle_delta = current.cpu_idle - previous.cpu_idle
        if total_delta > 0 and idle_delta >= 0:
            cpu_usage = min(100.0, max(0.0, ((total_delta - idle_delta) / total_delta) * 100.0))
            cpu_usage = round(cpu_usage, 3)

    cptr_process: McpProcessMetrics | None = None
    process_cpu: float | None = None
    if (
        previous is not None
        and elapsed_seconds > 0
        and previous.cptr_process_cpu_ticks is not None
        and current.cptr_process_cpu_ticks is not None
        and current.clock_ticks_per_second
    ):
        tick_delta = current.cptr_process_cpu_ticks - previous.cptr_process_cpu_ticks
        if tick_delta >= 0:
            process_cpu = max(
                0.0,
                (tick_delta / current.clock_ticks_per_second) / elapsed_seconds * 100.0,
            )
            process_cpu = round(process_cpu, 3)

    process_memory: float | None = None
    if (
        current.cptr_process_rss_bytes is not None
        and current.memory_total
        and current.memory_total > 0
    ):
        process_memory = min(
            100.0,
            max(0.0, current.cptr_process_rss_bytes / current.memory_total * 100.0),
        )
        process_memory = round(process_memory, 3)
    if process_cpu is not None or process_memory is not None:
        cptr_process = McpProcessMetrics(
            pid=os.getpid(),
            cpu_percent=process_cpu,
            memory_percent=process_memory,
            name=(current.cptr_process_name or "cptr")[:160],
        )

    return McpBackendMetricsSample(
        timestamp_ms=current.timestamp_ms,
        cpu_usage_percent=cpu_usage,
        cpu_count=current.cpu_count,
        load_avg=current.load_avg[:3],
        memory_total_bytes=current.memory_total,
        memory_available_bytes=current.memory_available,
        disk_total_bytes=current.disk_total,
        disk_used_bytes=current.disk_used,
        disk_free_bytes=current.disk_free,
        disk_read_bytes_per_s=_rate(
            previous.disk_read_bytes if previous else None,
            current.disk_read_bytes,
            elapsed_seconds,
        ),
        disk_write_bytes_per_s=_rate(
            previous.disk_write_bytes if previous else None,
            current.disk_write_bytes,
            elapsed_seconds,
        ),
        disk_read_ops_per_s=_rate(
            previous.disk_read_ops if previous else None,
            current.disk_read_ops,
            elapsed_seconds,
        ),
        disk_write_ops_per_s=_rate(
            previous.disk_write_ops if previous else None,
            current.disk_write_ops,
            elapsed_seconds,
        ),
        network_rx_bytes_per_s=_rate(
            previous.network_rx_bytes if previous else None,
            current.network_rx_bytes,
            elapsed_seconds,
        ),
        network_tx_bytes_per_s=_rate(
            previous.network_tx_bytes if previous else None,
            current.network_tx_bytes,
            elapsed_seconds,
        ),
        uptime_seconds=current.uptime_seconds,
        gpu_status=current.gpu_status,
        gpus=current.gpus[:16],
        cptr_process=cptr_process,
        processes=current.processes[:10],
    )


def _fallback_counter_snapshot() -> BackendCounterSnapshot:
    disk_total, disk_used, disk_free = _disk_capacity()
    return BackendCounterSnapshot(
        timestamp_ms=int(time.time() * 1000),
        cpu_total=None,
        cpu_idle=None,
        cpu_count=max(0, int(os.cpu_count() or 0)),
        memory_total=None,
        memory_available=None,
        disk_total=disk_total,
        disk_used=disk_used,
        disk_free=disk_free,
        disk_read_bytes=None,
        disk_write_bytes=None,
        disk_read_ops=None,
        disk_write_ops=None,
        network_rx_bytes=None,
        network_tx_bytes=None,
        uptime_seconds=None,
        load_avg=[],
        cptr_process_cpu_ticks=None,
        cptr_process_rss_bytes=None,
        clock_ticks_per_second=None,
        cptr_process_name="cptr",
        processes=[],
        gpus=[],
        gpu_status="error",
    )


class BackendMetricsSampler:
    """Idempotent asynchronous sampler that keeps all blocking probes off-loop."""

    def __init__(self, store: _SystemSampleStore, interval_seconds: float = 1.0) -> None:
        self.store = store
        self.interval_seconds = min(10.0, max(0.5, float(interval_seconds)))
        self._previous: BackendCounterSnapshot | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._start_lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._task is not None and not self._task.done():
                return
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name="mcp-backend-metrics")

    async def sample_once(self) -> McpBackendMetricsSample:
        try:
            counters = await asyncio.to_thread(collect_backend_counters)
        except Exception:
            counters = _fallback_counter_snapshot()
        sample = derive_backend_metrics(self._previous, counters)
        self._previous = counters
        await self.store.record_system_sample(sample)
        return sample

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.sample_once()
            except Exception:
                # Diagnostics are explicitly secondary to the MCP request path.
                pass
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                continue

    async def close(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=max(1.0, self.interval_seconds + 0.5))
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


mcp_metrics_sampler = BackendMetricsSampler(
    mcp_diagnostics_store,
    interval_seconds=(
        _bounded_env_int("CPTR_MCP_SYSTEM_METRICS_INTERVAL_MS", 1000, 500, 10_000) / 1000
    ),
)


__all__ = [
    "BackendCounterSnapshot",
    "BackendMetricsSampler",
    "collect_backend_counters",
    "derive_backend_metrics",
    "mcp_metrics_sampler",
]
