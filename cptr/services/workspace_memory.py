"""Transactional, owner-scoped memory for ChatGPT direct CPTR workspace work.

This is intentionally separate from ``cptr.utils.memory``.  Native-chat memory
may review conversations with a utility model; this service only persists
redacted, bounded, deterministic direct-tool activity and user-directed facts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError

from cptr.models import Workspace
from cptr.models.workspace_memory import (
    WorkspaceMemoryEvent,
    WorkspaceMemoryFact,
    WorkspaceMemoryStream,
)
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_external

MAX_EVENT_SUMMARY = 1_200
MAX_FACT_CONTENT = 1_600
MAX_DETAILS_BYTES = 4_000
MAX_AFFECTED_PATHS = 32
MAX_TIMELINE_LIMIT = 100
MAX_CONTEXT_FACTS = 12
MAX_CONTEXT_ACTIONS = 10
_MEMORY_BUSY_RETRIES = 3

_EVENT_KINDS = {
    "workspace.inspected",
    "workspace.changed",
    "workspace.command_started",
    "workspace.command_completed",
    "workspace.command_failed",
    "workspace.test_completed",
    "workspace.test_failed",
    "workspace.git_observed",
    "workspace.session_linked",
    "workspace.decision_recorded",
}
_FACT_CATEGORIES = {"architecture", "convention", "decision", "verification", "limitation", "note"}
_PATH_KEY = re.compile(r"(?:path|paths|file|files|source|destination)$", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/]|~[\\/])")
_SENSITIVE_OR_RAW_KEY = re.compile(
    r"(?:authorization|token|secret|password|credential|cookie|api[_-]?key|ticket|prompt|"
    r"argument|result_json|output|stdout|stderr|transcript|content)$",
    re.IGNORECASE,
)


def _now() -> int:
    return int(time.time())


def _bounded_text(value: Any, maximum: int) -> str:
    text = str(redact_external(str(value or ""))).strip()
    return text[:maximum]


def _safe_relative_path(value: Any) -> str | None:
    path = str(value or "").strip().replace("\\", "/")
    if not path or _ABSOLUTE_PATH.match(path) or "\x00" in path:
        return None
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    normalized = "/".join(parts)
    return normalized[:300]


def _safe_paths(values: Iterable[Any] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        path = _safe_relative_path(value)
        if path and path not in seen:
            seen.add(path)
            result.append(path)
        if len(result) >= MAX_AFFECTED_PATHS:
            break
    return result


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    """Return a small redacted diagnostic projection, never raw tool payloads."""
    if depth > 4:
        return "[max-depth]"
    if isinstance(value, str):
        return _bounded_text(value, 500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_bounded_json(item, depth=depth + 1) for item in list(value)[:20]]
    if not isinstance(value, dict):
        return _bounded_text(value, 500)
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:30]:
        key_text = str(key)[:100]
        if _SENSITIVE_OR_RAW_KEY.search(key_text):
            continue
        if _PATH_KEY.search(key_text):
            if isinstance(item, (list, tuple, set)):
                result[key_text] = _safe_paths(item)
            else:
                path = _safe_relative_path(item)
                if path:
                    result[key_text] = path
            continue
        result[key_text] = _bounded_json(item, depth=depth + 1)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= MAX_DETAILS_BYTES:
        return result
    return {"truncated": True, "sha256": hashlib.sha256(encoded.encode()).hexdigest()}


def _event_dict(event: WorkspaceMemoryEvent) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "sequence": event.sequence,
        "operation_id": event.operation_id,
        "kind": event.kind,
        "source": event.source,
        "session_id": event.session_id,
        "tool_name": event.tool_name,
        "outcome": event.outcome,
        "summary": event.summary,
        "affected_paths": list(event.affected_paths or []),
        "details": dict(event.details or {}),
        "workspace_fingerprint": event.workspace_fingerprint,
        "created_at": event.created_at,
    }


def _fact_dict(fact: WorkspaceMemoryFact, *, current_fingerprint: str | None = None) -> dict[str, Any]:
    is_stale = fact.status == "STALE" or (
        bool(current_fingerprint)
        and bool(fact.verified_fingerprint)
        and fact.verified_fingerprint != current_fingerprint
    )
    return {
        "fact_id": fact.id,
        "category": fact.category,
        "content": fact.content,
        "paths": list(fact.paths or []),
        "source_event_id": fact.source_event_id,
        "status": "STALE" if is_stale else fact.status,
        "pinned": bool(fact.pinned),
        "revision": fact.revision,
        "verified_fingerprint": fact.verified_fingerprint,
        "created_at": fact.created_at,
        "updated_at": fact.updated_at,
    }


def _snapshot_from_events(events: list[WorkspaceMemoryEvent], previous: dict[str, Any]) -> dict[str, Any]:
    """Deterministically compact the tail of the immutable event stream."""
    recent = events[-MAX_CONTEXT_ACTIONS:]
    changed_paths: list[str] = []
    blockers: list[str] = []
    verified_commands: list[dict[str, Any]] = []
    active_goal = previous.get("active_goal") if isinstance(previous, dict) else None
    last_completed = previous.get("last_completed") if isinstance(previous, dict) else None

    for event in events:
        for path in event.affected_paths or []:
            if path not in changed_paths:
                changed_paths.append(path)
        if event.kind in {"workspace.command_failed", "workspace.test_failed"}:
            if event.summary not in blockers:
                blockers.append(event.summary)
        elif event.kind in {"workspace.command_completed", "workspace.test_completed"}:
            blockers = [item for item in blockers if item != event.summary]
            details = dict(event.details or {})
            verified_commands.append(
                {
                    "tool_name": event.tool_name,
                    "summary": event.summary,
                    "exit_code": details.get("exit_code"),
                    "workspace_fingerprint": event.workspace_fingerprint,
                    "created_at": event.created_at,
                }
            )
        if event.kind == "workspace.decision_recorded":
            if dict(event.details or {}).get("category") == "decision":
                last_completed = event.summary
            if dict(event.details or {}).get("category") == "goal":
                active_goal = event.summary
        if event.outcome == "COMPLETE" and event.kind in {
            "workspace.changed",
            "workspace.command_completed",
            "workspace.test_completed",
        }:
            last_completed = event.summary

    return {
        "active_goal": _bounded_text(active_goal, 800) if active_goal else None,
        "last_completed": _bounded_text(last_completed, 800) if last_completed else None,
        "open_blockers": blockers[-6:],
        "changed_paths": changed_paths[-32:],
        "verified_commands": verified_commands[-6:],
        "recent_actions": [
            {
                "sequence": event.sequence,
                "kind": event.kind,
                "summary": event.summary,
                "outcome": event.outcome,
                "created_at": event.created_at,
            }
            for event in recent
        ],
    }


class WorkspaceMemoryStore:
    """Database-backed durable memory with a strict ``(owner, workspace)`` boundary."""

    async def _ensure_stream(self, db, *, owner_id: str, workspace_id: str, now: int) -> WorkspaceMemoryStream:
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None or workspace.user_id != owner_id:
            raise LookupError("workspace not found")
        await db.execute(
            sqlite_insert(WorkspaceMemoryStream)
            .values(
                id=f"wmm_{uuid.uuid4().hex}",
                user_id=owner_id,
                workspace_id=workspace_id,
                next_sequence=0,
                snapshot={},
                snapshot_through_sequence=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "workspace_id"])
        )
        result = await db.execute(
            select(WorkspaceMemoryStream).where(
                WorkspaceMemoryStream.user_id == owner_id,
                WorkspaceMemoryStream.workspace_id == workspace_id,
            )
        )
        stream = result.scalar_one_or_none()
        if stream is None:  # Defensive: the unique insert must make one stream available.
            raise RuntimeError("workspace memory stream was not created")
        return stream

    async def _existing_event(
        self, db, *, owner_id: str, workspace_id: str, operation_id: str, kind: str
    ) -> WorkspaceMemoryEvent | None:
        result = await db.execute(
            select(WorkspaceMemoryEvent).where(
                WorkspaceMemoryEvent.user_id == owner_id,
                WorkspaceMemoryEvent.workspace_id == workspace_id,
                WorkspaceMemoryEvent.operation_id == operation_id,
                WorkspaceMemoryEvent.kind == kind,
            )
        )
        return result.scalar_one_or_none()

    async def record_event(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        operation_id: str,
        kind: str,
        summary: str,
        tool_name: str | None = None,
        outcome: str = "COMPLETE",
        source: str = "mcp",
        session_id: str | None = None,
        affected_paths: Iterable[Any] | None = None,
        details: dict[str, Any] | None = None,
        workspace_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if kind not in _EVENT_KINDS:
            raise ValueError("unsupported workspace memory event kind")
        if not operation_id or len(operation_id) > 200:
            raise ValueError("operation_id is required and must be at most 200 characters")
        safe_summary = _bounded_text(summary, MAX_EVENT_SUMMARY)
        if not safe_summary:
            raise ValueError("summary is required")
        safe_paths = _safe_paths(affected_paths)
        safe_details = _bounded_json(details or {})
        safe_outcome = str(outcome or "COMPLETE").upper()[:32]
        safe_source = str(source or "mcp")[:80]
        safe_tool_name = _bounded_text(tool_name, 160) if tool_name else None
        safe_fingerprint = _bounded_text(workspace_fingerprint, 128) if workspace_fingerprint else None

        for attempt in range(_MEMORY_BUSY_RETRIES):
            now = _now()
            try:
                async with await get_db() as db:
                    stream = await self._ensure_stream(
                        db, owner_id=owner_id, workspace_id=workspace_id, now=now
                    )
                    existing = await self._existing_event(
                        db,
                        owner_id=owner_id,
                        workspace_id=workspace_id,
                        operation_id=operation_id,
                        kind=kind,
                    )
                    if existing:
                        # No state was changed for this duplicate operation. Do not
                        # roll back here: rollback expires async ORM attributes and
                        # would force implicit I/O while serializing the result.
                        return {"event": _event_dict(existing), "idempotent": True}

                    sequence_result = await db.execute(
                        update(WorkspaceMemoryStream)
                        .where(WorkspaceMemoryStream.id == stream.id)
                        .values(
                            next_sequence=WorkspaceMemoryStream.next_sequence + 1,
                            workspace_fingerprint=safe_fingerprint or stream.workspace_fingerprint,
                            updated_at=now,
                        )
                        .returning(WorkspaceMemoryStream.next_sequence)
                    )
                    sequence = int(sequence_result.scalar_one())
                    event = WorkspaceMemoryEvent(
                        id=f"wme_{uuid.uuid4().hex}",
                        stream_id=stream.id,
                        user_id=owner_id,
                        workspace_id=workspace_id,
                        sequence=sequence,
                        operation_id=operation_id,
                        kind=kind,
                        source=safe_source,
                        session_id=session_id,
                        tool_name=safe_tool_name,
                        outcome=safe_outcome,
                        summary=safe_summary,
                        affected_paths=safe_paths,
                        details=safe_details,
                        workspace_fingerprint=safe_fingerprint,
                        created_at=now,
                    )
                    db.add(event)
                    await db.flush()

                    if kind == "workspace.changed" and safe_paths:
                        facts_result = await db.execute(
                            select(WorkspaceMemoryFact).where(
                                WorkspaceMemoryFact.user_id == owner_id,
                                WorkspaceMemoryFact.workspace_id == workspace_id,
                                WorkspaceMemoryFact.status == "ACTIVE",
                            )
                        )
                        for fact in facts_result.scalars().all():
                            if set(fact.paths or []).intersection(safe_paths):
                                fact.status = "STALE"
                                fact.updated_at = now
                                fact.revision += 1

                    recent_result = await db.execute(
                        select(WorkspaceMemoryEvent)
                        .where(WorkspaceMemoryEvent.stream_id == stream.id)
                        .order_by(WorkspaceMemoryEvent.sequence.desc())
                        .limit(80)
                    )
                    recent_events = list(reversed(recent_result.scalars().all()))
                    stream.snapshot = _snapshot_from_events(recent_events, dict(stream.snapshot or {}))
                    stream.snapshot_through_sequence = sequence
                    stream.workspace_fingerprint = safe_fingerprint or stream.workspace_fingerprint
                    stream.updated_at = now
                    await db.commit()
                    return {"event": _event_dict(event), "idempotent": False}
            except IntegrityError:
                # A concurrent retry with the same MCP operation won the unique key.
                async with await get_db() as retry_db:
                    existing = await self._existing_event(
                        retry_db,
                        owner_id=owner_id,
                        workspace_id=workspace_id,
                        operation_id=operation_id,
                        kind=kind,
                    )
                    if existing:
                        return {"event": _event_dict(existing), "idempotent": True}
                if attempt + 1 >= _MEMORY_BUSY_RETRIES:
                    raise
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt + 1 >= _MEMORY_BUSY_RETRIES:
                    raise
            await asyncio.sleep(0.02 * (attempt + 1))
        raise RuntimeError("workspace memory write did not complete")

    async def record_fact(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        content: str,
        category: str = "note",
        pinned: bool = False,
        paths: Iterable[Any] | None = None,
        source_event_id: str | None = None,
        workspace_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        safe_content = _bounded_text(content, MAX_FACT_CONTENT)
        if not safe_content:
            raise ValueError("content is required")
        safe_category = str(category or "note").lower()
        if safe_category not in _FACT_CATEGORIES:
            raise ValueError("unsupported workspace memory fact category")
        now = _now()
        async with await get_db() as db:
            stream = await self._ensure_stream(db, owner_id=owner_id, workspace_id=workspace_id, now=now)
            source_event = None
            if source_event_id:
                source_event = await db.get(WorkspaceMemoryEvent, source_event_id)
                if (
                    source_event is None
                    or source_event.user_id != owner_id
                    or source_event.workspace_id != workspace_id
                ):
                    raise LookupError("workspace memory source event not found")
            fact = WorkspaceMemoryFact(
                id=f"wmf_{uuid.uuid4().hex}",
                user_id=owner_id,
                workspace_id=workspace_id,
                category=safe_category,
                content=safe_content,
                paths=_safe_paths(paths),
                source_event_id=source_event_id,
                verified_fingerprint=_bounded_text(workspace_fingerprint, 128)
                if workspace_fingerprint
                else stream.workspace_fingerprint,
                status="ACTIVE",
                pinned=bool(pinned),
                revision=1,
                created_at=now,
                updated_at=now,
            )
            db.add(fact)
            await db.commit()
            return _fact_dict(fact, current_fingerprint=stream.workspace_fingerprint)

    async def get_context(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        workspace_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        async with await get_db() as db:
            result = await db.execute(
                select(WorkspaceMemoryStream).where(
                    WorkspaceMemoryStream.user_id == owner_id,
                    WorkspaceMemoryStream.workspace_id == workspace_id,
                )
            )
            stream = result.scalar_one_or_none()
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None or workspace.user_id != owner_id:
                raise LookupError("workspace not found")
            if stream is None:
                return {
                    "workspace_id": workspace_id,
                    "memory_cursor": 0,
                    "workspace_stage": {},
                    "relevant_facts": [],
                    "freshness": {
                        "has_memory": False,
                        "matches_current_workspace_fingerprint": None,
                    },
                }
            current_fingerprint = _bounded_text(workspace_fingerprint, 128) if workspace_fingerprint else None
            facts_result = await db.execute(
                select(WorkspaceMemoryFact)
                .where(
                    WorkspaceMemoryFact.user_id == owner_id,
                    WorkspaceMemoryFact.workspace_id == workspace_id,
                    WorkspaceMemoryFact.status.in_(["ACTIVE", "STALE"]),
                    WorkspaceMemoryFact.deleted_at.is_(None),
                )
                .order_by(WorkspaceMemoryFact.pinned.desc(), WorkspaceMemoryFact.updated_at.desc())
                .limit(MAX_CONTEXT_FACTS * 3)
            )
            facts = [
                _fact_dict(fact, current_fingerprint=current_fingerprint)
                for fact in facts_result.scalars().all()
            ]
            active_facts = [fact for fact in facts if fact["status"] == "ACTIVE"][:MAX_CONTEXT_FACTS]
            matches = (
                None
                if not current_fingerprint or not stream.workspace_fingerprint
                else current_fingerprint == stream.workspace_fingerprint
            )
            return {
                "workspace_id": workspace_id,
                "memory_cursor": stream.next_sequence,
                "workspace_stage": dict(stream.snapshot or {}),
                "relevant_facts": active_facts,
                "freshness": {
                    "has_memory": True,
                    "snapshot_through_sequence": stream.snapshot_through_sequence,
                    "memory_workspace_fingerprint": stream.workspace_fingerprint,
                    "current_workspace_fingerprint": current_fingerprint,
                    "matches_current_workspace_fingerprint": matches,
                    "stale_fact_count": sum(1 for fact in facts if fact["status"] == "STALE"),
                },
            }

    async def list_events(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        after_sequence: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), MAX_TIMELINE_LIMIT))
        async with await get_db() as db:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None or workspace.user_id != owner_id:
                raise LookupError("workspace not found")
            result = await db.execute(
                select(WorkspaceMemoryEvent)
                .where(
                    WorkspaceMemoryEvent.user_id == owner_id,
                    WorkspaceMemoryEvent.workspace_id == workspace_id,
                    WorkspaceMemoryEvent.sequence > max(0, int(after_sequence)),
                )
                .order_by(WorkspaceMemoryEvent.sequence)
                .limit(safe_limit)
            )
            events = list(result.scalars().all())
            return {
                "workspace_id": workspace_id,
                "events": [_event_dict(event) for event in events],
                "last_sequence": events[-1].sequence if events else max(0, int(after_sequence)),
            }

    async def list_facts(self, *, owner_id: str, workspace_id: str, include_stale: bool = True) -> dict[str, Any]:
        async with await get_db() as db:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None or workspace.user_id != owner_id:
                raise LookupError("workspace not found")
            query = select(WorkspaceMemoryFact).where(
                WorkspaceMemoryFact.user_id == owner_id,
                WorkspaceMemoryFact.workspace_id == workspace_id,
                WorkspaceMemoryFact.deleted_at.is_(None),
            )
            if not include_stale:
                query = query.where(WorkspaceMemoryFact.status == "ACTIVE")
            result = await db.execute(
                query.order_by(WorkspaceMemoryFact.pinned.desc(), WorkspaceMemoryFact.updated_at.desc()).limit(100)
            )
            return {"workspace_id": workspace_id, "facts": [_fact_dict(fact) for fact in result.scalars().all()]}

    async def update_fact(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        fact_id: str,
        content: str | None = None,
        pinned: bool | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        async with await get_db() as db:
            fact = await db.get(WorkspaceMemoryFact, fact_id)
            if (
                fact is None
                or fact.user_id != owner_id
                or fact.workspace_id != workspace_id
                or fact.deleted_at is not None
            ):
                raise LookupError("workspace memory fact not found")
            if content is not None:
                safe_content = _bounded_text(content, MAX_FACT_CONTENT)
                if not safe_content:
                    raise ValueError("content is required")
                fact.content = safe_content
            if pinned is not None:
                fact.pinned = bool(pinned)
            if status is not None:
                normalized_status = str(status).upper()
                if normalized_status not in {"ACTIVE", "STALE", "ARCHIVED"}:
                    raise ValueError("unsupported workspace memory fact status")
                fact.status = normalized_status
            fact.revision += 1
            fact.updated_at = now
            await db.commit()
            return _fact_dict(fact)

    async def forget_fact(self, *, owner_id: str, workspace_id: str, fact_id: str) -> dict[str, Any]:
        now = _now()
        async with await get_db() as db:
            fact = await db.get(WorkspaceMemoryFact, fact_id)
            if (
                fact is None
                or fact.user_id != owner_id
                or fact.workspace_id != workspace_id
                or fact.deleted_at is not None
            ):
                raise LookupError("workspace memory fact not found")
            fact.deleted_at = now
            fact.status = "ARCHIVED"
            fact.revision += 1
            fact.updated_at = now
            await db.commit()
            return {"workspace_id": workspace_id, "fact_id": fact_id, "forgotten_at": now}

    async def clear(self, *, owner_id: str, workspace_id: str) -> dict[str, Any]:
        now = _now()
        async with await get_db() as db:
            stream = await self._ensure_stream(db, owner_id=owner_id, workspace_id=workspace_id, now=now)
            # SQLite foreign-key enforcement is deployment-configurable, so delete
            # related facts and events explicitly instead of relying only on ON DELETE CASCADE.
            await db.execute(
                delete(WorkspaceMemoryFact).where(
                    WorkspaceMemoryFact.user_id == owner_id,
                    WorkspaceMemoryFact.workspace_id == workspace_id,
                )
            )
            await db.execute(
                delete(WorkspaceMemoryEvent).where(
                    WorkspaceMemoryEvent.stream_id == stream.id,
                    WorkspaceMemoryEvent.user_id == owner_id,
                    WorkspaceMemoryEvent.workspace_id == workspace_id,
                )
            )
            stream.next_sequence = 0
            stream.snapshot = {}
            stream.snapshot_through_sequence = 0
            stream.workspace_fingerprint = None
            stream.updated_at = now
            await db.commit()
            return {"workspace_id": workspace_id, "cleared_at": now, "cursor": 0}


workspace_memory_store = WorkspaceMemoryStore()

__all__ = [
    "MAX_TIMELINE_LIMIT",
    "WorkspaceMemoryStore",
    "workspace_memory_store",
]
