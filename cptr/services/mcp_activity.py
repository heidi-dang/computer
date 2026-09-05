"""Bounded in-memory MCP tool activity for the admin Console.

Unlike ``mcp_traffic``, this channel may carry already-redacted bounded tool
input/output prepared by the MCP adapter. It remains non-durable and accepts
only a strict allowlisted event envelope.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cptr.services.mcp_client_policy import is_active_mcp_client_id


class McpActivityClient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=80)
    version: str | None = Field(default=None, max_length=64)


class McpActivityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    event_id: str = Field(min_length=8, max_length=128)
    sequence: int = Field(ge=1)
    timestamp_ms: int = Field(ge=0)
    client: McpActivityClient
    session_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    tool_name: str = Field(min_length=1, max_length=256)
    title: str | None = Field(default=None, max_length=160)
    phase: Literal["started", "complete", "failed"]
    summary: str = Field(min_length=1, max_length=500)
    arguments_json: str | None = Field(default=None, max_length=13_000)
    result_json: str | None = Field(default=None, max_length=13_000)
    error_json: str | None = Field(default=None, max_length=13_000)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)


class McpActivityBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[McpActivityEvent] = Field(min_length=1, max_length=100)


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


class McpActivityStore:
    """Small bounded event store with non-blocking subscriber fan-out."""

    def __init__(self, *, max_events: int = 250, subscriber_queue_size: int = 64) -> None:
        self.max_events = max(1, int(max_events))
        self.subscriber_queue_size = max(1, int(subscriber_queue_size))
        self._dedupe_capacity = max(32, self.max_events * 4)
        self._lock = asyncio.Lock()
        self._events: deque[dict[str, object]] = deque(maxlen=self.max_events)
        self._seen_event_ids: deque[str] = deque()
        self._seen_event_id_set: set[str] = set()
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self._ingestion_sequence = 0
        self._slow_subscriber_drops = 0

    def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=self.subscriber_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        self._subscribers.discard(queue)

    async def ingest(self, events: list[McpActivityEvent]) -> dict[str, int]:
        accepted = 0
        duplicates = 0
        dropped = 0
        async with self._lock:
            for event in events:
                if not is_active_mcp_client_id(event.client.id):
                    dropped += 1
                    continue
                if event.event_id in self._seen_event_id_set:
                    duplicates += 1
                    continue
                self._remember_event_id(event.event_id)
                self._ingestion_sequence += 1
                projected = event.model_dump(mode="json")
                projected["ingestion_sequence"] = self._ingestion_sequence
                self._events.append(projected)
                self._fan_out(projected)
                accepted += 1
        return {"accepted": accepted, "duplicates": duplicates, "dropped": dropped}

    async def snapshot(self) -> dict[str, object]:
        async with self._lock:
            self._events = deque(
                (
                    event
                    for event in self._events
                    if isinstance(event.get("client"), dict)
                    and is_active_mcp_client_id(event["client"].get("id"))
                ),
                maxlen=self.max_events,
            )
            return {
                "version": 1,
                "sequence": self._ingestion_sequence,
                "events": list(self._events),
                "stream_health": {
                    "subscriber_count": len(self._subscribers),
                    "slow_subscriber_drops": self._slow_subscriber_drops,
                    "event_capacity": self.max_events,
                    "subscriber_queue_capacity": self.subscriber_queue_size,
                },
            }

    def _remember_event_id(self, event_id: str) -> None:
        self._seen_event_ids.append(event_id)
        self._seen_event_id_set.add(event_id)
        while len(self._seen_event_ids) > self._dedupe_capacity:
            expired = self._seen_event_ids.popleft()
            self._seen_event_id_set.discard(expired)

    def _fan_out(self, projected: dict[str, object]) -> None:
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(projected)
                continue
            except asyncio.QueueFull:
                pass

            try:
                queue.get_nowait()
                self._slow_subscriber_drops += 1
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(projected)
            except asyncio.QueueFull:
                self._slow_subscriber_drops += 1


mcp_activity_store = McpActivityStore(
    max_events=_bounded_env_int("CPTR_MCP_ACTIVITY_MAX_EVENTS", 250, 25, 2000),
    subscriber_queue_size=_bounded_env_int("CPTR_MCP_ACTIVITY_SUBSCRIBER_QUEUE", 64, 8, 512),
)
