"""Transactional persistence for the durable Dark Factory core domain."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import FactoryCycle, FactoryEvent, FactoryEvidence, FactoryGateResult, FactoryRun
from cptr.services.factory_domain import (
    FactoryActor,
    FactoryState,
    is_terminal_factory_state,
    validate_factory_transition,
)
from cptr.services.factory_gates import EvidenceAuthority
from cptr.services.factory_victory import FactoryVictoryDecision, is_machine_issued_victory
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_sensitive

_MAX_FACTORY_PAYLOAD_BYTES = 64 * 1024


class FactoryIdempotencyConflict(ValueError):
    """The same idempotency key was replayed with a different operation payload."""


class FactoryPayloadTooLarge(ValueError):
    """A durable factory payload exceeded the bounded persistence limit."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical_payload(payload: Any) -> tuple[Any, str]:
    safe = redact_sensitive(payload)
    encoded = json.dumps(
        safe,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    if len(encoded) > _MAX_FACTORY_PAYLOAD_BYTES:
        raise FactoryPayloadTooLarge(
            f"factory payload exceeds {_MAX_FACTORY_PAYLOAD_BYTES} bytes"
        )
    return safe, hashlib.sha256(encoded).hexdigest()


def _config_fingerprint(*, policy: dict[str, Any], budget: dict[str, Any], model_id: str | None) -> str:
    _, digest = _canonical_payload(
        {"policy": policy, "budget": budget, "model_id": model_id or ""}
    )
    return digest


class SqlFactoryStore:
    """SQLite-backed source of truth for factory state and immutable evidence."""

    def __init__(self, *, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def create_run(
        self,
        *,
        user_id: str,
        workspace_id: str,
        mission: str,
        acceptance_criteria: Sequence[str],
        policy: dict[str, Any],
        budget: dict[str, Any],
        model_id: str | None,
        idempotency_key: str | None,
    ) -> FactoryRun:
        mission = mission.strip()
        criteria = [str(item).strip() for item in acceptance_criteria if str(item).strip()]
        if not mission:
            raise ValueError("mission must not be blank")
        if not criteria:
            raise ValueError("acceptance criteria must not be empty")

        async with self._session_factory() as db:
            async with db.begin():
                if idempotency_key:
                    existing = (
                        await db.execute(
                            select(FactoryRun).where(
                                FactoryRun.user_id == user_id,
                                FactoryRun.idempotency_key == idempotency_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        return existing

                now = _now_ms()
                run = FactoryRun(
                    id=_new_id("factory"),
                    user_id=user_id,
                    workspace_id=workspace_id,
                    mission=mission,
                    acceptance_criteria=criteria,
                    model_id=model_id,
                    state=FactoryState.MISSION.value,
                    policy=redact_sensitive(policy),
                    budget=redact_sensitive(budget),
                    config_fingerprint=_config_fingerprint(
                        policy=policy,
                        budget=budget,
                        model_id=model_id,
                    ),
                    idempotency_key=idempotency_key,
                    created_at=now,
                    updated_at=now,
                )
                db.add(run)
                payload, digest = _canonical_payload(
                    {
                        "workspace_id": workspace_id,
                        "acceptance_criteria_count": len(criteria),
                    }
                )
                db.add(
                    FactoryEvent(
                        id=_new_id("fev"),
                        run_id=run.id,
                        sequence=1,
                        actor=FactoryActor.SYSTEM.value,
                        event_type="run.created",
                        from_state=None,
                        to_state=FactoryState.MISSION.value,
                        idempotency_key=(f"create:{idempotency_key}" if idempotency_key else None),
                        payload_digest=digest,
                        payload=payload,
                        created_at=now,
                    )
                )
            return run

    async def get_run(self, run_id: str, *, user_id: str | None = None) -> FactoryRun | None:
        async with self._session_factory() as db:
            query = select(FactoryRun).where(FactoryRun.id == run_id)
            if user_id is not None:
                query = query.where(FactoryRun.user_id == user_id)
            return (await db.execute(query)).scalar_one_or_none()

    async def create_cycle(
        self,
        run_id: str,
        *,
        base_revision: str | None,
        base_fingerprint: str | None,
        idempotency_key: str | None,
    ) -> FactoryCycle:
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                if run is None:
                    raise KeyError("factory run not found")
                if is_terminal_factory_state(FactoryState(run.state)):
                    raise ValueError("cannot create a cycle for a terminal factory run")
                if idempotency_key:
                    existing = (
                        await db.execute(
                            select(FactoryCycle).where(
                                FactoryCycle.run_id == run_id,
                                FactoryCycle.idempotency_key == idempotency_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        return existing
                last_ordinal = (
                    await db.execute(
                        select(func.max(FactoryCycle.ordinal)).where(FactoryCycle.run_id == run_id)
                    )
                ).scalar_one()
                now = _now_ms()
                cycle = FactoryCycle(
                    id=_new_id("cycle"),
                    run_id=run_id,
                    ordinal=int(last_ordinal or 0) + 1,
                    state=run.state,
                    idempotency_key=idempotency_key,
                    base_revision=base_revision,
                    base_fingerprint=base_fingerprint,
                    created_at=now,
                    updated_at=now,
                )
                db.add(cycle)
                run.current_cycle_id = cycle.id
                run.updated_at = now
                await self._append_event_in_transaction(
                    db,
                    run=run,
                    cycle_id=cycle.id,
                    actor=FactoryActor.SYSTEM,
                    event_type="cycle.created",
                    from_state=FactoryState(run.state),
                    to_state=FactoryState(run.state),
                    idempotency_key=(f"cycle:{idempotency_key}" if idempotency_key else None),
                    payload={
                        "ordinal": cycle.ordinal,
                        "base_revision": base_revision,
                        "base_fingerprint": base_fingerprint,
                    },
                )
            return cycle

    async def list_cycles(self, run_id: str) -> list[FactoryCycle]:
        async with self._session_factory() as db:
            rows = await db.execute(
                select(FactoryCycle)
                .where(FactoryCycle.run_id == run_id)
                .order_by(FactoryCycle.ordinal)
            )
            return list(rows.scalars().all())

    async def set_cycle_target(
        self,
        run_id: str,
        cycle_id: str,
        *,
        revision: str,
        fingerprint: str,
        idempotency_key: str,
    ) -> FactoryCycle:
        if not revision.strip() or not fingerprint.strip():
            raise ValueError("cycle target revision and fingerprint must not be blank")
        payload = {
            "cycle_id": cycle_id,
            "revision": revision,
            "fingerprint": fingerprint,
        }
        _, digest = _canonical_payload(payload)
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                cycle = await db.get(FactoryCycle, cycle_id)
                if run is None or cycle is None or cycle.run_id != run_id:
                    raise KeyError("factory run/cycle not found")
                existing = (
                    await db.execute(
                        select(FactoryEvent).where(
                            FactoryEvent.run_id == run_id,
                            FactoryEvent.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.payload_digest != digest:
                        raise FactoryIdempotencyConflict(
                            "cycle target idempotency key was replayed with different intent"
                        )
                    return cycle
                now = _now_ms()
                cycle.target_revision = revision
                cycle.target_fingerprint = fingerprint
                cycle.updated_at = now
                run.updated_at = now
                await self._append_event_in_transaction(
                    db,
                    run=run,
                    cycle_id=cycle_id,
                    actor=FactoryActor.SYSTEM,
                    event_type="cycle.target_updated",
                    from_state=FactoryState(run.state),
                    to_state=FactoryState(run.state),
                    idempotency_key=idempotency_key,
                    payload=payload,
                    payload_digest=digest,
                )
            return cycle

    async def transition(
        self,
        run_id: str,
        *,
        to_state: FactoryState,
        actor: FactoryActor,
        reason: str,
        idempotency_key: str,
        evidence_ids: Sequence[str] = (),
        resumable_state: FactoryState | None = None,
    ) -> FactoryRun:
        if not idempotency_key.strip():
            raise ValueError("transition idempotency key must not be blank")
        intent_payload = {
            "to_state": to_state.value,
            "actor": actor.value,
            "reason": reason,
            "evidence_ids": list(evidence_ids),
            "resumable_state": resumable_state.value if resumable_state else None,
        }
        _, intent_digest = _canonical_payload(intent_payload)

        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                if run is None:
                    raise KeyError("factory run not found")
                existing = (
                    await db.execute(
                        select(FactoryEvent).where(
                            FactoryEvent.run_id == run_id,
                            FactoryEvent.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.payload_digest != intent_digest:
                        raise FactoryIdempotencyConflict(
                            "factory transition idempotency key was replayed with different intent"
                        )
                    return run

                current = FactoryState(run.state)
                effective_resumable = resumable_state
                if current in {FactoryState.PAUSED, FactoryState.APPROVAL_REQUIRED}:
                    effective_resumable = (
                        FactoryState(run.resumable_state) if run.resumable_state else resumable_state
                    )
                validate_factory_transition(
                    current,
                    to_state,
                    actor,
                    resumable_state=effective_resumable,
                    machine_victory=False,
                )

                now = _now_ms()
                previous = current
                if to_state in {FactoryState.PAUSED, FactoryState.APPROVAL_REQUIRED}:
                    run.resumable_state = current.value
                elif current in {FactoryState.PAUSED, FactoryState.APPROVAL_REQUIRED}:
                    run.resumable_state = None
                run.state = to_state.value
                run.updated_at = now
                if run.current_cycle_id:
                    cycle = await db.get(FactoryCycle, run.current_cycle_id)
                    if cycle is not None:
                        cycle.state = to_state.value
                        cycle.updated_at = now
                if is_terminal_factory_state(to_state):
                    run.completed_at = now
                await self._append_event_in_transaction(
                    db,
                    run=run,
                    cycle_id=run.current_cycle_id,
                    actor=actor,
                    event_type="state.transition",
                    from_state=previous,
                    to_state=to_state,
                    idempotency_key=idempotency_key,
                    payload=intent_payload,
                    payload_digest=intent_digest,
                )
            return run

    async def authorize_victory(
        self,
        run_id: str,
        cycle_id: str,
        decision: FactoryVictoryDecision,
        *,
        idempotency_key: str,
    ) -> FactoryRun:
        if not is_machine_issued_victory(decision):
            raise TypeError("Victory authorization requires a machine-issued FactoryVictoryDecision")
        if not decision.passed:
            raise ValueError("failed Victory decision cannot authorize the commit path")
        payload = {
            "cycle_id": cycle_id,
            "satisfied_gate_ids": list(decision.satisfied_gate_ids),
            "evaluated_revision": decision.evaluated_revision,
            "evaluated_fingerprint": decision.evaluated_fingerprint,
        }
        _, digest = _canonical_payload(payload)
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                cycle = await db.get(FactoryCycle, cycle_id)
                if run is None or cycle is None or cycle.run_id != run_id:
                    raise KeyError("factory run/cycle not found")
                if run.current_cycle_id != cycle_id:
                    raise ValueError("Victory decision does not target the active factory cycle")
                if not cycle.target_revision or not cycle.target_fingerprint:
                    raise ValueError("factory cycle has no target revision/fingerprint for Victory")
                if (
                    decision.evaluated_revision != cycle.target_revision
                    or decision.evaluated_fingerprint != cycle.target_fingerprint
                ):
                    raise ValueError("stale Victory decision for the current cycle target")
                existing = (
                    await db.execute(
                        select(FactoryEvent).where(
                            FactoryEvent.run_id == run_id,
                            FactoryEvent.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.payload_digest != digest:
                        raise FactoryIdempotencyConflict(
                            "Victory idempotency key was replayed with different evidence"
                        )
                    return run

                current = FactoryState(run.state)
                validate_factory_transition(
                    current,
                    FactoryState.COMMITTING,
                    FactoryActor.SYSTEM,
                    machine_victory=True,
                )
                now = _now_ms()
                run.state = FactoryState.COMMITTING.value
                run.updated_at = now
                cycle.state = FactoryState.COMMITTING.value
                cycle.updated_at = now
                await self._append_event_in_transaction(
                    db,
                    run=run,
                    cycle_id=cycle_id,
                    actor=FactoryActor.SYSTEM,
                    event_type="victory.authorized",
                    from_state=current,
                    to_state=FactoryState.COMMITTING,
                    idempotency_key=idempotency_key,
                    payload=payload,
                    payload_digest=digest,
                )
            return run

    async def append_evidence(
        self,
        *,
        run_id: str,
        cycle_id: str | None,
        gate_id: str | None,
        kind: str,
        source: str,
        authority: EvidenceAuthority,
        revision: str | None,
        fingerprint: str | None,
        payload: dict[str, Any],
    ) -> FactoryEvidence:
        safe_payload, digest = _canonical_payload(payload)
        row = FactoryEvidence(
            id=_new_id("fevidence"),
            run_id=run_id,
            cycle_id=cycle_id,
            gate_id=gate_id,
            kind=kind,
            source=source,
            authority=authority.value,
            revision=revision,
            fingerprint=fingerprint,
            digest=digest,
            payload=safe_payload,
            created_at=_now_ms(),
        )
        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
        return row

    async def list_evidence(self, run_id: str, *, limit: int = 100) -> list[FactoryEvidence]:
        limit = max(1, min(int(limit), 500))
        async with self._session_factory() as db:
            rows = await db.execute(
                select(FactoryEvidence)
                .where(FactoryEvidence.run_id == run_id)
                .order_by(FactoryEvidence.created_at, FactoryEvidence.id)
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def record_gate(
        self,
        *,
        run_id: str,
        cycle_id: str,
        gate_id: str,
        category: str,
        required: bool,
        applicable: bool,
        status: str,
        evidence_ids: Sequence[str],
        evaluated_revision: str | None,
        evaluated_fingerprint: str | None,
        reason: str | None,
        attempt: int,
    ) -> FactoryGateResult:
        now = _now_ms()
        row = FactoryGateResult(
            id=_new_id("fgate"),
            run_id=run_id,
            cycle_id=cycle_id,
            gate_id=gate_id,
            category=category,
            required=required,
            applicable=applicable,
            status=status,
            evidence_ids=list(evidence_ids),
            evaluated_revision=evaluated_revision,
            evaluated_fingerprint=evaluated_fingerprint,
            reason=reason,
            attempt=attempt,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
        return row

    async def list_gates(
        self,
        run_id: str,
        *,
        cycle_id: str | None = None,
    ) -> list[FactoryGateResult]:
        async with self._session_factory() as db:
            query = select(FactoryGateResult).where(FactoryGateResult.run_id == run_id)
            if cycle_id is not None:
                query = query.where(FactoryGateResult.cycle_id == cycle_id)
            rows = await db.execute(
                query.order_by(
                    FactoryGateResult.cycle_id,
                    FactoryGateResult.gate_id,
                    FactoryGateResult.attempt,
                )
            )
            return list(rows.scalars().all())

    async def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[FactoryEvent]:
        limit = max(1, min(int(limit), 500))
        async with self._session_factory() as db:
            rows = await db.execute(
                select(FactoryEvent)
                .where(
                    FactoryEvent.run_id == run_id,
                    FactoryEvent.sequence > max(0, int(after_sequence)),
                )
                .order_by(FactoryEvent.sequence)
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def claim_run(
        self,
        run_id: str,
        *,
        lease_token: str,
        now_ms: int,
        lease_ms: int,
    ) -> bool:
        if not lease_token:
            raise ValueError("lease token must not be blank")
        if lease_ms <= 0:
            raise ValueError("lease duration must be positive")
        terminal = [state.value for state in FactoryState if is_terminal_factory_state(state)]
        async with self._session_factory() as db:
            result = await db.execute(
                update(FactoryRun)
                .where(
                    FactoryRun.id == run_id,
                    FactoryRun.state.not_in(terminal),
                    or_(
                        FactoryRun.lease_token.is_(None),
                        FactoryRun.lease_expires_at.is_(None),
                        FactoryRun.lease_expires_at < now_ms,
                        FactoryRun.lease_token == lease_token,
                    ),
                )
                .values(
                    lease_token=lease_token,
                    lease_expires_at=now_ms + lease_ms,
                    updated_at=now_ms,
                )
            )
            await db.commit()
            return bool(result.rowcount == 1)

    async def renew_run(
        self,
        run_id: str,
        *,
        lease_token: str,
        now_ms: int,
        lease_ms: int,
    ) -> bool:
        async with self._session_factory() as db:
            result = await db.execute(
                update(FactoryRun)
                .where(
                    FactoryRun.id == run_id,
                    FactoryRun.lease_token == lease_token,
                    FactoryRun.lease_expires_at >= now_ms,
                )
                .values(lease_expires_at=now_ms + lease_ms, updated_at=now_ms)
            )
            await db.commit()
            return bool(result.rowcount == 1)

    async def release_run(self, run_id: str, *, lease_token: str) -> bool:
        async with self._session_factory() as db:
            result = await db.execute(
                update(FactoryRun)
                .where(
                    FactoryRun.id == run_id,
                    FactoryRun.lease_token == lease_token,
                )
                .values(lease_token=None, lease_expires_at=None, updated_at=_now_ms())
            )
            await db.commit()
            return bool(result.rowcount == 1)

    async def list_recoverable(self) -> list[FactoryRun]:
        terminal = [state.value for state in FactoryState if is_terminal_factory_state(state)]
        async with self._session_factory() as db:
            rows = await db.execute(
                select(FactoryRun)
                .where(FactoryRun.state.not_in(terminal))
                .order_by(FactoryRun.updated_at, FactoryRun.id)
            )
            return list(rows.scalars().all())

    async def _append_event_in_transaction(
        self,
        db,
        *,
        run: FactoryRun,
        cycle_id: str | None,
        actor: FactoryActor,
        event_type: str,
        from_state: FactoryState | None,
        to_state: FactoryState | None,
        idempotency_key: str | None,
        payload: dict[str, Any],
        payload_digest: str | None = None,
    ) -> FactoryEvent:
        safe_payload, calculated_digest = _canonical_payload(payload)
        sequence = (
            await db.execute(
                select(func.max(FactoryEvent.sequence)).where(FactoryEvent.run_id == run.id)
            )
        ).scalar_one()
        row = FactoryEvent(
            id=_new_id("fev"),
            run_id=run.id,
            cycle_id=cycle_id,
            sequence=int(sequence or 0) + 1,
            actor=actor.value,
            event_type=event_type,
            from_state=from_state.value if from_state else None,
            to_state=to_state.value if to_state else None,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest or calculated_digest,
            payload=safe_payload,
            created_at=_now_ms(),
        )
        db.add(row)
        return row
