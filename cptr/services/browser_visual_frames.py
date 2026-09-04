"""Bounded latest-frame store for paired user Chrome visual streaming."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_FRAME_ID = 160
MAX_SESSION_ID = 120
MAX_MIME = 64


@dataclass(frozen=True)
class BrowserVisualFrame:
    device_id: str
    session_id: str
    frame_id: str
    mime_type: str
    width: int
    height: int
    created_at_ms: int
    data: bytes


class BrowserVisualFrameStore:
    def __init__(self) -> None:
        # A paired Chrome device can host several concurrent CPTR sessions on
        # different tabs. Keep the latest visual frame per session so one live
        # card can never overwrite or starve another card on the same device.
        self._latest: dict[tuple[str, str], BrowserVisualFrame] = {}
        self._conditions: dict[tuple[str, str], asyncio.Condition] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(device_id: str, session_id: str) -> tuple[str, str]:
        if not device_id:
            raise ValueError("device id is required")
        if not session_id or len(session_id) > MAX_SESSION_ID:
            raise ValueError("invalid browser session id")
        return device_id, session_id

    async def put(self, frame: BrowserVisualFrame) -> None:
        key = self._key(frame.device_id, frame.session_id)
        if not frame.frame_id or len(frame.frame_id) > MAX_FRAME_ID:
            raise ValueError("invalid browser frame id")
        if frame.mime_type not in {"image/jpeg", "image/webp"} or len(frame.mime_type) > MAX_MIME:
            raise ValueError("unsupported browser frame type")
        if frame.width < 1 or frame.width > 7680 or frame.height < 1 or frame.height > 4320:
            raise ValueError("invalid browser frame dimensions")
        if not frame.data or len(frame.data) > MAX_FRAME_BYTES:
            raise ValueError("browser frame exceeds bounded size")
        async with self._lock:
            self._latest[key] = frame
            condition = self._conditions.setdefault(key, asyncio.Condition())
        async with condition:
            condition.notify_all()

    async def latest(self, *, device_id: str, session_id: str) -> BrowserVisualFrame | None:
        key = self._key(device_id, session_id)
        async with self._lock:
            return self._latest.get(key)

    async def wait_next(
        self,
        *,
        device_id: str,
        session_id: str,
        after_frame_id: str | None,
        timeout_seconds: float = 15.0,
    ) -> BrowserVisualFrame | None:
        key = self._key(device_id, session_id)
        current = await self.latest(device_id=device_id, session_id=session_id)
        if current is not None and current.frame_id != after_frame_id:
            return current
        async with self._lock:
            condition = self._conditions.setdefault(key, asyncio.Condition())
        try:
            async with condition:
                await asyncio.wait_for(
                    condition.wait(), timeout=max(0.1, min(timeout_seconds, 30.0))
                )
        except TimeoutError:
            return None
        current = await self.latest(device_id=device_id, session_id=session_id)
        if current is None or current.frame_id == after_frame_id:
            return None
        return current

    async def clear(self, *, device_id: str, session_id: str | None = None) -> None:
        async with self._lock:
            if session_id is not None:
                key = self._key(device_id, session_id)
                self._latest.pop(key, None)
                self._conditions.pop(key, None)
                return
            keys = [key for key in self._latest if key[0] == device_id]
            for key in keys:
                self._latest.pop(key, None)
                self._conditions.pop(key, None)


browser_visual_frames = BrowserVisualFrameStore()
