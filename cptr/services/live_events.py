"""Authoritative, bounded CPTR Live Workbench event publication and replay."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, desc, func, select

from cptr.models import ControlLiveEvent
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_external, redact_sensitive

MAX_EVENT_PAYLOAD_CHARS = 12_000
MAX_TERMINAL_CHUNK_CHARS = 4_096
MAX_REPLAY_EVENTS = 500
logger = logging.getLogger(__name__)

# Terminal output is untrusted text. Remove control sequences that can influence
# terminal emulators, clipboards, titles, or rendering before the event is stored.
_OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_terminal_text(value: str, *, limit: int = MAX_TERMINAL_CHUNK_CHARS) -> str:
    """Return redacted, display-safe terminal text with bounded output."""
    text = _OSC_ESCAPE_RE.sub("", value)
    text = _CSI_ESCAPE_RE.sub("", text)
    text = _UNSAFE_CONTROL_RE.sub("", text)
    text = redact_external(text)
    if len(text) > limit:
        return f"{text[:limit]}… [truncated]"
    return text


def _cap(value: Any, *, limit: int = MAX_EVENT_PAYLOAD_CHARS) -> Any:
    value = redact_sensitive(value)
    if isinstance(value, dict):
        return {str(key): _cap(item, limit=limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_cap(item, limit=limit) for item in value[:200]]
    if isinstance(value, str):
        return sanitize_terminal_text(value, limit=limit)
    return value


@dataclass(frozen=True)
class LiveEventEnvelope:
    event_id: str
    sequence: int
    timestamp: str
    user_id: str
    target_key: str
    task_id: str | None
    monitor_id: str | None
    worker_task_id: str | None
    event_type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        target_type, _, target_id = self.target_key.partition(":")
        return {
            "version": 1,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "target": {"type": target_type, "id": target_id},
            "task_id": self.task_id,
            "monitor_id": self.monitor_id,
            "worker_task_id": self.worker_task_id,
            "type": self.event_type,
            "payload": self.payload,
            "redaction_applied": True,
        }


class LiveEventStore:
    """Durable store in production; in-memory store for isolated unit tests."""

    def __init__(
        self,
        *,
        max_payload_chars: int = MAX_EVENT_PAYLOAD_CHARS,
        persistent: bool = False,
    ):
        self.max_payload_chars = max_payload_chars
        self.persistent = persistent
        self._events: dict[str, list[LiveEventEnvelope]] = {}
        self._lock = asyncio.Lock()

    async def append(
        self,
        *,
        user_id: str,
        target_key: str,
        task_id: str | None = None,
        monitor_id: str | None = None,
        worker_task_id: str | None = None,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> LiveEventEnvelope:
        async with self._lock:
            now = int(time.time() * 1000)
            safe_payload = _cap(payload or {}, limit=self.max_payload_chars)
            if self.persistent:
                async with await get_db() as db:
                    maximum = await db.scalar(
                        select(func.max(ControlLiveEvent.sequence)).where(
                            ControlLiveEvent.target_key == target_key
                        )
                    )
                    sequence = int(maximum or 0) + 1
                    row = ControlLiveEvent(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        target_key=target_key,
                        sequence=sequence,
                        task_id=task_id,
                        monitor_id=monitor_id,
                        worker_task_id=worker_task_id,
                        event_type=event_type,
                        payload=safe_payload,
                        created_at=now,
                    )
                    db.add(row)
                    await db.commit()
                    # Keep the durable replay window bounded per target. A
                    # reconnect can still recover older events only while a
                    # client holds a valid cursor inside this window.
                    stale_ids = (
                        await db.scalars(
                            select(ControlLiveEvent.id)
                            .where(ControlLiveEvent.target_key == target_key)
                            .order_by(desc(ControlLiveEvent.sequence))
                            .offset(MAX_REPLAY_EVENTS)
                        )
                    ).all()
                    if stale_ids:
                        await db.execute(
                            delete(ControlLiveEvent).where(ControlLiveEvent.id.in_(stale_ids))
                        )
                        await db.commit()
                    event_id = row.id
            else:
                current = self._events.setdefault(target_key, [])
                sequence = (current[-1].sequence if current else 0) + 1
                event_id = str(uuid.uuid4())

            envelope = LiveEventEnvelope(
                event_id=event_id,
                sequence=sequence,
                timestamp=datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat(),
                user_id=user_id,
                target_key=target_key,
                task_id=task_id,
                monitor_id=monitor_id,
                worker_task_id=worker_task_id,
                event_type=event_type,
                payload=safe_payload,
            )
            if not self.persistent:
                self._events.setdefault(target_key, []).append(envelope)
                self._events[target_key] = self._events[target_key][-MAX_REPLAY_EVENTS:]
            return envelope

    async def replay(
        self,
        target_key: str,
        *,
        after_sequence: int = 0,
        limit: int = MAX_REPLAY_EVENTS,
    ) -> list[LiveEventEnvelope]:
        limit = max(1, min(limit, MAX_REPLAY_EVENTS))
        if self.persistent:
            async with await get_db() as db:
                rows = (
                    await db.scalars(
                        select(ControlLiveEvent)
                        .where(
                            ControlLiveEvent.target_key == target_key,
                            ControlLiveEvent.sequence > after_sequence,
                        )
                        .order_by(ControlLiveEvent.sequence.asc())
                        .limit(limit)
                    )
                ).all()
            return [self._from_row(row) for row in rows]
        async with self._lock:
            return [
                event
                for event in self._events.get(target_key, [])
                if event.sequence > after_sequence
            ][:limit]

    async def snapshot(
        self,
        target_key: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return a bounded, redacted replay snapshot for stream recovery."""
        events = await self.replay(target_key, after_sequence=after_sequence, limit=limit)
        return {
            "target_key": target_key,
            "after_sequence": after_sequence,
            "last_sequence": events[-1].sequence if events else after_sequence,
            "events": [event.to_dict() for event in events],
        }

    @staticmethod
    def _from_row(row: ControlLiveEvent) -> LiveEventEnvelope:
        return LiveEventEnvelope(
            event_id=row.id,
            sequence=int(row.sequence),
            timestamp=datetime.fromtimestamp(row.created_at / 1000, tz=timezone.utc).isoformat(),
            user_id=row.user_id,
            target_key=row.target_key,
            task_id=row.task_id,
            monitor_id=row.monitor_id,
            worker_task_id=row.worker_task_id,
            event_type=row.event_type,
            payload=_cap(row.payload or {}),
        )


