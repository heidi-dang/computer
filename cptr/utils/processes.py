"""Process-group lifecycle helpers for task-owned subprocesses."""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
from contextlib import suppress
from typing import Any


def signal_process_group(pid: int, *, force: bool = False) -> None:
    """Signal one task-owned process group without touching its parent group."""
    sig = signal.SIGKILL if force else signal.SIGTERM
    if os.name != "nt":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)
            return
    with suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


async def terminate_process_group(proc: Any, *, timeout: float = 3.0) -> None:
    """Terminate a process and its descendants, escalating within a bound."""
    if proc is None or getattr(proc, "returncode", None) is not None:
        return
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int):
        signal_process_group(pid)
    else:
        with suppress(ProcessLookupError):
            proc.terminate()
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        return
    try:
        result = wait()
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=max(0.1, timeout))
        return
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    if isinstance(pid, int):
        signal_process_group(pid, force=True)
    else:
        with suppress(ProcessLookupError):
            proc.kill()
    with suppress(asyncio.TimeoutError, ProcessLookupError):
        result = wait()
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=1.0)
