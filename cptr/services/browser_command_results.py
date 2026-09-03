"""Bounded in-memory correlation for outstanding browser commands."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any


class BrowserCommandResultRegistry:
    def __init__(self, *, max_completed: int = 512) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._completed: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max_completed = max(16, max_completed)
        self._lock = asyncio.Lock()

    async def reserve(self, command_id: str) -> asyncio.Future[dict[str, Any]]:
        async with self._lock:
            completed = self._completed.get(command_id)
            if completed is not None:
                future = asyncio.get_running_loop().create_future()
                future.set_result(dict(completed))
                return future
            pending = self._pending.get(command_id)
            if pending is not None:
                return pending
            future = asyncio.get_running_loop().create_future()
            self._pending[command_id] = future
            return future

    async def complete(self, command_id: str, result: dict[str, Any]) -> bool:
        async with self._lock:
            if command_id in self._completed:
                return False
            future = self._pending.pop(command_id, None)
            self._completed[command_id] = dict(result)
            self._completed.move_to_end(command_id)
            while len(self._completed) > self._max_completed:
                self._completed.popitem(last=False)
        if future is not None and not future.done():
            future.set_result(dict(result))
        return True

    async def abandon(self, command_id: str) -> None:
        async with self._lock:
            future = self._pending.pop(command_id, None)
        if future is not None and not future.done():
            future.cancel()

    async def wait(self, command_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        future = await self.reserve(command_id)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            raise TimeoutError("browser command timed out") from None


browser_command_results = BrowserCommandResultRegistry()
