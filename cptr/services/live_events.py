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

from sqlalchemy import delete, func, select

from cptr.env import (
    LIVE_EVENT_QUEUE_SIZE,
    LIVE_EVENT_RETENTION_CLEANUP_INTERVAL,
    LIVE_EVENT_WRITE_BATCH_SIZE,
)
from cptr.models import ControlLiveEvent
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_external, redact_sensitive

MAX_EVENT_PAYLOAD_CHARS = 12_000
MAX_TERMINAL_CHUNK_CHARS = 8_192
MAX_REPLAY_EVENTS = 500
logger = logging.getLogger(__name__)

_OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STOP_WRITER = object()


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


@dataclass
class _PendingWrite:
    user_id: str
    target_key: str
    task_id: str | None
    monitor_id: str | None
    worker_task_id: str | None
    event_type: str
    payload: dict[str, Any]
    created_at: int
    future: asyncio.Future[LiveEventEnvelope]


class LiveEventStore:
    """Durable store in production; in-memory store for isolated unit tests.

    Persistent writes are queued and committed in batches. Sequence maxima are
    loaded once per target per process instead of querying MAX(sequence) for
    every event. This preserves durable replay semantics while removing the
    former global lock + multi-commit cost from each publish.
    """

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
        self._writer_start_lock = asyncio.Lock()
        self._write_queue: asyncio.Queue[Any] | None = (
            asyncio.Queue(maxsize=LIVE_EVENT_QUEUE_SIZE) if persistent else None
        )
        self._writer_task: asyncio.Task | None = None
        self._sequence_cache: dict[str, int] = {}
        self._closed = False
        self._write_batches = 0
        self._written_events = 0
        self._retention_cleanups = 0

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
        now = int(time.time() * 1000)
        safe_payload = _cap(payload or {}, limit=self.max_payload_chars)
        if not self.persistent:
            async with self._lock:
                current = self._events.setdefault(target_key, [])
                sequence = (current[-1].sequence if current else 0) + 1
                envelope = self._envelope(
                    event_id=str(uuid.uuid4()),
                    sequence=sequence,
                    created_at=now,
                    user_id=user_id,
                    target_key=target_key,
                    task_id=task_id,
                    monitor_id=monitor_id,
                    worker_task_id=worker_task_id,
                    event_type=event_type,
                    payload=safe_payload,
                )
                current.append(envelope)
                self._events[target_key] = current[-MAX_REPLAY_EVENTS:]
                self._written_events += 1
                return envelope

        if self._closed:
            raise RuntimeError("live event store is closed")
        await self._ensure_writer()
        queue = self._write_queue
        if queue is None:
            raise RuntimeError("persistent live event queue is unavailable")
        future: asyncio.Future[LiveEventEnvelope] = asyncio.get_running_loop().create_future()
        await queue.put(
            _PendingWrite(
                user_id=user_id,
                target_key=target_key,
                task_id=task_id,
                monitor_id=monitor_id,
                worker_task_id=worker_task_id,
                event_type=event_type,
                payload=safe_payload,
                created_at=now,
                future=future,
            )
        )
        return await future

    async def _ensure_writer(self) -> None:
        if self._writer_task is not None and not self._writer_task.done():
            return
        async with self._writer_start_lock:
            if self._writer_task is None or self._writer_task.done():
                self._writer_task = asyncio.create_task(
                    self._writer_loop(), name="cptr-live-event-writer"
                )

    async def _writer_loop(self) -> None:
        queue = self._write_queue
        if queue is None:
            return
        while True:
            first = await queue.get()
            if first is _STOP_WRITER:
                queue.task_done()
                return
            batch: list[_PendingWrite] = [first]
            # Yield once so concurrently arriving events can share one commit.
            await asyncio.sleep(0)
            while len(batch) < LIVE_EVENT_WRITE_BATCH_SIZE:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is _STOP_WRITER:
                    queue.task_done()
                    # Reinsert after this batch so all accepted writes flush first.
                    queue.put_nowait(_STOP_WRITER)
                    break
                batch.append(item)
            try:
                await self._persist_batch(batch)
            except Exception as exc:
                logger.exception("live-event durability batch failed")
                for pending in batch:
                    if not pending.future.done():
                        pending.future.set_exception(exc)
            finally:
                for _ in batch:
                    queue.task_done()

    async def _persist_batch(self, batch: list[_PendingWrite]) -> None:
        missing_targets = {
            pending.target_key
            for pending in batch
            if pending.target_key not in self._sequence_cache
        }
        async with await get_db() as db:
            if missing_targets:
                rows = await db.execute(
                    select(ControlLiveEvent.target_key, func.max(ControlLiveEvent.sequence))
                    .where(ControlLiveEvent.target_key.in_(missing_targets))
                    .group_by(ControlLiveEvent.target_key)
                )
                maxima = {str(target): int(maximum or 0) for target, maximum in rows.all()}
                for target in missing_targets:
                    self._sequence_cache[target] = maxima.get(target, 0)

            envelopes: list[LiveEventEnvelope] = []
            cleanup_targets: dict[str, int] = {}
            rows_to_add: list[ControlLiveEvent] = []
            for pending in batch:
                sequence = self._sequence_cache[pending.target_key] + 1
                self._sequence_cache[pending.target_key] = sequence
                event_id = str(uuid.uuid4())
                rows_to_add.append(
                    ControlLiveEvent(
                        id=event_id,
                        user_id=pending.user_id,
                        target_key=pending.target_key,
                        sequence=sequence,
                        task_id=pending.task_id,
                        monitor_id=pending.monitor_id,
                        worker_task_id=pending.worker_task_id,
                        event_type=pending.event_type,
                        payload=pending.payload,
                        created_at=pending.created_at,
                    )
                )
                envelopes.append(
                    self._envelope(
                        event_id=event_id,
                        sequence=sequence,
                        created_at=pending.created_at,
                        user_id=pending.user_id,
                        target_key=pending.target_key,
                        task_id=pending.task_id,
                        monitor_id=pending.monitor_id,
                        worker_task_id=pending.worker_task_id,
                        event_type=pending.event_type,
                        payload=pending.payload,
                    )
                )
                if (
                    sequence > MAX_REPLAY_EVENTS
                    and sequence % LIVE_EVENT_RETENTION_CLEANUP_INTERVAL == 0
                ):
                    cleanup_targets[pending.target_key] = sequence - MAX_REPLAY_EVENTS

            db.add_all(rows_to_add)
            for target_key, cutoff in cleanup_targets.items():
                await db.execute(
                    delete(ControlLiveEvent).where(
                        ControlLiveEvent.target_key == target_key,
                        ControlLiveEvent.sequence <= cutoff,
                    )
                )
            await db.commit()

        self._write_batches += 1
        self._written_events += len(batch)
        self._retention_cleanups += len(cleanup_targets)
        for pending, envelope in zip(batch, envelopes):
            if not pending.future.done():
                pending.future.set_result(envelope)

    @staticmethod
    def _envelope(
        *,
        event_id: str,
        sequence: int,
        created_at: int,
        user_id: str,
        target_key: str,
        task_id: str | None,
        monitor_id: str | None,
        worker_task_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> LiveEventEnvelope:
        return LiveEventEnvelope(
            event_id=event_id,
            sequence=sequence,
            timestamp=datetime.fromtimestamp(created_at / 1000, tz=timezone.utc).isoformat(),
            user_id=user_id,
            target_key=target_key,
            task_id=task_id,
            monitor_id=monitor_id,
            worker_task_id=worker_task_id,
            event_type=event_type,
            payload=payload,
        )

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
        events = await self.replay(target_key, after_sequence=after_sequence, limit=limit)
        return {
            "target_key": target_key,
            "after_sequence": after_sequence,
            "last_sequence": events[-1].sequence if events else after_sequence,
            "events": [event.to_dict() for event in events],
        }

    async def start(self) -> None:
        """Re-open the store for a fresh application lifespan."""
        self._closed = False

    async def close(self) -> None:
        if not self.persistent or self._closed:
            return
        self._closed = True
        queue = self._write_queue
        task = self._writer_task
        if queue is None or task is None:
            return
        await queue.join()
        await queue.put(_STOP_WRITER)
        await task

    def stats(self) -> dict[str, int]:
        queue = self._write_queue
        return {
            "queue_depth": queue.qsize() if queue is not None else 0,
            "queue_capacity": LIVE_EVENT_QUEUE_SIZE if queue is not None else 0,
            "write_batches": self._write_batches,
            "written_events": self._written_events,
            "retention_cleanups": self._retention_cleanups,
            "sequence_targets": len(self._sequence_cache),
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
        self._slow_subscriber_disconnects = 0

    async def publish(self, **kwargs: Any) -> LiveEventEnvelope:
        event = await self.store.append(**kwargs)
        async with self._subscriber_lock:
            subscribers = list(self._subscribers.get(event.target_key, set()))
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._slow_subscriber_disconnects += 1
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

    async def start(self) -> None:
        await self.store.start()

    async def close(self) -> None:
        await self.store.close()
        async with self._subscriber_lock:
            subscribers = [queue for queues in self._subscribers.values() for queue in queues]
            self._subscribers.clear()
        for queue in subscribers:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)

    def stats(self) -> dict[str, int]:
        subscriber_count = sum(len(items) for items in self._subscribers.values())
        return {
            **self.store.stats(),
            "subscriber_count": subscriber_count,
            "subscriber_targets": len(self._subscribers),
            "slow_subscriber_disconnects": self._slow_subscriber_disconnects,
        }


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
    try:
        return await publish_task_event(**kwargs)
    except Exception:
        logger.debug("live task event unavailable", exc_info=True)
        return None


def command_target_key(workspace_id: str, command_id: str) -> str:
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
    try:
        return await publish_monitor_event(**kwargs)
    except Exception:
        logger.debug("live monitor event unavailable", exc_info=True)
        return None