class LiveEventHub:
    def __init__(self, *, store: LiveEventStore | None = None):
        self.store = store or LiveEventStore(persistent=True)
        self._subscribers: dict[str, set[asyncio.Queue[LiveEventEnvelope | None]]] = {}
        self._subscriber_lock = asyncio.Lock()

    async def publish(self, **kwargs: Any) -> LiveEventEnvelope:
        event = await self.store.append(**kwargs)
        async with self._subscriber_lock:
            subscribers = list(self._subscribers.get(event.target_key, set()))
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow client must not turn into unbounded memory growth.
                # Close this subscription so the client reconnects with its
                # last acknowledged cursor and replays the missed window.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
        return event

    async def subscribe(
        self,
        target_key: str,
        *,
        after_sequence: int = 0,
        queue_size: int = 128,
    ) -> AsyncIterator[LiveEventEnvelope]:
        queue: asyncio.Queue[LiveEventEnvelope | None] = asyncio.Queue(maxsize=max(8, queue_size))
        async with self._subscriber_lock:
            self._subscribers.setdefault(target_key, set()).add(queue)
        try:
            replay = await self.store.replay(target_key, after_sequence=after_sequence)
            last_sequence = after_sequence
            for event in replay:
                if event.sequence > last_sequence:
                    last_sequence = event.sequence
                    yield event
            while True:
                event = await queue.get()
                if event is None:
                    return
                if event.sequence <= last_sequence:
                    continue
                last_sequence = event.sequence
                yield event
        finally:
            async with self._subscriber_lock:
                subscribers = self._subscribers.get(target_key)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(target_key, None)


live_event_hub = LiveEventHub()


async def publish_task_event(
    *,
    user_id: str,
    task_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    worker_task_id: str | None = None,
) -> LiveEventEnvelope:
    return await live_event_hub.publish(
        user_id=user_id,
        target_key=f"task:{task_id}",
        task_id=task_id,
        worker_task_id=worker_task_id or task_id,
        event_type=event_type,
        payload=payload,
    )


async def safe_publish_task_event(**kwargs: Any) -> LiveEventEnvelope | None:
    """Publish activity without making the worker lifecycle depend on telemetry."""
    try:
        return await publish_task_event(**kwargs)
    except Exception:
        logger.debug("live task event unavailable", exc_info=True)
        return None


def command_target_key(workspace_id: str, command_id: str) -> str:
    """Return the collision-safe live-event key for one workspace-owned command."""
    return f"command:{workspace_id}:{command_id}"


async def publish_command_event(
    *,
    user_id: str,
    workspace_id: str,
    command_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> LiveEventEnvelope:
    return await live_event_hub.publish(
        user_id=user_id,
        target_key=command_target_key(workspace_id, command_id),
        event_type=event_type,
        payload=payload,
    )


async def safe_publish_command_event(**kwargs: Any) -> LiveEventEnvelope | None:
    try:
        return await publish_command_event(**kwargs)
    except Exception:
        logger.debug("live command event unavailable", exc_info=True)
        return None


async def publish_terminal_event(
    *,
    user_id: str,
    target_type: str,
    target_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    worker_task_id: str | None = None,
    workspace_id: str | None = None,
) -> LiveEventEnvelope:
    """Publish a normalized terminal event to the already-authorized target stream."""
    if target_type == "task":
        return await publish_task_event(
            user_id=user_id,
            task_id=target_id,
            event_type=event_type,
            payload=payload,
            worker_task_id=worker_task_id,
        )
    if target_type == "monitor":
        return await publish_monitor_event(
            user_id=user_id,
            monitor_id=target_id,
            event_type=event_type,
            payload=payload,
            task_id=worker_task_id,
        )
    if target_type == "command":
        if not workspace_id:
            raise ValueError("workspace_id is required for a command live target")
        return await publish_command_event(
            user_id=user_id,
            workspace_id=workspace_id,
            command_id=target_id,
            event_type=event_type,
            payload=payload,
        )
    raise ValueError("unsupported live terminal target")


async def safe_publish_terminal_event(**kwargs: Any) -> LiveEventEnvelope | None:
    try:
        return await publish_terminal_event(**kwargs)
    except Exception:
        logger.debug("live terminal event unavailable", exc_info=True)
        return None


async def publish_monitor_event(
    *,
    user_id: str,
    monitor_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> LiveEventEnvelope:
    return await live_event_hub.publish(
        user_id=user_id,
        target_key=f"monitor:{monitor_id}",
        monitor_id=monitor_id,
        worker_task_id=task_id,
        event_type=event_type,
        payload=payload,
    )


async def safe_publish_monitor_event(**kwargs: Any) -> LiveEventEnvelope | None:
    """Publish monitor activity without changing supervisor correctness."""
    try:
        return await publish_monitor_event(**kwargs)
    except Exception:
        logger.debug("live monitor event unavailable", exc_info=True)
        return None
