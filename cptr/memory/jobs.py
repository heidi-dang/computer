"""Durable asynchronous work queue for Memory Core maintenance."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select

from cptr.models import MemoryJob
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_sensitive, redact_text


def _now_ms() -> int:
    return int(time.time() * 1000)


class MemoryJobStore:
    def __init__(self, *, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()

    @asynccontextmanager
    async def _session(self):
        async with self._session_factory() as db:
            yield db

    async def enqueue(
        self,
        *,
        user_id: str,
        workspace: str,
        job_type: str,
        payload: dict[str, Any],
        not_before_ms: int | None = None,
    ) -> str:
        now = _now_ms()
        row = MemoryJob(
            user_id=user_id,
            workspace=str(workspace or ""),
            job_type=redact_text(job_type).strip()[:120] or "maintenance",
            status="pending",
            payload=redact_sensitive(payload),
            attempts=0,
            not_before_ms=int(not_before_ms if not_before_ms is not None else now),
            created_at_ms=now,
            updated_at_ms=now,
        )
        async with self._session() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row.id

    async def claim_due(self, *, now_ms: int | None = None) -> dict[str, Any] | None:
        now = int(now_ms if now_ms is not None else _now_ms())
        async with self._session() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(MemoryJob)
                        .where(
                            MemoryJob.status == "pending",
                            MemoryJob.not_before_ms <= now,
                        )
                        .order_by(MemoryJob.not_before_ms.asc(), MemoryJob.created_at_ms.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                row.status = "running"
                row.attempts = int(row.attempts or 0) + 1
                row.updated_at_ms = now
                await db.flush()
                return {
                    "job_id": row.id,
                    "user_id": row.user_id,
                    "workspace": row.workspace,
                    "job_type": row.job_type,
                    "payload": row.payload if isinstance(row.payload, dict) else {},
                    "attempts": int(row.attempts),
                }

    async def complete(self, job_id: str) -> None:
        async with self._session() as db:
            async with db.begin():
                row = await db.get(MemoryJob, job_id)
                if row is None:
                    return
                row.status = "complete"
                row.last_error = None
                row.updated_at_ms = _now_ms()

    async def fail(
        self,
        job_id: str,
        *,
        error: Exception,
        max_attempts: int = 5,
        retry_delay_ms: int = 10_000,
    ) -> None:
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                row = await db.get(MemoryJob, job_id)
                if row is None:
                    return
                row.last_error = redact_text(type(error).__name__)[:500]
                if int(row.attempts or 0) >= max(1, int(max_attempts)):
                    row.status = "failed"
                else:
                    row.status = "pending"
                    row.not_before_ms = now + max(1_000, int(retry_delay_ms))
                row.updated_at_ms = now

    async def recover_stale(self, *, stale_after_ms: int = 300_000) -> int:
        cutoff = _now_ms() - max(30_000, int(stale_after_ms))
        recovered = 0
        async with self._session() as db:
            async with db.begin():
                rows = list(
                    (
                        await db.scalars(
                            select(MemoryJob).where(
                                MemoryJob.status == "running",
                                MemoryJob.updated_at_ms < cutoff,
                            )
                        )
                    ).all()
                )
                for row in rows:
                    row.status = "pending"
                    row.not_before_ms = _now_ms()
                    row.last_error = "recovered_after_restart"
                    row.updated_at_ms = _now_ms()
                    recovered += 1
        return recovered

    async def counts(
        self, *, user_id: str | None = None, workspace: str | None = None
    ) -> dict[str, int]:
        predicates = []
        if user_id is not None:
            predicates.append(MemoryJob.user_id == user_id)
        if workspace is not None:
            predicates.append(MemoryJob.workspace == str(workspace or ""))
        async with self._session() as db:
            rows = list((await db.scalars(select(MemoryJob).where(*predicates))).all())
        counts = {"pending": 0, "running": 0, "complete": 0, "failed": 0}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts


memory_job_store = MemoryJobStore()
