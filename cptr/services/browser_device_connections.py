"""In-memory live browser-device connection registry with bounded control delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket


@dataclass
class DeviceConnection:
    device_id: str
    websocket: WebSocket
    send_lock: asyncio.Lock


class BrowserDeviceConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, DeviceConnection] = {}
        self._lock = asyncio.Lock()

    async def attach(self, *, device_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            previous = self._connections.get(device_id)
            self._connections[device_id] = DeviceConnection(
                device_id=device_id,
                websocket=websocket,
                send_lock=asyncio.Lock(),
            )
        if previous is not None and previous.websocket is not websocket:
            try:
                await previous.websocket.close(code=1012, reason="device connection replaced")
            except Exception:
                pass

    async def detach(self, *, device_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            current = self._connections.get(device_id)
            if current is not None and current.websocket is websocket:
                self._connections.pop(device_id, None)

    async def send_control(self, *, device_id: str, message: dict[str, Any]) -> bool:
        async with self._lock:
            connection = self._connections.get(device_id)
        if connection is None:
            return False
        async with connection.send_lock:
            try:
                await connection.websocket.send_json(message)
                return True
            except Exception:
                await self.detach(device_id=device_id, websocket=connection.websocket)
                return False

    async def is_connected(self, *, device_id: str) -> bool:
        async with self._lock:
            return device_id in self._connections


browser_device_connections = BrowserDeviceConnectionRegistry()
