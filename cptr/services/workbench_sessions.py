"""Durable owner-scoped Workbench Session registry and event timeline.

Workbench sessions are safe, listable observability records. They are distinct
from the short-lived browser tickets used by the ChatGPT Live Workbench.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from sqlalchemy import select

from cptr.models import WorkbenchSession, WorkbenchSessionEvent
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_external, redact_external_text

MAX_SESSION_NAME_CHARS = 120
MAX_EVENT_SUMMARY_CHARS = 4_000
MAX_EVENT_DETAIL_CHARS = 8_000
MAX_EVENT_DETAILS_KEYS = 32
MAX_EVENT_LIST_LIMIT = 200
DELETE_CONFIRMATION_TTL_MS = 5 * 60 * 1000

_ALLOWED_SOURCES = {"plugin", "cptr", "workbench", "system"}
_ALLOWED_ACTORS = {"chatgpt_plugin", "cptr_worker", "user", "system"}
_ALLOWED_TARGET_TYPES = {"task", "command", "monitor"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clip_text(value: object, limit: int) -> str:
    text = redact_external_text(str(value or "")).strip()
    return text[:limit]


def _safe_json(value: object) -> dict[str, Any]:
    """Return a small redacted JSON object suitable for browser replay."""
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    for key, item in list(value.items())[:MAX_EVENT_DETAILS_KEYS]:
        safe_key = str(key)[:120]
        safe_value = redact_external(item)
        if isinstance(safe_value, str):
            safe_value = safe_value[:MAX_EVENT_DETAIL_CHARS]
        elif isinstance(safe_value, (list, tuple)):
            safe_value = list(safe_value)[:32]
        elif isinstance(safe_value, dict):
            safe_value = dict(list(safe_value.items())[:32])
        output[safe_key] = safe_value
    return output


def _safe_name(value: str | None) -> str:
    cleaned = " ".join(_clip_text(value or "", MAX_SESSION_NAME_CHARS).split())
    return cleaned or "CPTR plugin session"


def _event_dict(event: WorkbenchSessionEvent) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "session_id": event.session_id,
        "sequence": int(event.sequence),
        "source": event.source,
        "actor": event.actor,
        "event_type": event.event_type,
        "state": event.state,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "workspace_id": event.workspace_id,
        "tool_name": event.tool_name,
        "summary": event.summary,
        "details": dict(event.details or {}),
        "metrics": dict(event.metrics or {}),
        "policy": dict(event.policy or {}),
        "created_at": int(event.created_at),
    }


def _session_dict(session: WorkbenchSession) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "name": session.name,
        "workspace_id": session.workspace_id,
        "status": session.status,
        "active_target_type": session.active_target_type,
        "active_target_id": session.active_target_id,
        "active_workspace_id": session.active_workspace_id,
        "event_count": int(session.event_count or 0),
        "created_at": int(session.created_at),
        "updated_at": int(session.updated_at),
        "last_event_at": int(session.last_event_at) if session.last_event_at else None,
        "archived_at": int(session.archived_at) if session.archived_at else None,
    }


@dataclass(frozen=True)
class WorkbenchSessionEventEnvelope:
    session_id: str
    sequence: int
    event: dict[str, Any]


class WorkbenchSessionHub:
    """Bounded owner-scoped in-memory fan-out for already persisted events."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[WorkbenchSessionEventEnvelope | None]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, owner_id: str, envelope: WorkbenchSessionEventEnvelope) -> None:
        async with self._lock:
            subscribers = list(self._subscribers.get(owner_id, set()))
        for queue in subscribers:
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # A slow browser must reconnect from its persisted cursor instead
                # of retaining unbounded server memory. Drain first so the close
                # sentinel cannot itself be dropped behind a full queue.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)

    async def subscribe(
        self, owner_id: str, *, queue_size: int = 128
    ) -> AsyncIterator[WorkbenchSessionEventEnvelope]:
        queue: asyncio.Queue[WorkbenchSessionEventEnvelope | None] = asyncio.Queue(
            maxsize=max(8, queue_size)
        )
        async with self._lock:
            self._subscribers.setdefault(owner_id, set()).add(queue)
        try:
            while True:
                envelope = await queue.get()
                if envelope is None:
                    return
                yield envelope
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(owner_id)
                if subscribers:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(owner_id, None)


