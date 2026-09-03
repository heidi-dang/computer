"""Durable structured-reasoning history for Dark Factory role isolation."""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import FactoryCycle, FactoryReasoningCall, FactoryRun
from cptr.services.factory_reasoning import (
    ReasoningRole,
    StructuredReasoningResult,
    _safe_provider_metadata,
)
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_sensitive

_MAX_REASONING_PAYLOAD_BYTES = 64 * 1024


def _bounded_safe_payload(payload: Any) -> Any:
    safe = redact_sensitive(payload)
    encoded = json.dumps(
        safe,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    if len(encoded) > _MAX_REASONING_PAYLOAD_BYTES:
        raise ValueError(
            f"factory reasoning payload exceeds {_MAX_REASONING_PAYLOAD_BYTES} bytes"
        )
    return safe


class SqlFactoryReasoningStore:
    """Append-only reasoning ledger and per-role continuation checkpoint source."""

    def __init__(self, *, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def record_result(self, result: StructuredReasoningResult) -> FactoryReasoningCall:
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, result.run_id)
                cycle = await db.get(FactoryCycle, result.cycle_id)
                if run is None:
                    raise KeyError("factory run not found")
                if cycle is None or cycle.run_id != run.id:
                    raise KeyError("factory cycle not found")

                ordinal_result = await db.execute(
                    select(func.max(FactoryReasoningCall.role_ordinal)).where(
                        FactoryReasoningCall.cycle_id == result.cycle_id,
                        FactoryReasoningCall.role == result.role.value,
                    )
                )
                role_ordinal = int(ordinal_result.scalar_one_or_none() or 0) + 1
                row = FactoryReasoningCall(
                    run_id=result.run_id,
                    cycle_id=result.cycle_id,
                    role=result.role.value,
                    role_ordinal=role_ordinal,
                    schema_id=result.schema_id,
                    provider=result.provider,
                    model=result.model,
                    response_id=result.response_id,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    total_tokens=result.total_tokens,
                    runtime_ms=result.runtime_ms,
                    cost_microusd=int(round(result.cost_usd * 1_000_000)),
                    attempt_count=result.attempt_count,
                    data=_bounded_safe_payload(result.data),
                    provider_metadata=_bounded_safe_payload(
                        _safe_provider_metadata(result.provider_metadata)
                    ),
                    created_at=int(time.time() * 1000),
                )
                db.add(row)
                await db.flush()
                return row

    async def latest_response_id(
        self,
        *,
        run_id: str,
        cycle_id: str,
        role: ReasoningRole,
    ) -> str | None:
        async with self._session_factory() as db:
            result = await db.execute(
                select(FactoryReasoningCall.response_id)
                .where(
                    FactoryReasoningCall.run_id == run_id,
                    FactoryReasoningCall.cycle_id == cycle_id,
                    FactoryReasoningCall.role == role.value,
                    FactoryReasoningCall.response_id.is_not(None),
                )
                .order_by(FactoryReasoningCall.role_ordinal.desc())
                .limit(1)
            )
            value = result.scalar_one_or_none()
            return str(value) if value else None

    async def list_results(
        self,
        *,
        run_id: str,
        cycle_id: str | None = None,
        role: ReasoningRole | None = None,
        limit: int = 100,
    ) -> list[FactoryReasoningCall]:
        if limit < 1 or limit > 500:
            raise ValueError("reasoning result limit must be between 1 and 500")
        async with self._session_factory() as db:
            query = select(FactoryReasoningCall).where(FactoryReasoningCall.run_id == run_id)
            if cycle_id is not None:
                query = query.where(FactoryReasoningCall.cycle_id == cycle_id)
            if role is not None:
                query = query.where(FactoryReasoningCall.role == role.value)
                query = query.order_by(FactoryReasoningCall.role_ordinal.desc())
            else:
                query = query.order_by(
                    FactoryReasoningCall.created_at.desc(),
                    FactoryReasoningCall.id.desc(),
                )
            rows = await db.execute(query.limit(limit))
            return list(rows.scalars().all())
