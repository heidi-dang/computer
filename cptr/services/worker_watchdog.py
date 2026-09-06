"""Deterministic supervision for CPTR's permanent in-process background workers.

The watchdog never invokes a model. It owns explicit asyncio tasks, records
heartbeats, restarts workers that have exited, and opens a restart circuit after
a bounded number of attempts. A live task with a stale heartbeat is reported as
stalled but is not force-cancelled: maintenance must never destroy user work just
to make a health indicator green.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

WorkerFactory = Callable[[], Awaitable[None]]


@dataclass
class WorkerSpec:
    name: str
    task_factory: WorkerFactory
    stale_after_seconds: float
    success_stale_after_seconds: float | None
    restart_limit: int
    restart_window_seconds: float
    critical: bool = True


@dataclass
class WorkerRuntime:
    task: asyncio.Task[None] | None = None
    started_at: float | None = None
    last_heartbeat_at: float | None = None
    last_success_at: float | None = None
    last_error: str | None = None
    restart_times: deque[float] = field(default_factory=deque)


class WorkerWatchdog:
    def __init__(
        self,
        *,
        check_interval_seconds: float = 15.0,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.check_interval_seconds = max(0.1, float(check_interval_seconds))
        self._monotonic = monotonic_fn
        self._specs: dict[str, WorkerSpec] = {}
        self._runtime: dict[str, WorkerRuntime] = {}
        self._supervisor_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def register(
        self,
        *,
        name: str,
        task_factory: WorkerFactory,
        stale_after_seconds: float,
        success_stale_after_seconds: float | None = None,
        restart_limit: int = 3,
        restart_window_seconds: float = 300.0,
        critical: bool = True,
    ) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("worker name must not be blank")
        if normalized in self._specs:
            raise ValueError(f"worker already registered: {normalized}")
        if restart_limit < 1:
            raise ValueError("restart_limit must be positive")
        self._specs[normalized] = WorkerSpec(
            name=normalized,
            task_factory=task_factory,
            stale_after_seconds=max(0.1, float(stale_after_seconds)),
            success_stale_after_seconds=(
                max(0.1, float(success_stale_after_seconds))
                if success_stale_after_seconds is not None
                else None
            ),
            restart_limit=int(restart_limit),
            restart_window_seconds=max(1.0, float(restart_window_seconds)),
            critical=bool(critical),
        )
        self._runtime[normalized] = WorkerRuntime()

    def heartbeat(self, name: str, *, now: float | None = None, success: bool = False) -> None:
        runtime = self._runtime.get(name)
        if runtime is None:
            return
        current = self._monotonic() if now is None else float(now)
        runtime.last_heartbeat_at = current
        if success:
            runtime.last_success_at = current
            runtime.last_error = None

    def _trim_restart_window(self, spec: WorkerSpec, runtime: WorkerRuntime, now: float) -> None:
        cutoff = now - spec.restart_window_seconds
        while runtime.restart_times and runtime.restart_times[0] < cutoff:
            runtime.restart_times.popleft()

    def _capture_terminal_error(self, runtime: WorkerRuntime) -> None:
        task = runtime.task
        if task is None or not task.done() or task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            runtime.last_error = f"{error.__class__.__name__}: {error}"[:1000]
        elif runtime.last_error is None:
            runtime.last_error = "worker exited unexpectedly"

    def _start_worker(self, spec: WorkerSpec, runtime: WorkerRuntime, now: float) -> None:
        task = asyncio.create_task(spec.task_factory(), name=f"cptr-worker-{spec.name}")
        runtime.task = task
        runtime.started_at = now
        runtime.last_heartbeat_at = now
        runtime.last_success_at = None
        runtime.restart_times.append(now)

    async def reconcile(self) -> dict[str, object]:
        async with self._lock:
            now = self._monotonic()
            workers: dict[str, dict[str, object]] = {}
            aggregate = "healthy"

            for name, spec in self._specs.items():
                runtime = self._runtime[name]
                self._trim_restart_window(spec, runtime, now)
                self._capture_terminal_error(runtime)

                task = runtime.task
                if task is None or task.done():
                    if len(runtime.restart_times) >= spec.restart_limit:
                        status = "restart_suspended"
                        aggregate = "unhealthy" if spec.critical else "moderate"
                    else:
                        self._start_worker(spec, runtime, now)
                        task = runtime.task
                        status = "restarted"
                else:
                    heartbeat = runtime.last_heartbeat_at
                    heartbeat_age = None if heartbeat is None else max(0.0, now - heartbeat)
                    success_reference = runtime.last_success_at or runtime.started_at
                    success_age = (
                        None if success_reference is None else max(0.0, now - success_reference)
                    )
                    if heartbeat_age is not None and heartbeat_age > spec.stale_after_seconds:
                        status = "stalled"
                        if aggregate == "healthy":
                            aggregate = "moderate"
                    elif (
                        spec.success_stale_after_seconds is not None
                        and success_age is not None
                        and success_age > spec.success_stale_after_seconds
                    ):
                        status = "degraded"
                        if aggregate == "healthy":
                            aggregate = "moderate"
                    else:
                        status = "healthy"

                task = runtime.task
                heartbeat = runtime.last_heartbeat_at
                heartbeat_age = None if heartbeat is None else max(0.0, now - heartbeat)
                success_reference = runtime.last_success_at or runtime.started_at
                success_age = (
                    None if success_reference is None else max(0.0, now - success_reference)
                )
                workers[name] = {
                    "status": status,
                    "critical": spec.critical,
                    "task_running": bool(task is not None and not task.done()),
                    "stale_after_seconds": spec.stale_after_seconds,
                    "heartbeat_age_seconds": heartbeat_age,
                    "success_stale_after_seconds": spec.success_stale_after_seconds,
                    "last_success_age_seconds": success_age,
                    "restart_count_window": len(runtime.restart_times),
                    "restart_limit": spec.restart_limit,
                    "restart_window_seconds": spec.restart_window_seconds,
                    "last_error": runtime.last_error,
                }

            return {
                "aggregate": aggregate,
                "worker_count": len(workers),
                "workers": workers,
            }

    async def snapshot(self) -> dict[str, object]:
        """Return worker health without starting, cancelling, or restarting tasks."""
        async with self._lock:
            now = self._monotonic()
            workers: dict[str, dict[str, object]] = {}
            aggregate = "healthy"
            for name, spec in self._specs.items():
                runtime = self._runtime[name]
                self._trim_restart_window(spec, runtime, now)
                self._capture_terminal_error(runtime)
                task = runtime.task
                heartbeat = runtime.last_heartbeat_at
                heartbeat_age = None if heartbeat is None else max(0.0, now - heartbeat)
                success_reference = runtime.last_success_at or runtime.started_at
                success_age = (
                    None if success_reference is None else max(0.0, now - success_reference)
                )

                if task is None:
                    status = "missing"
                    aggregate = "unhealthy" if spec.critical else "moderate"
                elif task.done():
                    if len(runtime.restart_times) >= spec.restart_limit:
                        status = "restart_suspended"
                    else:
                        status = "crashed"
                    aggregate = "unhealthy" if spec.critical else "moderate"
                elif heartbeat_age is not None and heartbeat_age > spec.stale_after_seconds:
                    status = "stalled"
                    if aggregate == "healthy":
                        aggregate = "moderate"
                elif (
                    spec.success_stale_after_seconds is not None
                    and success_age is not None
                    and success_age > spec.success_stale_after_seconds
                ):
                    status = "degraded"
                    if aggregate == "healthy":
                        aggregate = "moderate"
                else:
                    status = "healthy"

                workers[name] = {
                    "status": status,
                    "critical": spec.critical,
                    "task_running": bool(task is not None and not task.done()),
                    "stale_after_seconds": spec.stale_after_seconds,
                    "heartbeat_age_seconds": heartbeat_age,
                    "success_stale_after_seconds": spec.success_stale_after_seconds,
                    "last_success_age_seconds": success_age,
                    "restart_count_window": len(runtime.restart_times),
                    "restart_limit": spec.restart_limit,
                    "restart_window_seconds": spec.restart_window_seconds,
                    "last_error": runtime.last_error,
                }
            return {
                "aggregate": aggregate,
                "worker_count": len(workers),
                "workers": workers,
            }

    async def start(self) -> dict[str, object]:
        result = await self.reconcile()
        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = asyncio.create_task(
                self._supervisor_loop(), name="cptr-background-worker-watchdog"
            )
        return result

    async def _supervisor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.check_interval_seconds)
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Reconciliation failures must not kill the watchdog itself.
                await asyncio.sleep(self.check_interval_seconds)

    async def stop(self) -> None:
        task = self._supervisor_task
        self._supervisor_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def close(self, *, cancel_workers: bool = True) -> None:
        await self.stop()
        if not cancel_workers:
            return
        tasks = [
            runtime.task
            for runtime in self._runtime.values()
            if runtime.task is not None and not runtime.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def clear(self) -> None:
        """Reset registrations after a fully quiesced application lifespan."""
        if self._supervisor_task is not None and not self._supervisor_task.done():
            raise RuntimeError("cannot clear a running worker watchdog")
        if any(
            runtime.task is not None and not runtime.task.done()
            for runtime in self._runtime.values()
        ):
            raise RuntimeError("cannot clear while managed workers are running")
        self._specs.clear()
        self._runtime.clear()


worker_watchdog = WorkerWatchdog()


def heartbeat_worker(name: str, *, success: bool = False) -> None:
    """Cheap model-free heartbeat used by permanent worker loops."""
    worker_watchdog.heartbeat(name, success=success)


async def heartbeat_sleep(
    name: str,
    delay_seconds: float,
    *,
    pulse_seconds: float = 15.0,
    success: bool = False,
) -> None:
    """Sleep while preserving liveness; optionally preserve known-good idle state."""
    remaining = max(0.0, float(delay_seconds))
    pulse = max(0.1, float(pulse_seconds))
    while remaining > 0:
        interval = min(pulse, remaining)
        await asyncio.sleep(interval)
        remaining -= interval
        heartbeat_worker(name, success=success)


def configure_runtime_workers(app: object, *, watchdog: WorkerWatchdog = worker_watchdog) -> WorkerWatchdog:
    """Register CPTR's permanent model-free runtime loops with one supervisor."""
    from cptr.env import (
        AUTOMATION_POLL_INTERVAL,
        COMMAND_SESSION_REAPER_INTERVAL_SECONDS,
        EVENT_LOOP_LAG_SAMPLE_INTERVAL_MS,
        TIMER_POLL_INTERVAL,
        WORKBENCH_SESSION_REAPER_INTERVAL_SECONDS,
    )
    from cptr.memory.worker import memory_worker_loop
    from cptr.services.runtime_metrics import event_loop_lag_worker
    from cptr.services.workbench_sessions import workbench_session_reaper_loop
    from cptr.utils.automations import scheduler_worker_loop
    from cptr.utils.timers import timer_worker_loop
    from cptr.utils.tools import command_session_reaper_loop

    watchdog.register(
        name="command_reaper",
        task_factory=command_session_reaper_loop,
        stale_after_seconds=max(30.0, min(60.0, COMMAND_SESSION_REAPER_INTERVAL_SECONDS / 2)),
        success_stale_after_seconds=max(60.0, COMMAND_SESSION_REAPER_INTERVAL_SECONDS * 2.0),
    )
    watchdog.register(
        name="workbench_reaper",
        task_factory=workbench_session_reaper_loop,
        stale_after_seconds=max(30.0, min(60.0, WORKBENCH_SESSION_REAPER_INTERVAL_SECONDS / 2)),
        success_stale_after_seconds=max(60.0, WORKBENCH_SESSION_REAPER_INTERVAL_SECONDS * 2.0),
    )
    watchdog.register(
        name="event_loop_monitor",
        task_factory=event_loop_lag_worker,
        stale_after_seconds=max(5.0, (EVENT_LOOP_LAG_SAMPLE_INTERVAL_MS / 1000.0) * 5),
        success_stale_after_seconds=max(
            5.0, (EVENT_LOOP_LAG_SAMPLE_INTERVAL_MS / 1000.0) * 5
        ),
    )
    watchdog.register(
        name="memory_maintenance",
        task_factory=memory_worker_loop,
        stale_after_seconds=60.0,
        success_stale_after_seconds=120.0,
    )
    watchdog.register(
        name="automation_scheduler",
        task_factory=lambda: scheduler_worker_loop(app),
        stale_after_seconds=max(15.0, AUTOMATION_POLL_INTERVAL * 3.0),
        success_stale_after_seconds=max(30.0, AUTOMATION_POLL_INTERVAL * 4.0),
    )
    watchdog.register(
        name="timer_worker",
        task_factory=lambda: timer_worker_loop(app),
        stale_after_seconds=max(5.0, TIMER_POLL_INTERVAL * 5.0),
        success_stale_after_seconds=max(10.0, TIMER_POLL_INTERVAL * 8.0),
    )
    return watchdog
