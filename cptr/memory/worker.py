"""Background Memory Core maintenance worker.

Critical context preparation remains synchronous. Expensive/derived work is durable and
restart-safe here so CPTR actions do not wait for consolidation or graph projection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cptr.memory.domain import ConsolidationInput
from cptr.memory.graph import MemoryGraphStore, memory_graph_store
from cptr.memory.jobs import MemoryJobStore, memory_job_store
from cptr.memory.service import EmbeddedMemoryService, get_memory_service

logger = logging.getLogger(__name__)


class MemoryWorker:
    def __init__(
        self,
        *,
        service: EmbeddedMemoryService | None = None,
        job_store: MemoryJobStore | None = None,
        graph_store: MemoryGraphStore | None = None,
    ) -> None:
        self.service = service or get_memory_service()
        self.job_store = job_store or memory_job_store
        self.graph_store = graph_store or memory_graph_store

    async def process(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            raise ValueError("memory job has no id")
        try:
            job_type = str(job.get("job_type") or "")
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            if job_type == "consolidate":
                ref = await self.service.consolidate(
                    ConsolidationInput(
                        user_id=str(job.get("user_id") or ""),
                        workspace=str(job.get("workspace") or ""),
                        scope=str(payload.get("scope") or "workspace"),
                        text=str(payload.get("text") or ""),
                        heading=str(payload.get("heading") or ""),
                        kind=str(payload["kind"]) if payload.get("kind") else None,
                        structured_value=(
                            payload.get("structured_value")
                            if isinstance(payload.get("structured_value"), dict)
                            else {}
                        ),
                        source_event_ids=[
                            str(item) for item in payload.get("source_event_ids", [])
                        ][:200]
                        if isinstance(payload.get("source_event_ids"), list)
                        else [],
                        trust_level=str(payload.get("trust_level") or "agent_observation"),
                        confidence=float(payload.get("confidence") or 0.85),
                        importance=float(payload.get("importance") or 0.5),
                    )
                )
                await self.service.project_graph(
                    user_id=str(job.get("user_id") or ""),
                    workspace=str(job.get("workspace") or ""),
                    memory_id=ref.memory_id,
                    heading=str(payload.get("heading") or ""),
                )
            elif job_type == "graph_project":
                await self.service.project_graph(
                    user_id=str(job.get("user_id") or ""),
                    workspace=str(job.get("workspace") or ""),
                    memory_id=str(payload.get("memory_id") or ""),
                    heading=str(payload.get("heading") or ""),
                )
            else:
                raise ValueError(f"unsupported memory job type: {job_type}")
            await self.job_store.complete(job_id)
        except Exception as exc:
            await self.job_store.fail(job_id, error=exc)
            logger.debug("memory job %s failed", job_id, exc_info=True)


async def memory_worker_loop() -> None:
    worker = MemoryWorker()
    await worker.job_store.recover_stale()
    while True:
        try:
            from cptr.utils.memory import get_memory_settings

            settings = await get_memory_settings()
            interval = max(5, int(settings.get("maintenance_interval_seconds") or 30))
            if not bool(settings.get("maintenance_enabled", True)):
                await asyncio.sleep(interval)
                continue
            processed = 0
            # Bound each turn so a backlog cannot monopolize the event loop.
            for _ in range(20):
                job = await worker.job_store.claim_due()
                if job is None:
                    break
                await worker.process(job)
                processed += 1
            await asyncio.sleep(0 if processed >= 20 else interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("memory maintenance worker iteration failed", exc_info=True)
            await asyncio.sleep(10)
