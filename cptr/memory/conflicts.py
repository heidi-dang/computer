"""Contradiction detection and durable conflict records for CPTR Memory Core."""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import delete, or_, select

from cptr.memory.domain import MemoryConflictRef
from cptr.models import MemoryConflict
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_text

_FACT_RE = re.compile(
    r"^\s*(?P<subject>[A-Za-z0-9_. /#-]{2,160}?)\s+"
    r"(?P<predicate>is|are|uses?|runs\s+on|points\s+to|equals?)\s+"
    r"(?P<value>[^.\n]{1,300})[.!]?\s*$",
    re.IGNORECASE,
)
_ASSIGNMENT_RE = re.compile(
    r"^\s*(?P<subject>[A-Za-z0-9_. /#-]{2,160}?)\s*(?:=|:)\s*(?P<value>[^\n]{1,300})\s*$"
)
_HIGH_TRUST = {"user_directive", "verified_system_fact", "tool_result", "managed_memory"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize(value: Any, limit: int = 500) -> str:
    return " ".join(redact_text(str(value or "")).lower().split())[:limit]


def fact_signature(row: dict[str, Any]) -> tuple[str, str] | None:
    structured = (
        row.get("structured_value") if isinstance(row.get("structured_value"), dict) else {}
    )
    subject = structured.get("subject") or structured.get("fact_subject")
    predicate = structured.get("predicate") or structured.get("fact_predicate")
    value = structured.get("value") if "value" in structured else structured.get("fact_value")
    if subject and value is not None:
        key = f"{_normalize(subject, 200)}|{_normalize(predicate or 'is', 80)}"
        normalized_value = _normalize(value, 300)
        return (key, normalized_value) if normalized_value else None

    text = " ".join(str(row.get("canonical_text") or "").split())
    match = _FACT_RE.match(text)
    if match:
        return (
            f"{_normalize(match.group('subject'), 200)}|{_normalize(match.group('predicate'), 80)}",
            _normalize(match.group("value"), 300),
        )
    match = _ASSIGNMENT_RE.match(text)
    if match:
        return (
            f"{_normalize(match.group('subject'), 200)}|equals",
            _normalize(match.group("value"), 300),
        )
    return None


def conflict_classification(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, float, str]:
    left_from = int(left.get("valid_from_ms") or 0)
    right_from = int(right.get("valid_from_ms") or 0)
    right_trust = str(right.get("trust_level") or "")
    if right_from > left_from and right_trust in _HIGH_TRUST:
        return (
            "temporal_change_candidate",
            0.94,
            "same fact key has a newer high-trust value; temporal supersession requires resolution",
        )
    return (
        "contradiction",
        0.88,
        "same fact key has incompatible active values",
    )


class MemoryConflictStore:
    def __init__(self, *, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()

    @asynccontextmanager
    async def _session(self):
        async with self._session_factory() as db:
            yield db

    async def clear_scope(self, *, user_id: str, workspace: str) -> None:
        async with self._session() as db:
            async with db.begin():
                await db.execute(
                    delete(MemoryConflict).where(
                        MemoryConflict.user_id == user_id,
                        MemoryConflict.workspace == str(workspace or ""),
                    )
                )

    async def record(
        self,
        *,
        user_id: str,
        workspace: str,
        fact_key: str,
        left_memory_id: str,
        right_memory_id: str,
        classification: str,
        confidence: float,
        reason: str,
    ) -> MemoryConflictRef:
        if left_memory_id == right_memory_id:
            raise ValueError("conflict requires two distinct memories")
        now = _now_ms()
        workspace = str(workspace or "")
        async with self._session() as db:
            async with db.begin():
                existing = (
                    await db.execute(
                        select(MemoryConflict).where(
                            MemoryConflict.user_id == user_id,
                            MemoryConflict.workspace == workspace,
                            MemoryConflict.fact_key == fact_key,
                            or_(
                                (
                                    (MemoryConflict.left_memory_id == left_memory_id)
                                    & (MemoryConflict.right_memory_id == right_memory_id)
                                ),
                                (
                                    (MemoryConflict.left_memory_id == right_memory_id)
                                    & (MemoryConflict.right_memory_id == left_memory_id)
                                ),
                            ),
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing = MemoryConflict(
                        user_id=user_id,
                        workspace=workspace,
                        fact_key=_normalize(fact_key, 500),
                        left_memory_id=left_memory_id,
                        right_memory_id=right_memory_id,
                        classification=classification,
                        status="open",
                        confidence_ppm=max(0, min(1_000_000, int(confidence * 1_000_000))),
                        reason=redact_text(reason)[:1000],
                        created_at_ms=now,
                        updated_at_ms=now,
                    )
                    db.add(existing)
                    await db.flush()
                return self._ref(existing)

    async def get(self, conflict_id: str, *, user_id: str) -> dict[str, Any]:
        row = await self.lookup(conflict_id)
        if row["user_id"] != user_id:
            raise KeyError("memory conflict not found")
        return {key: value for key, value in row.items() if key not in {"user_id", "workspace"}}

    async def lookup(self, conflict_id: str) -> dict[str, Any]:
        """Internal lookup used by MemoryService after the conflict ID is already trusted."""
        async with self._session() as db:
            row = await db.get(MemoryConflict, conflict_id)
            if row is None:
                raise KeyError("memory conflict not found")
            return {**self._dict(row), "user_id": row.user_id, "workspace": row.workspace}

    async def list(
        self,
        *,
        user_id: str,
        workspace: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        predicates = [MemoryConflict.user_id == user_id]
        if workspace is not None:
            predicates.append(MemoryConflict.workspace == str(workspace or ""))
        if status:
            predicates.append(MemoryConflict.status == status)
        async with self._session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(MemoryConflict)
                        .where(*predicates)
                        .order_by(MemoryConflict.updated_at_ms.desc())
                        .limit(max(1, min(int(limit), 500)))
                    )
                ).all()
            )
        return [self._dict(row) for row in rows]

    async def resolve(self, conflict_id: str, *, user_id: str, resolution: str) -> dict[str, Any]:
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                row = await db.get(MemoryConflict, conflict_id)
                if row is None or row.user_id != user_id:
                    raise KeyError("memory conflict not found")
                row.status = "resolved"
                row.resolution = redact_text(resolution).strip()[:120]
                row.updated_at_ms = now
                await db.flush()
                return self._dict(row)

    @staticmethod
    def _ref(row: MemoryConflict) -> MemoryConflictRef:
        return MemoryConflictRef(
            conflict_id=row.id,
            fact_key=row.fact_key,
            left_memory_id=row.left_memory_id,
            right_memory_id=row.right_memory_id,
            classification=row.classification,
            status=row.status,
            confidence=int(row.confidence_ppm or 0) / 1_000_000,
        )

    @staticmethod
    def _dict(row: MemoryConflict) -> dict[str, Any]:
        return {
            "conflict_id": row.id,
            "fact_key": row.fact_key,
            "left_memory_id": row.left_memory_id,
            "right_memory_id": row.right_memory_id,
            "classification": row.classification,
            "status": row.status,
            "confidence": int(row.confidence_ppm or 0) / 1_000_000,
            "reason": row.reason,
            "resolution": row.resolution,
            "created_at_ms": int(row.created_at_ms),
            "updated_at_ms": int(row.updated_at_ms),
        }
