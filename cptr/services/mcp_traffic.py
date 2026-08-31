"""Bounded in-memory MCP traffic telemetry state.

The traffic subsystem records only an allowlisted operational envelope. It is
intentionally non-durable and never stores request payloads, tool arguments,
results, headers, credentials, prompts, or arbitrary exception messages.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class McpTrafficClient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=80)
    version: str | None = Field(default=None, max_length=64)


class McpTrafficEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    event_id: str = Field(min_length=8, max_length=128)
    sequence: int = Field(ge=1)
    event_type: Literal[
        "session_opened",
        "session_closed",
        "request_started",
        "request_finished",
        "request_failed",
        "tool_started",
        "tool_finished",
        "tool_failed",
    ]
    timestamp_ms: int = Field(ge=0)
    session_id: str | None = Field(default=None, max_length=128)
    client: McpTrafficClient
    request_id: str | None = Field(default=None, max_length=128)
    method: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=256)
    status: Literal["started", "complete", "error", "connected", "disconnected"]
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    request_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    response_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    error_code: (
        Literal[
            "timeout",
            "validation_error",
            "unauthorized",
            "tool_error",
            "transport_error",
            "internal_error",
        ]
        | None
    ) = None


class McpTrafficBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[McpTrafficEvent] = Field(min_length=1, max_length=100)


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


class McpTrafficStore:
    """Bounded mutable topology state protected by one asyncio lock."""

    def __init__(
        self,
        *,
        max_events: int = 500,
        max_sessions: int = 128,
        subscriber_queue_size: int = 128,
        session_ttl_seconds: int = 300,
    ) -> None:
        self.max_events = max(1, int(max_events))
        self.max_sessions = max(1, int(max_sessions))
        self.subscriber_queue_size = max(1, int(subscriber_queue_size))
        self.session_ttl_ms = max(1, int(session_ttl_seconds)) * 1000
        self.max_clients = max(8, min(self.max_events, self.max_sessions * 2))
        self._dedupe_capacity = max(32, self.max_events * 4)

        self._lock = asyncio.Lock()
        self._events: deque[dict[str, object]] = deque(maxlen=self.max_events)
        self._seen_event_ids: deque[str] = deque()
        self._seen_event_id_set: set[str] = set()
        self._sessions: dict[str, dict[str, object]] = {}
        self._clients: dict[str, dict[str, object]] = {}
        self._active_requests: dict[str, dict[str, object]] = {}
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self._ingestion_sequence = 0
        self._slow_subscriber_drops = 0
        self._session_evictions = 0
        self._expired_sessions = 0

    def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=self.subscriber_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        self._subscribers.discard(queue)

    async def ingest(self, events: list[McpTrafficEvent]) -> dict[str, int]:
        accepted = 0
        duplicates = 0
        dropped = 0
        async with self._lock:
            for event in events:
                if event.event_id in self._seen_event_id_set:
                    duplicates += 1
                    continue

                self._remember_event_id(event.event_id)
                self._ingestion_sequence += 1
                projected = event.model_dump(mode="json")
                projected["ingestion_sequence"] = self._ingestion_sequence
                self._events.append(projected)
                self._apply_event(event)
                self._fan_out(projected)
                accepted += 1

            self._prune_clients()
        return {"accepted": accepted, "duplicates": duplicates, "dropped": dropped}

    async def snapshot(self) -> dict[str, object]:
        async with self._lock:
            clients = []
            for client_id in sorted(self._clients):
                client = self._clients[client_id]
                clients.append(
                    {
                        "id": client_id,
                        "label": client["label"],
                        "version": client["version"],
                        "active_sessions": self._active_session_count(client_id),
                        "active_requests": self._active_request_count(client_id),
                        "total_requests": client["total_requests"],
                        "errors": client["errors"],
                        "last_seen": client["last_seen"],
                        "last_tool": client["last_tool"],
                    }
                )

            sessions = [
                {
                    "session_id": session_id,
                    "client_id": session["client_id"],
                    "connected_at": session["connected_at"],
                    "last_seen": session["last_seen"],
                }
                for session_id, session in sorted(self._sessions.items())
            ]

            return {
                "version": 1,
                "sequence": self._ingestion_sequence,
                "center": {"id": "cptr-mcp", "label": "CPTR MCP", "status": "online"},
                "clients": clients,
                "sessions": sessions,
                "events": list(self._events),
                "stream_health": {
                    "subscriber_count": len(self._subscribers),
                    "slow_subscriber_drops": self._slow_subscriber_drops,
                    "session_evictions": self._session_evictions,
                    "expired_sessions": self._expired_sessions,
                    "event_capacity": self.max_events,
                    "session_capacity": self.max_sessions,
                },
            }

    async def expire_stale_sessions(self, now_ms: int | None = None) -> int:
        current = int(time.time() * 1000) if now_ms is None else int(now_ms)
        async with self._lock:
            stale = [
                session_id
                for session_id, session in self._sessions.items()
                if current - int(session["last_seen"]) > self.session_ttl_ms
            ]
            for session_id in stale:
                self._sessions.pop(session_id, None)
            self._expired_sessions += len(stale)
            self._prune_clients()
            return len(stale)

    def _remember_event_id(self, event_id: str) -> None:
        self._seen_event_ids.append(event_id)
        self._seen_event_id_set.add(event_id)
        while len(self._seen_event_ids) > self._dedupe_capacity:
            expired = self._seen_event_ids.popleft()
            self._seen_event_id_set.discard(expired)

    def _touch_client(self, event: McpTrafficEvent) -> dict[str, object]:
        client_id = event.client.id
        state = self._clients.get(client_id)
        if state is None:
            state = {
                "label": event.client.label,
                "version": event.client.version,
                "total_requests": 0,
                "errors": 0,
                "last_seen": event.timestamp_ms,
                "last_tool": None,
            }
            self._clients[client_id] = state
        else:
            state["label"] = event.client.label
            state["version"] = event.client.version
            state["last_seen"] = max(int(state["last_seen"]), event.timestamp_ms)
        if event.tool_name:
            state["last_tool"] = event.tool_name
        return state

    def _apply_event(self, event: McpTrafficEvent) -> None:
        client = self._touch_client(event)
        if event.session_id and event.session_id in self._sessions:
            self._sessions[event.session_id]["last_seen"] = max(
                int(self._sessions[event.session_id]["last_seen"]), event.timestamp_ms
            )

        if event.event_type == "session_opened" and event.session_id:
            if event.session_id not in self._sessions and len(self._sessions) >= self.max_sessions:
                oldest = min(
                    self._sessions.items(),
                    key=lambda item: (int(item[1]["last_seen"]), item[0]),
                )[0]
                self._sessions.pop(oldest, None)
                self._session_evictions += 1
            self._sessions[event.session_id] = {
                "client_id": event.client.id,
                "connected_at": event.timestamp_ms,
                "last_seen": event.timestamp_ms,
            }
        elif event.event_type == "session_closed" and event.session_id:
            self._sessions.pop(event.session_id, None)
        elif event.event_type == "request_started" and event.request_id:
            self._active_requests[event.request_id] = {
                "client_id": event.client.id,
                "started_at": event.timestamp_ms,
                "tool_name": event.tool_name,
            }
        elif event.event_type in {"request_finished", "request_failed"}:
            if event.request_id:
                self._active_requests.pop(event.request_id, None)
            client["total_requests"] = int(client["total_requests"]) + 1
            if event.event_type == "request_failed":
                client["errors"] = int(client["errors"]) + 1

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

    def _active_session_count(self, client_id: str) -> int:
        return sum(1 for session in self._sessions.values() if session["client_id"] == client_id)

    def _active_request_count(self, client_id: str) -> int:
        return sum(
            1 for request in self._active_requests.values() if request["client_id"] == client_id
        )

    def _prune_clients(self) -> None:
        if len(self._clients) <= self.max_clients:
            return
        protected = {str(session["client_id"]) for session in self._sessions.values()} | {
            str(request["client_id"]) for request in self._active_requests.values()
        }
        removable = sorted(
            (
                (client_id, int(state["last_seen"]))
                for client_id, state in self._clients.items()
                if client_id not in protected
            ),
            key=lambda item: (item[1], item[0]),
        )
        for client_id, _ in removable:
            if len(self._clients) <= self.max_clients:
                break
            self._clients.pop(client_id, None)


mcp_traffic_store = McpTrafficStore(
    max_events=_bounded_env_int("CPTR_MCP_TRAFFIC_MAX_EVENTS", 500, 10, 10_000),
    max_sessions=_bounded_env_int("CPTR_MCP_TRAFFIC_MAX_SESSIONS", 128, 1, 2_000),
    subscriber_queue_size=_bounded_env_int("CPTR_MCP_TRAFFIC_SUBSCRIBER_QUEUE_SIZE", 128, 1, 2_000),
    session_ttl_seconds=_bounded_env_int("CPTR_MCP_TRAFFIC_SESSION_TTL_SECONDS", 300, 10, 86_400),
)
