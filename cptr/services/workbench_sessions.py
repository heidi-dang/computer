"""Durable owner-scoped Workbench Session persistence.

Workbench sessions are safe observability records used by the ChatGPT plugin.
They never contain private prompts or reasoning and are distinct from task,
command, and autonomous execution state.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from typing import Any

from sqlalchemy import delete, select

from cptr.models import WorkbenchSession, WorkbenchSessionEvent
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_external, redact_external_text

MAX_SESSION_NAME_CHARS = 120
MAX_EVENT_SUMMARY_CHARS = 4_000
MAX_EVENT_DETAIL_CHARS = 8_000
MAX_EVENT_DETAILS_KEYS = 32
MAX_EVENT_LIST_LIMIT = 200
DELETE_CONFIRMATION_TTL_MS = 5 * 60 * 1000
_ALLOWED_TARGET_TYPES = {"task", "command", "monitor"}
_ALLOWED_STATES = {
    "OPEN",
    "RUNNING",
    "WAITING_APPROVAL",
    "REVIEW_REQUIRED",
    "COMPLETE",
    "FAILED",
    "CANCELLED",
    "ARCHIVED",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clip(value: object, limit: int) -> str:
    return redact_external_text(str(value or "")).strip()[:limit]


def _safe_name(value: str | None) -> str:
    cleaned = " ".join(_clip(value or "", MAX_SESSION_NAME_CHARS).split())
    return cleaned or "CPTR Workbench Session"


def _safe_json(value: object) -> dict[str, Any]:
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
        "last_event_at": int(session.last_event_at) if session.last_event_at is not None else None,
        "archived_at": int(session.archived_at) if session.archived_at is not None else None,
    }


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


class WorkbenchSessionStore:
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

    async def get(self, *, owner_id: str, session_id: str) -> dict[str, Any] | None:
        async with await get_db() as db:
            session = await db.scalar(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            return _session_dict(session) if session else None

    async def list(
        self, *, owner_id: str, limit: int = 50, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), MAX_EVENT_LIST_LIMIT))
        async with await get_db() as db:
            query = select(WorkbenchSession).where(
                WorkbenchSession.user_id == owner_id,
                WorkbenchSession.deleted_at.is_(None),
            )
            if not include_archived:
                query = query.where(WorkbenchSession.archived_at.is_(None))
            rows = await db.scalars(
                query.order_by(WorkbenchSession.updated_at.desc()).limit(safe_limit)
            )
            return [_session_dict(row) for row in rows.all()]

    async def archive_stale(
        self,
        *,
        idle_seconds: int,
        now_ms: int | None = None,
    ) -> int:
        """Archive idle Workbench UI records without touching linked execution.

        Workbench sessions are durable observability projections. Archiving a
        stale OPEN/RUNNING record only removes it from the active-session list;
        it never cancels a task, command, monitor, or workspace.
        """
        current = _now_ms() if now_ms is None else int(now_ms)
        cutoff = current - max(1, int(idle_seconds)) * 1000
        async with await get_db() as db:
            rows = await db.scalars(
                select(WorkbenchSession).where(
                    WorkbenchSession.deleted_at.is_(None),
                    WorkbenchSession.archived_at.is_(None),
                    WorkbenchSession.updated_at < cutoff,
                )
            )
            sessions = list(rows.all())
            for session in sessions:
                session.status = "ARCHIVED"
                session.archived_at = current
                session.updated_at = current
                session.active_target_type = None
                session.active_target_id = None
                session.active_workspace_id = None
            if sessions:
                await db.commit()
            return len(sessions)

    async def append_event(
        self,
        *,
        owner_id: str,
        session_id: str,
        event_type: str,
        summary: str,
        state: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        workspace_id: str | None = None,
        tool_name: str | None = None,
        source: str = "plugin",
        actor: str = "chatgpt_plugin",
        details: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if target_type is not None and target_type not in _ALLOWED_TARGET_TYPES:
            raise ValueError("invalid workbench target type")
        if not event_type or len(event_type) > 120:
            raise ValueError("invalid workbench event type")
        now = _now_ms()
        async with await get_db() as db:
            session = await db.scalar(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            if session is None:
                return None
            sequence = int(session.event_count or 0) + 1
            event = WorkbenchSessionEvent(
                session_id=session.id,
                user_id=owner_id,
                sequence=sequence,
                source=_clip(source, 40) or "plugin",
                actor=_clip(actor, 80) or "chatgpt_plugin",
                event_type=event_type,
                state=_clip(state, 80) if state else None,
                target_type=target_type,
                target_id=_clip(target_id, 200) if target_id else None,
                workspace_id=_clip(workspace_id, 200) if workspace_id else None,
                tool_name=_clip(tool_name, 160) if tool_name else None,
                summary=_clip(summary, MAX_EVENT_SUMMARY_CHARS),
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
                session.active_target_id = event.target_id
                session.active_workspace_id = event.workspace_id
            normalized_state = (state or "").upper()
            if normalized_state in _ALLOWED_STATES:
                session.status = normalized_state
            await db.commit()
            await db.refresh(event)
            return _event_dict(event)

    async def events(
        self, *, owner_id: str, session_id: str, after_sequence: int = 0, limit: int = 100
    ) -> list[dict[str, Any]] | None:
        safe_limit = max(1, min(int(limit), MAX_EVENT_LIST_LIMIT))
        async with await get_db() as db:
            exists = await db.scalar(
                select(WorkbenchSession.id).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            if exists is None:
                return None
            rows = await db.scalars(
                select(WorkbenchSessionEvent)
                .where(
                    WorkbenchSessionEvent.session_id == session_id,
                    WorkbenchSessionEvent.user_id == owner_id,
                    WorkbenchSessionEvent.sequence > max(0, int(after_sequence)),
                )
                .order_by(WorkbenchSessionEvent.sequence.asc())
                .limit(safe_limit)
            )
            return [_event_dict(row) for row in rows.all()]

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
            raise ValueError("invalid workbench target")
        now = _now_ms()
        async with await get_db() as db:
            session = await db.scalar(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            if session is None:
                return None
            session.active_target_type = target_type
            session.active_target_id = _clip(target_id, 200)
            session.active_workspace_id = _clip(workspace_id, 200) if workspace_id else None
            if session.status == "OPEN":
                session.status = "RUNNING"
            session.updated_at = now
            await db.commit()
            await db.refresh(session)
            return _session_dict(session)

    async def reconcile_command_terminal(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        command_id: str,
        status: str,
        exit_code: int | None = None,
    ) -> int:
        normalized_status = str(status or "").upper()
        if normalized_status not in {"COMPLETE", "FAILED", "CANCELLED"}:
            raise ValueError("invalid command terminal status")
        now = _now_ms()
        async with await get_db() as db:
            rows = await db.scalars(
                select(WorkbenchSession).where(
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                    WorkbenchSession.archived_at.is_(None),
                    WorkbenchSession.active_target_type == "command",
                    WorkbenchSession.active_target_id == command_id,
                    WorkbenchSession.active_workspace_id == workspace_id,
                )
            )
            sessions = rows.all()
            for session in sessions:
                sequence = int(session.event_count or 0) + 1
                db.add(
                    WorkbenchSessionEvent(
                        session_id=session.id,
                        user_id=owner_id,
                        sequence=sequence,
                        source="backend",
                        actor="cptr_runtime",
                        event_type="command.completed",
                        state=normalized_status,
                        target_type="command",
                        target_id=_clip(command_id, 200),
                        workspace_id=_clip(workspace_id, 200),
                        tool_name=None,
                        summary=_clip(
                            "Command finished; Workbench remains available for ChatGPT follow-up.",
                            MAX_EVENT_SUMMARY_CHARS,
                        ),
                        details=_safe_json({"exit_code": exit_code}),
                        metrics={},
                        policy={},
                        created_at=now,
                    )
                )
                session.event_count = sequence
                session.updated_at = now
                session.last_event_at = now
                session.active_target_type = None
                session.active_target_id = None
                session.active_workspace_id = None
                session.status = "OPEN"
            if sessions:
                await db.commit()
            return len(sessions)

    async def rename(self, *, owner_id: str, session_id: str, name: str) -> dict[str, Any] | None:
        now = _now_ms()
        async with await get_db() as db:
            session = await db.scalar(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
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
            session = await db.scalar(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
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
        confirmation_id = secrets.token_urlsafe(24)
        confirmation_hash = hashlib.sha256(confirmation_id.encode()).hexdigest()
        async with await get_db() as db:
            session = await db.scalar(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
            )
            if session is None:
                return None
            session.delete_requested_at = now
            session.delete_confirmation_hash = confirmation_hash
            session.delete_confirmation_expires_at = now + DELETE_CONFIRMATION_TTL_MS
            session.updated_at = now
            await db.commit()
            return {
                "session_id": session.id,
                "confirmation_id": confirmation_id,
                "expires_at": int(session.delete_confirmation_expires_at),
                "event_count": int(session.event_count or 0),
                "impact": "Deletes this Workbench session and its redacted session events only; linked CPTR work remains unchanged.",
            }

    async def confirm_delete(self, *, owner_id: str, confirmation_id: str) -> dict[str, Any] | None:
        now = _now_ms()
        confirmation_hash = hashlib.sha256(confirmation_id.encode()).hexdigest()
        async with await get_db() as db:
            session = await db.scalar(
                select(WorkbenchSession).where(
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                    WorkbenchSession.delete_confirmation_hash == confirmation_hash,
                    WorkbenchSession.delete_confirmation_expires_at.is_not(None),
                    WorkbenchSession.delete_confirmation_expires_at >= now,
                )
            )
            if session is None:
                return None
            session_id = session.id
            await db.execute(
                delete(WorkbenchSessionEvent).where(
                    WorkbenchSessionEvent.session_id == session_id,
                    WorkbenchSessionEvent.user_id == owner_id,
                )
            )
            session.status = "DELETED"
            session.deleted_at = now
            session.updated_at = now
            session.delete_confirmation_hash = None
            session.delete_confirmation_expires_at = None
            await db.commit()
            return {"session_id": session_id, "status": "DELETED", "deleted_at": now}


workbench_session_store = WorkbenchSessionStore()


async def workbench_session_reaper_loop() -> None:
    """Periodically archive stale Workbench observability records."""
    from cptr.env import (
        WORKBENCH_SESSION_IDLE_ARCHIVE_SECONDS,
        WORKBENCH_SESSION_REAPER_INTERVAL_SECONDS,
    )

    while True:
        await workbench_session_store.archive_stale(
            idle_seconds=WORKBENCH_SESSION_IDLE_ARCHIVE_SECONDS
        )
        await asyncio.sleep(WORKBENCH_SESSION_REAPER_INTERVAL_SECONDS)
