"""Durable event journal for CPTR managed-memory recall and mutation provenance."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select

from cptr.models.memory_fabric import MemoryFabricEvent
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_sensitive, redact_text

_MAX_PAYLOAD_BYTES = 32_000
_MAX_TEXT_FIELD = 2_000


def _bounded_text(value: str | None, limit: int = _MAX_TEXT_FIELD) -> str | None:
    if value is None:
        return None
    text = redact_text(str(value))
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _bounded_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    value = redact_sensitive(payload) if isinstance(payload, dict) else {}
    try:
        encoded = json.dumps(value, separators=(",", ":"), default=str)
    except Exception:
        return {"truncated": True, "reason": "payload was not JSON serializable"}
    if len(encoded.encode("utf-8")) <= _MAX_PAYLOAD_BYTES:
        return value
    return {
        "truncated": True,
        "original_bytes": len(encoded.encode("utf-8")),
        "preview": encoded[:8_000],
    }


def _serialize(event: MemoryFabricEvent) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "workspace": event.workspace,
        "event_type": event.event_type,
        "scope": event.scope,
        "memory_id": event.memory_id,
        "path": event.path,
        "heading": event.heading,
        "reason": event.reason,
        "trust_level": event.trust_level,
        "confidence": max(0.0, min(1.0, int(event.confidence_ppm or 0) / 1_000_000)),
        "payload": event.payload if isinstance(event.payload, dict) else {},
        "created_at_ms": int(event.created_at_ms),
    }


class MemoryFabricStore:
    """Append/read owner-scoped immutable memory events."""

    def __init__(self, *, session_factory=None) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self):
        if self._session_factory is not None:
            async with self._session_factory() as db:
                yield db
            return
        async with await get_db() as db:
            yield db

    async def record_event(
        self,
        *,
        user_id: str,
        event_type: str,
        workspace: str = "",
        scope: str | None = None,
        memory_id: str | None = None,
        path: str | None = None,
        heading: str | None = None,
        reason: str | None = None,
        trust_level: str = "managed_memory",
        confidence_ppm: int = 1_000_000,
        payload: dict[str, Any] | None = None,
        created_at_ms: int | None = None,
    ) -> MemoryFabricEvent:
        event = MemoryFabricEvent(
            user_id=user_id,
            workspace=_bounded_text(workspace or None),
            event_type=_bounded_text(event_type, 120) or "unknown",
            scope=_bounded_text(scope, 64),
            memory_id=_bounded_text(memory_id, 240),
            path=_bounded_text(path, 1_000),
            heading=_bounded_text(heading, 500),
            reason=_bounded_text(reason, 1_000),
            trust_level=_bounded_text(trust_level, 120) or "managed_memory",
            confidence_ppm=max(0, min(1_000_000, int(confidence_ppm))),
            payload=_bounded_payload(payload),
            created_at_ms=int(created_at_ms if created_at_ms is not None else time.time() * 1000),
        )
        async with self._session() as db:
            db.add(event)
            await db.commit()
            await db.refresh(event)
        return event

    async def list_events(
        self,
        user_id: str,
        *,
        workspace: str | None = None,
        limit: int = 120,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        predicates = [MemoryFabricEvent.user_id == user_id]
        if workspace is not None:
            predicates.append(MemoryFabricEvent.workspace == (workspace or None))
        async with self._session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(MemoryFabricEvent)
                        .where(*predicates)
                        .order_by(
                            MemoryFabricEvent.created_at_ms.desc(), MemoryFabricEvent.id.desc()
                        )
                        .limit(safe_limit)
                    )
                ).all()
            )
        return [_serialize(row) for row in rows]


memory_fabric_store = MemoryFabricStore()
