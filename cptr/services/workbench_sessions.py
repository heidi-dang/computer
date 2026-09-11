"""Durable owner-scoped Workbench Session persistence.

Workbench sessions are safe observability records used by the ChatGPT plugin.
They never contain private prompts or reasoning and are distinct from task,
command, and autonomous execution state.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from sqlalchemy import delete, select, update

from cptr.models import WorkbenchSession, WorkbenchSessionEvent
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_external, redact_external_text

MAX_SESSION_NAME_CHARS = 120
MAX_EVENT_SUMMARY_CHARS = 4_000
MAX_EVENT_DETAIL_CHARS = 8_000
MAX_EVENT_DETAILS_KEYS = 32
MAX_EVENT_LIST_LIMIT = 200
DELETE_CONFIRMATION_TTL_MS = 5 * 60 * 1000
_UNSET = object()
_ALLOWED_TARGET_TYPES = {"task", "command", "monitor"}
_TERMINAL_TARGET_STATES = {"COMPLETE", "FAILED", "CANCELLED"}
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


def _normalized_event_state(event_type: str, state: str | None) -> str:
    event_name = str(event_type or "").strip().lower()
    if event_name == "workbench.target.bound" or event_name.endswith(".started"):
        return "RUNNING"
    if event_name.endswith((".completed", ".complete")):
        return "COMPLETE"
    if event_name.endswith(".failed"):
        return "FAILED"
    if event_name.endswith((".cancelled", ".canceled")):
        return "CANCELLED"
    normalized = str(state or "").strip().upper()
    return normalized if normalized in _ALLOWED_STATES else ""


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


def _session_dict(session: WorkbenchSession | Any) -> dict[str, Any]:
    env_override = getattr(session, "environment_profile_override", None)
    role_ctx = getattr(session, "role_context", None)
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
        "environment_profile_id": getattr(session, "environment_profile_id", None),
        "environment_profile_override": dict(env_override)
        if isinstance(env_override, dict)
        else None,
        "admin_role": getattr(session, "admin_role", None),
        "role_context": dict(role_ctx) if isinstance(role_ctx, dict) else None,
        "last_context_snapshot_id": getattr(session, "last_context_snapshot_id", None),
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
        self,
        *,
        owner_id: str,
        name: str | None = None,
        workspace_id: str | None = None,
        environment_profile_id: str | None = None,
        environment_profile_override: dict[str, Any] | None = None,
        admin_role: str | None = None,
        role_context: dict[str, Any] | None = None,
        last_context_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with await get_db() as db:
            session = WorkbenchSession(
                user_id=owner_id,
                name=_safe_name(name),
                workspace_id=_clip(workspace_id, 200) if workspace_id else None,
                status="OPEN",
                event_count=0,
                created_at=now,
                updated_at=now,
                environment_profile_id=_clip(environment_profile_id, 200)
                if environment_profile_id
                else None,
                environment_profile_override=_safe_json(environment_profile_override)
                if environment_profile_override
                else None,
                admin_role=_clip(admin_role, 120) if admin_role else None,
                role_context=_safe_json(role_context) if role_context else None,
                last_context_snapshot_id=_clip(last_context_snapshot_id, 200)
                if last_context_snapshot_id
                else None,
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
            session_ids = [session.id for session in sessions]
        if session_ids:
            from cptr.services.capability_os.lifecycle import revoke_task_authority

            for session_id in session_ids:
                await revoke_task_authority(session_id, now_ms=current)
        return len(session_ids)

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
        normalized_state = _normalized_event_state(event_type, state)
        async with await get_db() as db:
            # Claim the sequence with the first write in this transaction. SQLite
            # serializes writers, and UPDATE ... RETURNING also gives PostgreSQL
            # row-level serialization if this store is migrated later. This avoids
            # the read-then-increment race where two appenders observed the same
            # event_count and attempted the same (session_id, sequence).
            claimed_result = await db.execute(
                update(WorkbenchSession)
                .where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                )
                .values(
                    event_count=WorkbenchSession.event_count + 1,
                    updated_at=now,
                    last_event_at=now,
                )
                .returning(
                    WorkbenchSession.id,
                    WorkbenchSession.event_count,
                    WorkbenchSession.active_target_type,
                    WorkbenchSession.active_target_id,
                )
            )
            claimed = claimed_result.one_or_none()
            if claimed is None:
                return None
            sequence = int(claimed.event_count)
            event = WorkbenchSessionEvent(
                session_id=claimed.id,
                user_id=owner_id,
                sequence=sequence,
                source=_clip(source, 40) or "plugin",
                actor=_clip(actor, 80) or "chatgpt_plugin",
                event_type=event_type,
                state=normalized_state or None,
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
            projection: dict[str, Any] = {}
            if target_type and target_id:
                target_matches = (
                    claimed.active_target_type == target_type
                    and claimed.active_target_id == event.target_id
                )
                if normalized_state in _TERMINAL_TARGET_STATES:
                    if target_matches:
                        projection.update(
                            active_target_type=None,
                            active_target_id=None,
                            active_workspace_id=None,
                            status="OPEN",
                        )
                else:
                    projection.update(
                        active_target_type=target_type,
                        active_target_id=event.target_id,
                        active_workspace_id=event.workspace_id,
                    )
                    if normalized_state in _ALLOWED_STATES:
                        projection["status"] = normalized_state
            elif normalized_state in _ALLOWED_STATES:
                projection["status"] = normalized_state
            if projection:
                await db.execute(
                    update(WorkbenchSession)
                    .where(
                        WorkbenchSession.id == claimed.id,
                        WorkbenchSession.user_id == owner_id,
                    )
                    .values(**projection)
                )
            await db.commit()
            await db.refresh(event)
            result = _event_dict(event)
        if target_type is None and normalized_state in _TERMINAL_TARGET_STATES:
            from cptr.services.capability_os.lifecycle import revoke_task_authority

            await revoke_task_authority(session_id, now_ms=now)
        return result

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
        last_context_snapshot_id: str | None = None,
        make_sticky: bool = False,
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
            target_ws = _clip(workspace_id, 200) if workspace_id else None
            if target_ws:
                session.active_workspace_id = target_ws
                if session.workspace_id is None or make_sticky:
                    session.workspace_id = target_ws
            elif session.workspace_id:
                session.active_workspace_id = session.workspace_id
            else:
                session.active_workspace_id = None

            if last_context_snapshot_id is not None:
                session.last_context_snapshot_id = (
                    _clip(last_context_snapshot_id, 200) if last_context_snapshot_id else None
                )

            session.status = "RUNNING"
            session.archived_at = None
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
            # Terminal reconciliation competes with plugin-originated appends, so
            # claim every matching session's next sequence in the same atomic
            # UPDATE that clears the active target projection. Re-checking the
            # target predicates in the write also prevents a stale pre-read from
            # completing a newer command binding.
            claimed_result = await db.execute(
                update(WorkbenchSession)
                .where(
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                    WorkbenchSession.archived_at.is_(None),
                    WorkbenchSession.active_target_type == "command",
                    WorkbenchSession.active_target_id == command_id,
                    WorkbenchSession.active_workspace_id == workspace_id,
                )
                .values(
                    event_count=WorkbenchSession.event_count + 1,
                    updated_at=now,
                    last_event_at=now,
                    active_target_type=None,
                    active_target_id=None,
                    active_workspace_id=None,
                    status="OPEN",
                )
                .returning(WorkbenchSession.id, WorkbenchSession.event_count)
            )
            claimed_sessions = claimed_result.all()
            for claimed in claimed_sessions:
                db.add(
                    WorkbenchSessionEvent(
                        session_id=claimed.id,
                        user_id=owner_id,
                        sequence=int(claimed.event_count),
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
            if claimed_sessions:
                await db.commit()
            return len(claimed_sessions)

    async def update(
        self,
        *,
        owner_id: str,
        session_id: str,
        name: str | None = None,
        workspace_id: str | None = _UNSET,
        environment_profile_id: str | None = _UNSET,
        environment_profile_override: dict[str, Any] | None = _UNSET,
        admin_role: str | None = _UNSET,
        role_context: dict[str, Any] | None = _UNSET,
        last_context_snapshot_id: str | None = _UNSET,
    ) -> dict[str, Any] | None:
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
            if name is not None:
                session.name = _safe_name(name)
            if workspace_id is not _UNSET:
                session.workspace_id = _clip(workspace_id, 200) if workspace_id else None
            if environment_profile_id is not _UNSET:
                session.environment_profile_id = (
                    _clip(environment_profile_id, 200) if environment_profile_id else None
                )
            if environment_profile_override is not _UNSET:
                session.environment_profile_override = (
                    _safe_json(environment_profile_override)
                    if environment_profile_override
                    else None
                )
            if admin_role is not _UNSET:
                session.admin_role = _clip(admin_role, 120) if admin_role else None
            if role_context is not _UNSET:
                session.role_context = _safe_json(role_context) if role_context else None
            if last_context_snapshot_id is not _UNSET:
                session.last_context_snapshot_id = (
                    _clip(last_context_snapshot_id, 200) if last_context_snapshot_id else None
                )
            session.updated_at = now
            await db.commit()
            await db.refresh(session)
            return _session_dict(session)

    async def rename(self, *, owner_id: str, session_id: str, name: str) -> dict[str, Any] | None:
        return await self.update(owner_id=owner_id, session_id=session_id, name=name)

    async def reconcile_restart(self, *, now_ms: int | None = None) -> int:
        """Reconcile transient targets left dangling after a backend process restart.

        Preserves sticky Workspace OS bindings (workspace_id, environment_profile,
        admin_role, role_context, last_context_snapshot_id) while releasing active
        in-process targets left dangling by the restart.
        """
        now = _now_ms() if now_ms is None else int(now_ms)
        async with await get_db() as db:
            rows = await db.scalars(
                select(WorkbenchSession).where(
                    WorkbenchSession.deleted_at.is_(None),
                    WorkbenchSession.archived_at.is_(None),
                    WorkbenchSession.active_target_type == "command",
                )
            )
            sessions = list(rows.all())
            for session in sessions:
                old_target_id = session.active_target_id
                old_workspace_id = session.active_workspace_id
                session.event_count = int(session.event_count or 0) + 1
                session.active_target_type = None
                session.active_target_id = None
                session.active_workspace_id = None
                session.status = "OPEN"
                session.updated_at = now
                session.last_event_at = now
                db.add(
                    WorkbenchSessionEvent(
                        session_id=session.id,
                        user_id=session.user_id,
                        sequence=int(session.event_count),
                        source="backend",
                        actor="cptr_runtime",
                        event_type="workbench.restart_reconciled",
                        state="OPEN",
                        target_type="command",
                        target_id=_clip(old_target_id, 200) if old_target_id else None,
                        workspace_id=_clip(old_workspace_id or session.workspace_id, 200)
                        if (old_workspace_id or session.workspace_id)
                        else None,
                        summary="Transient target cleared after backend process restart; Workbench remains open.",
                        details=_safe_json(
                            {"reconciled_after_restart": True, "target_id": old_target_id}
                        ),
                        metrics={},
                        policy={},
                        created_at=now,
                    )
                )
            if sessions:
                await db.commit()
            return len(sessions)

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
            session.active_target_type = None
            session.active_target_id = None
            session.active_workspace_id = None
            session.updated_at = now
            await db.commit()
            await db.refresh(session)
            result = _session_dict(session)
        from cptr.services.capability_os.lifecycle import revoke_task_authority

        await revoke_task_authority(session_id, now_ms=now)
        return result

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
        from cptr.services.capability_os.lifecycle import revoke_task_authority

        await revoke_task_authority(session_id, now_ms=now)
        return {"session_id": session_id, "status": "DELETED", "deleted_at": now}


workbench_session_store = WorkbenchSessionStore()


async def workbench_session_reaper_loop() -> None:
    """Periodically archive stale Workbench observability records."""
    from cptr.env import (
        WORKBENCH_SESSION_IDLE_ARCHIVE_SECONDS,
        WORKBENCH_SESSION_REAPER_INTERVAL_SECONDS,
    )
    from cptr.services.worker_watchdog import heartbeat_sleep, heartbeat_worker

    while True:
        await workbench_session_store.archive_stale(
            idle_seconds=WORKBENCH_SESSION_IDLE_ARCHIVE_SECONDS
        )
        # Capability OS authority has the same owner-scoped task identities as
        # Workbench/Factory/Control. Reconcile it here on startup and every
        # reaper interval so a crash between a task-state commit and cleanup
        # cannot leave durable authority behind.
        from cptr.services.capability_os.lifecycle import reconcile_inactive_task_authority

        await reconcile_inactive_task_authority()
        heartbeat_worker("workbench_reaper", success=True)
        await heartbeat_sleep("workbench_reaper", WORKBENCH_SESSION_REAPER_INTERVAL_SECONDS)
