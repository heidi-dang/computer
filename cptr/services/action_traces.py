"""Bounded owner-scoped lifecycle traces for MCP actions.

The trace store is deliberately operational metadata only.  It links the
existing MCP correlation ID to backend-owned lifecycle entities without storing
request/tool payloads, command text, terminal bytes, URLs, browser frames,
headers, or arbitrary exception messages.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Literal

TraceLayer = Literal["chatgpt", "mcp", "backend", "command", "workbench", "browser", "cleanup"]
TraceStatus = Literal["started", "running", "ok", "error", "cancelled"]
EntityType = Literal["command", "workbench", "browser"]

_LAYER_ORDER: tuple[TraceLayer, ...] = (
    "chatgpt",
    "mcp",
    "backend",
    "command",
    "workbench",
    "browser",
    "cleanup",
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")

TRACE_ID_HEADER = "x-cptr-trace-id"
REQUEST_ID_HEADER = "x-cptr-request-id"
MCP_SESSION_ID_HEADER = "x-cptr-mcp-session-id"
TOOL_NAME_HEADER = "x-cptr-tool-name"


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _safe_id(value: object | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_ID_RE.fullmatch(candidate) else None


def _safe_tool(value: object | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_TOOL_RE.fullmatch(candidate) else None


@dataclass(frozen=True, slots=True)
class ActionTraceContext:
    trace_id: str
    request_id: str | None = None
    mcp_session_id: str | None = None
    tool_name: str | None = None


def trace_context_from_request(request: Any) -> ActionTraceContext | None:
    """Read only the four bounded operational trace headers from a request-like object."""
    headers = getattr(request, "headers", None)
    if headers is None:
        return None

    def header(name: str) -> object | None:
        try:
            return headers.get(name)
        except Exception:
            return None

    trace_id = _safe_id(header(TRACE_ID_HEADER))
    if trace_id is None:
        return None
    return ActionTraceContext(
        trace_id=trace_id,
        request_id=_safe_id(header(REQUEST_ID_HEADER)),
        mcp_session_id=_safe_id(header(MCP_SESSION_ID_HEADER)),
        tool_name=_safe_tool(header(TOOL_NAME_HEADER)),
    )


class _TraceRecord:
    __slots__ = (
        "owner_id",
        "trace_id",
        "request_id",
        "mcp_session_id",
        "tool_name",
        "task_ids",
        "created_at_ms",
        "updated_at_ms",
        "stages",
        "entities",
        "dedupe_order",
        "dedupe_set",
        "next_stage_sequence",
    )

    def __init__(
        self,
        *,
        owner_id: str,
        trace_id: str,
        max_stages: int,
        request_id: str | None = None,
        mcp_session_id: str | None = None,
        tool_name: str | None = None,
        now_ms: int,
    ) -> None:
        self.owner_id = owner_id
        self.trace_id = trace_id
        self.request_id = request_id
        self.mcp_session_id = mcp_session_id
        self.tool_name = tool_name
        self.task_ids: set[str] = set()
        self.created_at_ms = now_ms
        self.updated_at_ms = now_ms
        self.stages: deque[dict[str, object]] = deque(maxlen=max_stages)
        self.entities: dict[EntityType, set[str]] = {
            "command": set(),
            "workbench": set(),
            "browser": set(),
        }
        self.dedupe_order: deque[str] = deque()
        self.dedupe_set: set[str] = set()
        self.next_stage_sequence = 0


class ActionTraceStore:
    """Globally bounded, owner-isolated trace ring with bounded entity indexes."""

    def __init__(
        self,
        *,
        max_traces: int = 256,
        max_stages_per_trace: int = 48,
        max_dedupe_keys_per_trace: int = 96,
    ) -> None:
        self.max_traces = max(1, int(max_traces))
        self.max_stages_per_trace = max(1, int(max_stages_per_trace))
        self.max_dedupe_keys_per_trace = max(4, int(max_dedupe_keys_per_trace))
        self._traces: OrderedDict[tuple[str, str], _TraceRecord] = OrderedDict()
        self._entities: dict[tuple[str, EntityType, str], str] = {}
        self._owner_sequences: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _bump_owner(self, owner_id: str) -> None:
        self._owner_sequences[owner_id] = self._owner_sequences.get(owner_id, 0) + 1

    def _evict_if_needed(self) -> None:
        while len(self._traces) > self.max_traces:
            (owner_id, trace_id), record = self._traces.popitem(last=False)
            for entity_type, entity_ids in record.entities.items():
                for entity_id in entity_ids:
                    key = (owner_id, entity_type, entity_id)
                    if self._entities.get(key) == trace_id:
                        self._entities.pop(key, None)
            self._bump_owner(owner_id)

    def _ensure_trace(
        self,
        *,
        owner_id: str,
        trace_id: str,
        request_id: str | None = None,
        mcp_session_id: str | None = None,
        tool_name: str | None = None,
        now_ms: int | None = None,
    ) -> _TraceRecord | None:
        safe_trace_id = _safe_id(trace_id)
        safe_owner_id = _safe_id(owner_id)
        if safe_trace_id is None or safe_owner_id is None:
            return None
        key = (safe_owner_id, safe_trace_id)
        record = self._traces.get(key)
        current = self._now_ms() if now_ms is None else max(0, int(now_ms))
        if record is None:
            record = _TraceRecord(
                owner_id=safe_owner_id,
                trace_id=safe_trace_id,
                max_stages=self.max_stages_per_trace,
                request_id=_safe_id(request_id),
                mcp_session_id=_safe_id(mcp_session_id),
                tool_name=_safe_tool(tool_name),
                now_ms=current,
            )
            self._traces[key] = record
            self._bump_owner(safe_owner_id)
            self._evict_if_needed()
        else:
            if record.request_id is None:
                record.request_id = _safe_id(request_id)
            if record.mcp_session_id is None:
                record.mcp_session_id = _safe_id(mcp_session_id)
            if record.tool_name is None:
                record.tool_name = _safe_tool(tool_name)
        return record

    def _remember_dedupe(self, record: _TraceRecord, dedupe_key: str | None) -> bool:
        if not dedupe_key:
            return True
        bounded = str(dedupe_key)[:160]
        if bounded in record.dedupe_set:
            return False
        record.dedupe_order.append(bounded)
        record.dedupe_set.add(bounded)
        while len(record.dedupe_order) > self.max_dedupe_keys_per_trace:
            expired = record.dedupe_order.popleft()
            record.dedupe_set.discard(expired)
        return True

    async def append(
        self,
        *,
        owner_id: str,
        trace_id: str,
        layer: TraceLayer,
        name: str,
        status: TraceStatus,
        timestamp_ms: int | None = None,
        duration_ms: int | None = None,
        request_id: str | None = None,
        mcp_session_id: str | None = None,
        tool_name: str | None = None,
        workspace_id: str | None = None,
        task_id: str | None = None,
        entity_type: EntityType | None = None,
        entity_id: str | None = None,
        error_code: str | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        if layer not in _LAYER_ORDER or status not in {
            "started",
            "running",
            "ok",
            "error",
            "cancelled",
        }:
            return False
        safe_name = _safe_tool(name)
        if safe_name is None:
            return False
        current = self._now_ms() if timestamp_ms is None else max(0, int(timestamp_ms))
        async with self._lock:
            record = self._ensure_trace(
                owner_id=owner_id,
                trace_id=trace_id,
                request_id=request_id,
                mcp_session_id=mcp_session_id,
                tool_name=tool_name,
                now_ms=current,
            )
            if record is None or not self._remember_dedupe(record, dedupe_key):
                return False
            safe_entity_id = _safe_id(entity_id)
            if entity_type is not None and safe_entity_id is not None:
                entity_key = (record.owner_id, entity_type, safe_entity_id)
                linked_trace = self._entities.get(entity_key)
                if linked_trace is None:
                    self._entities[entity_key] = record.trace_id
                    record.entities[entity_type].add(safe_entity_id)
                elif linked_trace == record.trace_id:
                    record.entities[entity_type].add(safe_entity_id)
            record.next_stage_sequence += 1
            stage: dict[str, object] = {
                "sequence": record.next_stage_sequence,
                "timestamp_ms": current,
                "layer": layer,
                "name": safe_name,
                "status": status,
            }
            bounded_duration = int(duration_ms) if duration_ms is not None else None
            if bounded_duration is not None and 0 <= bounded_duration <= 86_400_000:
                stage["duration_ms"] = bounded_duration
            safe_request_id = _safe_id(request_id) or record.request_id
            safe_tool_name = _safe_tool(tool_name) or record.tool_name
            safe_workspace_id = _safe_id(workspace_id)
            safe_task_id = _safe_id(task_id)
            safe_error_code = _safe_tool(error_code)
            if safe_request_id:
                stage["request_id"] = safe_request_id
            if safe_tool_name:
                stage["tool_name"] = safe_tool_name
            if safe_workspace_id:
                stage["workspace_id"] = safe_workspace_id
            if safe_task_id:
                stage["task_id"] = safe_task_id
                record.task_ids.add(safe_task_id)
            if entity_type is not None and safe_entity_id is not None:
                stage["entity_type"] = entity_type
                stage["entity_id"] = safe_entity_id
            if safe_error_code:
                stage["error_code"] = safe_error_code
            record.stages.append(stage)
            record.created_at_ms = min(record.created_at_ms, current)
            record.updated_at_ms = max(record.updated_at_ms, current)
            key = (record.owner_id, record.trace_id)
            self._traces.move_to_end(key)
            self._bump_owner(record.owner_id)
            return True

    async def link_entity(
        self,
        *,
        owner_id: str,
        trace_id: str,
        entity_type: EntityType,
        entity_id: str,
    ) -> bool:
        safe_entity_id = _safe_id(entity_id)
        if safe_entity_id is None:
            return False
        async with self._lock:
            record = self._ensure_trace(owner_id=owner_id, trace_id=trace_id)
            if record is None:
                return False
            key = (record.owner_id, entity_type, safe_entity_id)
            existing = self._entities.get(key)
            if existing is not None and existing != record.trace_id:
                return False
            changed = existing is None
            self._entities[key] = record.trace_id
            record.entities[entity_type].add(safe_entity_id)
            if changed:
                record.updated_at_ms = max(record.updated_at_ms, self._now_ms())
                self._traces.move_to_end((record.owner_id, record.trace_id))
                self._bump_owner(record.owner_id)
            return True

    async def resolve_entity(
        self, *, owner_id: str, entity_type: EntityType, entity_id: str
    ) -> str | None:
        safe_owner_id = _safe_id(owner_id)
        safe_entity_id = _safe_id(entity_id)
        if safe_owner_id is None or safe_entity_id is None:
            return None
        async with self._lock:
            return self._entities.get((safe_owner_id, entity_type, safe_entity_id))

    async def append_for_entity(
        self,
        *,
        owner_id: str,
        entity_type: EntityType,
        entity_id: str,
        layer: TraceLayer,
        name: str,
        status: TraceStatus,
        timestamp_ms: int | None = None,
        duration_ms: int | None = None,
        workspace_id: str | None = None,
        error_code: str | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        trace_id = await self.resolve_entity(
            owner_id=owner_id, entity_type=entity_type, entity_id=entity_id
        )
        if trace_id is None:
            return False
        return await self.append(
            owner_id=owner_id,
            trace_id=trace_id,
            layer=layer,
            name=name,
            status=status,
            timestamp_ms=timestamp_ms,
            duration_ms=duration_ms,
            workspace_id=workspace_id,
            entity_type=entity_type,
            entity_id=entity_id,
            error_code=error_code,
            dedupe_key=dedupe_key,
        )

    @staticmethod
    def _layers(record: _TraceRecord) -> list[str]:
        present = {str(stage.get("layer")) for stage in record.stages}
        for entity_type, entity_ids in record.entities.items():
            if entity_ids:
                present.add(entity_type)
        return [layer for layer in _LAYER_ORDER if layer in present]

    @staticmethod
    def _summary(record: _TraceRecord) -> dict[str, object]:
        ordered_stages = sorted(
            record.stages,
            key=lambda stage: (
                int(stage.get("timestamp_ms") or 0),
                int(stage.get("sequence") or 0),
            ),
        )
        status = "running"
        error_code: str | None = None
        capability_stage: dict[str, object] | None = None
        if ordered_stages:
            statuses = [str(stage.get("status") or "") for stage in ordered_stages]
            if "error" in statuses:
                status = "error"
            elif "cancelled" in statuses:
                status = "cancelled"
            elif statuses[-1] == "ok":
                status = "ok"
            for stage in reversed(ordered_stages):
                name = stage.get("name")
                if (
                    capability_stage is None
                    and isinstance(name, str)
                    and name.startswith("capability_os.")
                ):
                    capability_stage = stage
                value = stage.get("error_code")
                if error_code is None and isinstance(value, str) and value:
                    error_code = value
                if capability_stage is not None and error_code is not None:
                    break
        # Trace-level timestamps remain authoritative even when the bounded stage
        # deque evicts early detail. This preserves true end-to-end duration while
        # keeping retained stage memory strictly bounded.
        first_ms = min(
            record.created_at_ms,
            min(
                (
                    int(stage.get("timestamp_ms") or record.created_at_ms)
                    for stage in ordered_stages
                ),
                default=record.created_at_ms,
            ),
        )
        last_ms = max(
            record.updated_at_ms,
            max(
                (
                    int(stage.get("timestamp_ms") or record.updated_at_ms)
                    for stage in ordered_stages
                ),
                default=record.updated_at_ms,
            ),
        )
        return {
            "trace_id": record.trace_id,
            "request_id": record.request_id,
            "mcp_session_id": record.mcp_session_id,
            "tool_name": record.tool_name,
            "task_ids": sorted(record.task_ids),
            "status": status,
            "started_at_ms": first_ms,
            "updated_at_ms": last_ms,
            "duration_ms": max(0, last_ms - first_ms),
            "stage_count": len(record.stages),
            "layers": ActionTraceStore._layers(record),
            "error_code": error_code,
            "capability_action": (
                str(capability_stage.get("name"))[len("capability_os.") :]
                if capability_stage is not None
                else None
            ),
            "capability_status": (
                str(capability_stage.get("status")) if capability_stage is not None else None
            ),
            "capability_updated_at_ms": (
                int(capability_stage.get("timestamp_ms") or 0)
                if capability_stage is not None
                else None
            ),
            "entities": {
                entity_type: sorted(entity_ids)
                for entity_type, entity_ids in record.entities.items()
                if entity_ids
            },
        }

    async def observe_mcp_traffic(self, *, owner_id: str, events: list[Any]) -> int:
        """Project existing MCP traffic lifecycle events into traces without payloads."""
        accepted = 0
        mapping: dict[str, tuple[TraceLayer, str, TraceStatus]] = {
            "request_started": ("chatgpt", "chatgpt.request.dispatched", "started"),
            "request_finished": ("mcp", "mcp.request.completed", "ok"),
            "request_failed": ("mcp", "mcp.request.failed", "error"),
            "tool_started": ("mcp", "mcp.tool.started", "started"),
            "tool_finished": ("mcp", "mcp.tool.completed", "ok"),
            "tool_failed": ("mcp", "mcp.tool.failed", "error"),
        }
        for event in events:
            trace_id = _safe_id(getattr(event, "correlation_id", None))
            event_type = str(getattr(event, "event_type", "") or "")
            projected = mapping.get(event_type)
            if trace_id is None or projected is None:
                continue
            layer, name, status = projected
            event_id = _safe_id(getattr(event, "event_id", None))
            if await self.append(
                owner_id=owner_id,
                trace_id=trace_id,
                layer=layer,
                name=name,
                status=status,
                timestamp_ms=getattr(event, "timestamp_ms", None),
                duration_ms=getattr(event, "duration_ms", None),
                request_id=getattr(event, "request_id", None),
                mcp_session_id=getattr(event, "session_id", None),
                tool_name=getattr(event, "tool_name", None),
                error_code=getattr(event, "error_code", None),
                dedupe_key=f"traffic:{event_id}" if event_id else None,
            ):
                accepted += 1
        return accepted

    async def observe_mcp_diagnostics(self, *, owner_id: str, events: list[Any]) -> int:
        """Project measured backend RTT/failures into the same correlation trace."""
        accepted = 0
        for event in events:
            trace_id = _safe_id(getattr(event, "correlation_id", None))
            if trace_id is None:
                continue
            kind = str(getattr(event, "kind", "") or "")
            event_id = _safe_id(
                getattr(event, "event_id", None) or getattr(event, "diagnostic_id", None)
            )
            if (
                kind == "latency"
                and str(getattr(event, "edge_id", "") or "") == "cptr-mcp-cptr-backend"
            ):
                if await self.append(
                    owner_id=owner_id,
                    trace_id=trace_id,
                    layer="backend",
                    name="backend.request",
                    status="error" if getattr(event, "status", "ok") == "error" else "ok",
                    timestamp_ms=getattr(event, "timestamp_ms", None),
                    duration_ms=getattr(event, "duration_ms", None),
                    request_id=getattr(event, "request_id", None),
                    tool_name=getattr(event, "tool_name", None),
                    dedupe_key=f"diagnostics:{event_id}" if event_id else None,
                ):
                    accepted += 1
            elif kind == "failure" and str(getattr(event, "stage", "") or "") == "cptr_backend":
                if await self.append(
                    owner_id=owner_id,
                    trace_id=trace_id,
                    layer="backend",
                    name="backend.failure",
                    status="error",
                    timestamp_ms=getattr(event, "completed_at_ms", None),
                    duration_ms=getattr(event, "duration_ms", None),
                    request_id=getattr(event, "request_id", None),
                    mcp_session_id=getattr(event, "session_id", None),
                    tool_name=getattr(event, "tool_name", None),
                    error_code=getattr(event, "error_code", None),
                    dedupe_key=f"diagnostics:{event_id}" if event_id else None,
                ):
                    accepted += 1
        return accepted

    async def summaries(
        self,
        *,
        owner_id: str,
        limit: int = 20,
        task_id: str | None = None,
    ) -> dict[str, object]:
        safe_owner_id = _safe_id(owner_id)
        safe_task_id = _safe_id(task_id) if task_id is not None else None
        if safe_owner_id is None or (task_id is not None and safe_task_id is None):
            return {"version": 1, "sequence": 0, "traces": []}
        bounded_limit = max(1, min(int(limit), 50))
        async with self._lock:
            records = [
                record
                for (record_owner, _), record in reversed(self._traces.items())
                if record_owner == safe_owner_id
                and (safe_task_id is None or safe_task_id in record.task_ids)
            ][:bounded_limit]
            return {
                "version": 1,
                "sequence": self._owner_sequences.get(safe_owner_id, 0),
                "traces": [self._summary(record) for record in records],
            }

    async def get(self, *, owner_id: str, trace_id: str) -> dict[str, object] | None:
        safe_owner_id = _safe_id(owner_id)
        safe_trace_id = _safe_id(trace_id)
        if safe_owner_id is None or safe_trace_id is None:
            return None
        async with self._lock:
            record = self._traces.get((safe_owner_id, safe_trace_id))
            if record is None:
                return None
            summary = self._summary(record)
            stages = sorted(
                (dict(stage) for stage in record.stages),
                key=lambda stage: (
                    int(stage.get("timestamp_ms") or 0),
                    int(stage.get("sequence") or 0),
                ),
            )
            return {**summary, "version": 1, "stages": stages}


action_trace_store = ActionTraceStore(
    max_traces=_bounded_env_int("CPTR_ACTION_TRACE_MAX_TRACES", 256, 32, 2000),
    max_stages_per_trace=_bounded_env_int("CPTR_ACTION_TRACE_MAX_STAGES", 48, 8, 200),
    max_dedupe_keys_per_trace=_bounded_env_int("CPTR_ACTION_TRACE_MAX_DEDUPE", 96, 16, 400),
)


__all__ = [
    "ActionTraceContext",
    "ActionTraceStore",
    "action_trace_store",
    "trace_context_from_request",
    "TRACE_ID_HEADER",
    "REQUEST_ID_HEADER",
    "MCP_SESSION_ID_HEADER",
    "TOOL_NAME_HEADER",
]