class WorkbenchSessionStore:
    """Owner-scoped persistence API for CPTR plugin session records."""

    async def create(
        self, *, owner_id: str, name: str | None = None, workspace_id: str | None = None
    ) -> dict[str, Any]:
        now = _now_ms()
        async with await get_db() as db:
            session = WorkbenchSession(
                user_id=owner_id,
                name=_safe_name(name),
                workspace_id=workspace_id,
                status="OPEN",
                event_count=0,
                created_at=now,
                updated_at=now,
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)
            return _session_dict(session)

    async def get(self, *, owner_id: str, session_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
        async with await get_db() as db:
            query = select(WorkbenchSession).where(
                WorkbenchSession.id == session_id,
                WorkbenchSession.user_id == owner_id,
            )
            if not include_deleted:
                query = query.where(WorkbenchSession.deleted_at.is_(None))
            result = await db.execute(query)
            session = result.scalar_one_or_none()
            return _session_dict(session) if session else None

    async def list(
        self,
        *,
        owner_id: str,
        limit: int = 50,
        include_archived: bool = False,
        before_updated_at: int | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), MAX_EVENT_LIST_LIMIT))
        async with await get_db() as db:
            query = select(WorkbenchSession).where(
                WorkbenchSession.user_id == owner_id,
                WorkbenchSession.deleted_at.is_(None),
            )
            if not include_archived:
                query = query.where(WorkbenchSession.archived_at.is_(None))
            if before_updated_at is not None:
                query = query.where(WorkbenchSession.updated_at < int(before_updated_at))
            result = await db.execute(
                query.order_by(WorkbenchSession.updated_at.desc()).limit(safe_limit)
            )
            return [_session_dict(row) for row in result.scalars().all()]

    async def append_event(
        self,
        *,
        owner_id: str,
        session_id: str,
        source: str,
        actor: str,
        event_type: str,
        summary: str,
        state: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        workspace_id: str | None = None,
        tool_name: str | None = None,
        details: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
    ) -> WorkbenchSessionEventEnvelope | None:
        if source not in _ALLOWED_SOURCES or actor not in _ALLOWED_ACTORS:
            raise ValueError("invalid workbench session event source or actor")
        if target_type is not None and target_type not in _ALLOWED_TARGET_TYPES:
            raise ValueError("invalid workbench session target type")
        if not event_type or len(event_type) > 120:
            raise ValueError("invalid workbench session event type")
        now = _now_ms()
        async with await get_db() as db:
            result = await db.execute(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            session = result.scalar_one_or_none()
            if session is None:
                return None
            sequence = int(session.event_count or 0) + 1
            event = WorkbenchSessionEvent(
                session_id=session.id,
                user_id=owner_id,
                sequence=sequence,
                source=source,
                actor=actor,
                event_type=event_type,
                state=_clip_text(state, 80) if state else None,
                target_type=target_type,
                target_id=_clip_text(target_id, 200) if target_id else None,
                workspace_id=_clip_text(workspace_id, 200) if workspace_id else None,
                tool_name=_clip_text(tool_name, 160) if tool_name else None,
                summary=_clip_text(summary, MAX_EVENT_SUMMARY_CHARS),
                details=_safe_json(details or {}),
                metrics=_safe_json(metrics or {}),
                policy=_safe_json(policy or {}),
                created_at=now,
            )
            db.add(event)
            session.event_count = sequence
            session.updated_at = now
            session.last_event_at = now
            if target_type and target_id:
                session.active_target_type = target_type
                session.active_target_id = _clip_text(target_id, 200)
                session.active_workspace_id = _clip_text(workspace_id, 200) if workspace_id else None
            if state in {"RUNNING", "WAITING_APPROVAL", "REVIEW_REQUIRED", "COMPLETE", "FAILED", "CANCELLED"}:
                session.status = state
            await db.commit()
            await db.refresh(event)
            return WorkbenchSessionEventEnvelope(session.id, sequence, _event_dict(event))

    async def events(
        self, *, owner_id: str, session_id: str, after_sequence: int = 0, limit: int = 100
    ) -> list[dict[str, Any]] | None:
        safe_limit = max(1, min(int(limit), MAX_EVENT_LIST_LIMIT))
        async with await get_db() as db:
            session = await db.scalar(
                select(WorkbenchSession.id).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            if session is None:
                return None
            result = await db.execute(
                select(WorkbenchSessionEvent)
                .where(
                    WorkbenchSessionEvent.session_id == session_id,
                    WorkbenchSessionEvent.user_id == owner_id,
                    WorkbenchSessionEvent.sequence > max(0, int(after_sequence)),
                )
                .order_by(WorkbenchSessionEvent.sequence.asc())
                .limit(safe_limit)
            )
            return [_event_dict(row) for row in result.scalars().all()]

    async def bind_target(
        self,
        *,
        owner_id: str,
        session_id: str,
        target_type: str,
        target_id: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        if target_type not in _ALLOWED_TARGET_TYPES or not target_id.strip():
            raise ValueError("invalid workbench session target")
        now = _now_ms()
        async with await get_db() as db:
            result = await db.execute(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            session = result.scalar_one_or_none()
            if session is None:
                return None
            session.active_target_type = target_type
            session.active_target_id = _clip_text(target_id, 200)
            session.active_workspace_id = _clip_text(workspace_id, 200) if workspace_id else None
            session.status = "RUNNING" if session.status == "OPEN" else session.status
            session.updated_at = now
            await db.commit()
            await db.refresh(session)
            return _session_dict(session)

    async def rename(self, *, owner_id: str, session_id: str, name: str) -> dict[str, Any] | None:
        now = _now_ms()
        async with await get_db() as db:
            result = await db.execute(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            session = result.scalar_one_or_none()
            if session is None:
                return None
            session.name = _safe_name(name)
            session.updated_at = now
            await db.commit()
            await db.refresh(session)
            return _session_dict(session)

    async def archive(self, *, owner_id: str, session_id: str) -> dict[str, Any] | None:
        now = _now_ms()
        async with await get_db() as db:
            result = await db.execute(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            session = result.scalar_one_or_none()
            if session is None:
                return None
            session.status = "ARCHIVED"
            session.archived_at = now
            session.updated_at = now
            await db.commit()
            await db.refresh(session)
            return _session_dict(session)

    async def request_delete(self, *, owner_id: str, session_id: str) -> dict[str, Any] | None:
        now = _now_ms()
        confirmation = secrets.token_urlsafe(24)
        confirmation_hash = hashlib.sha256(confirmation.encode()).hexdigest()
        async with await get_db() as db:
            result = await db.execute(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            session = result.scalar_one_or_none()
            if session is None:
                return None
            session.delete_requested_at = now
            session.delete_confirmation_hash = confirmation_hash
            session.delete_confirmation_expires_at = now + DELETE_CONFIRMATION_TTL_MS
            session.updated_at = now
            await db.commit()
            return {
                "session_id": session.id,
                "confirmation_id": confirmation,
                "expires_at": session.delete_confirmation_expires_at,
                "event_count": int(session.event_count or 0),
                "impact": "Deletes this Plugin session and its redacted UI events only; linked CPTR tasks, commands, workspaces, and control audit records remain unchanged.",
            }

    async def confirm_delete(
        self, *, owner_id: str, confirmation_id: str
    ) -> dict[str, Any] | None:
        now = _now_ms()
        confirmation_hash = hashlib.sha256(confirmation_id.encode()).hexdigest()
        async with await get_db() as db:
            result = await db.execute(
                select(WorkbenchSession).where(
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                    WorkbenchSession.delete_confirmation_hash == confirmation_hash,
                    WorkbenchSession.delete_confirmation_expires_at.is_not(None),
                    WorkbenchSession.delete_confirmation_expires_at >= now,
                )
            )
            session = result.scalar_one_or_none()
            if session is None:
                return None
            session.status = "DELETED"
            session.deleted_at = now
            session.updated_at = now
            session.delete_confirmation_hash = None
            session.delete_confirmation_expires_at = None
            await db.commit()
            return {"session_id": session.id, "status": "DELETED", "deleted_at": now}


workbench_session_store = WorkbenchSessionStore()
workbench_session_hub = WorkbenchSessionHub()


async def publish_workbench_session_event(**kwargs: Any) -> WorkbenchSessionEventEnvelope | None:
    """Persist then fan out a safe session event without losing replayability."""
    envelope = await workbench_session_store.append_event(**kwargs)
    if envelope is not None:
        await workbench_session_hub.publish(kwargs["owner_id"], envelope)
    return envelope
